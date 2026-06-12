"""batch_tool_use_experiment.py — Batch Retailer Tool Use 실험 Harness

production phase3.py와 완전히 독립된 실험 파일.
retailer 1건 ~ 100건에 대해 Claude tool_use 루프를 반복 실행하고
성능·품질·실패 특성을 계측한다.

Mock Claude 사용: 실제 API 호출 없음. Tool 실행(lookup_retailer, confirm_mapping)은 실제 코드.
→ Tool Layer 동작과 Metrics 누적은 실제로 검증된다.

측정 항목:
  retailer 수 / tool call 수 / lookup·confirm 호출 수
  Claude turn 수 / 평균 turn / max_turns 도달 여부
  성공·실패 수 / elapsed time / metrics snapshot

Failure 시나리오:
  SCENARIO_SUCCESS       — 정상: lookup → confirm → end_turn
  SCENARIO_NOT_FOUND     — 조회 불가: lookup → end_turn (confirm 없음)
  SCENARIO_TOOL_EXCEPTION — dispatch 예외 발생 후 루프 계속
  SCENARIO_INVALID_TOOL  — allowlist 외 tool 호출 → 차단 후 end_turn
  SCENARIO_MAX_TURNS     — max_turns 초과 → RuntimeError
"""
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ..tools.metrics import ToolMetrics, get_metrics, reset_metrics
from .phase3_tool_use_experiment import run_retailer_mapping_experiment

__all__ = [
    "SCENARIO_SUCCESS",
    "SCENARIO_NOT_FOUND",
    "SCENARIO_TOOL_EXCEPTION",
    "SCENARIO_INVALID_TOOL",
    "SCENARIO_MAX_TURNS",
    "RetailerBatchResult",
    "BatchStats",
    "BatchExperimentResult",
    "run_batch_retailer_experiment",
    "format_batch_report",
]

# ── 시나리오 상수 ─────────────────────────────────────────────────────────────

SCENARIO_SUCCESS        = "success"
SCENARIO_NOT_FOUND      = "not_found"
SCENARIO_TOOL_EXCEPTION = "tool_exception"
SCENARIO_INVALID_TOOL   = "invalid_tool"
SCENARIO_MAX_TURNS      = "max_turns"


# ── 결과 타입 ─────────────────────────────────────────────────────────────────

@dataclass
class RetailerBatchResult:
    """단일 retailer 실험 결과."""
    ocr_name: str
    success: bool             # RuntimeError 없이 end_turn 도달
    confirmed_code: str | None
    lookup_basis: str | None  # "cache" | "bracket_code" | "candidate" | "not_found"
    tool_call_count: int
    lookup_call_count: int
    confirm_call_count: int
    turns_used: int
    max_turns_hit: bool
    elapsed_ms: float
    error: str | None = None
    # Claude API token usage (defensive — mock 환경에서는 0)
    input_tokens:  int = 0
    output_tokens: int = 0
    api_call_count: int = 0
    tool_not_called: bool = False   # lookup_retailer 없이 end_turn 반환한 경우


@dataclass
class BatchStats:
    """batch 전체 집계 통계."""
    batch_size: int
    success_count: int
    failure_count: int           # exception 또는 max_turns 포함
    max_turns_hit_count: int
    not_found_count: int         # lookup_basis == "not_found" 건수
    total_tool_calls: int
    total_lookup_calls: int
    total_confirm_calls: int
    total_turns: int
    avg_turns: float
    elapsed_ms: float
    metrics_snapshot: dict[str, ToolMetrics] = field(default_factory=dict)
    # 전체 token usage 합계
    total_input_tokens:  int = 0
    total_output_tokens: int = 0
    total_api_calls:     int = 0
    tool_not_called_count: int = 0  # tool 미호출 end_turn 건수 (계약 위반)


@dataclass
class BatchExperimentResult:
    """batch 실험 전체 결과."""
    scenario: str
    batch_size: int
    stats: BatchStats
    per_retailer: list[RetailerBatchResult]


# ── Mock 클라이언트 헬퍼 ──────────────────────────────────────────────────────

def _text_block(text: str) -> MagicMock:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_block(tool_id: str, name: str, input_dict: dict) -> MagicMock:
    b = MagicMock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = input_dict
    return b


def _response(stop_reason: str, *blocks: MagicMock) -> MagicMock:
    r = MagicMock()
    r.stop_reason = stop_reason
    r.content = list(blocks)
    return r


def _make_scenario_client(
    scenario: str,
    ocr_name: str,
    max_turns: int = 5,
) -> Any:
    """시나리오별 mock Anthropic AsyncAnthropic 클라이언트를 생성한다.

    클라이언트는 pre-scripted 응답 시퀀스를 반환한다.
    Tool 실행 자체(dispatch_tool_call)는 실제 코드를 사용한다.
    """
    client = MagicMock()

    if scenario == SCENARIO_SUCCESS:
        # 정상: lookup → confirm → end_turn (3 turns)
        responses = [
            _response("tool_use",
                _tool_block("tu_1", "lookup_retailer", {"ocr_name": ocr_name})),
            _response("tool_use",
                _tool_block("tu_2", "confirm_mapping", {
                    "mapping_type": "retailer",
                    "ocr_name": ocr_name,
                    "confirmed_code": "R001",
                })),
            _response("end_turn", _text_block("매핑 완료: R001")),
        ]

    elif scenario == SCENARIO_NOT_FOUND:
        # 후보 없음: lookup → end_turn (2 turns)
        responses = [
            _response("tool_use",
                _tool_block("tu_1", "lookup_retailer", {"ocr_name": ocr_name})),
            _response("end_turn", _text_block("후보가 없어 매핑 불가")),
        ]

    elif scenario == SCENARIO_TOOL_EXCEPTION:
        # dispatch 예외 발생 후 Claude가 종료 (2 turns)
        responses = [
            _response("tool_use",
                _tool_block("tu_1", "lookup_retailer", {"ocr_name": ocr_name})),
            _response("end_turn", _text_block("도구 실행 오류 발생")),
        ]

    elif scenario == SCENARIO_INVALID_TOOL:
        # allowlist 외 tool 호출 → 차단 후 종료 (2 turns)
        responses = [
            _response("tool_use",
                _tool_block("tu_1", "nonexistent_tool", {"ocr_name": ocr_name})),
            _response("end_turn", _text_block("허용되지 않은 도구")),
        ]

    elif scenario == SCENARIO_MAX_TURNS:
        # max_turns를 초과할 만큼 tool_use를 반복
        responses = [
            _response("tool_use",
                _tool_block(f"tu_{i}", "lookup_retailer", {"ocr_name": ocr_name}))
            for i in range(max_turns + 2)
        ]

    else:
        raise ValueError(f"알 수 없는 scenario: {scenario!r}")

    client.messages.create = AsyncMock(side_effect=responses)
    return client


# ── Dispatch 패치 컨텍스트 ────────────────────────────────────────────────────

@asynccontextmanager
async def _dispatch_patch_ctx(scenario: str):
    """TOOL_EXCEPTION 시나리오에서 dispatch_tool_call을 실패하도록 패치한다."""
    if scenario == SCENARIO_TOOL_EXCEPTION:
        async def _always_fail(name: str, arguments: dict):
            raise RuntimeError(f"[SCENARIO] tool '{name}' 강제 실패")
        with patch(
            "backend.experiments.phase3_tool_use_experiment.dispatch_tool_call",
            new=_always_fail,
        ):
            yield
    else:
        yield


# ── 단일 retailer 실행 래퍼 ───────────────────────────────────────────────────

async def _run_one(
    ocr_name: str,
    form_id: str,
    mappings_dir: Path,
    form_definitions_dir: Path | None,
    scenario: str,
    max_turns: int,
    allow_side_effects: bool,
    model: str = "claude-haiku-4-5-20251001",
    client: Any = None,
) -> RetailerBatchResult:
    """단일 retailer에 대해 실험을 실행하고 RetailerBatchResult를 반환한다.

    client:
      None  → _make_scenario_client(scenario, ...) 사용 (테스트 전용 mock 경로)
      non-None → 전달된 client 사용 (운영 경로 — 실제 Anthropic API 호출)
    """
    effective_client = client if client is not None else _make_scenario_client(scenario, ocr_name, max_turns)
    t0 = time.perf_counter()

    try:
        result = await run_retailer_mapping_experiment(
            ocr_name=ocr_name,
            form_id=form_id,
            mappings_dir=mappings_dir,
            form_definitions_dir=form_definitions_dir,
            max_turns=max_turns,
            allow_side_effects=allow_side_effects,
            client=effective_client,
            model=model,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        lookup_calls  = sum(1 for tc in result.tool_calls if tc.name == "lookup_retailer")
        confirm_calls = sum(1 for tc in result.tool_calls if tc.name == "confirm_mapping")

        return RetailerBatchResult(
            ocr_name=ocr_name,
            success=True,
            confirmed_code=result.confirmed_code,
            lookup_basis=result.lookup_basis,
            tool_call_count=len(result.tool_calls),
            lookup_call_count=lookup_calls,
            confirm_call_count=confirm_calls,
            turns_used=result.turns_used,
            max_turns_hit=False,
            elapsed_ms=elapsed_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            api_call_count=result.api_call_count,
        )

    except RuntimeError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        is_max_turns     = "max_turns" in str(exc)
        is_tool_not_called = "tool_not_called" in str(exc)
        return RetailerBatchResult(
            ocr_name=ocr_name,
            success=False,
            confirmed_code=None,
            lookup_basis=None,
            tool_call_count=0,
            lookup_call_count=0,
            confirm_call_count=0,
            turns_used=max_turns if is_max_turns else 1,
            max_turns_hit=is_max_turns,
            elapsed_ms=elapsed_ms,
            error=str(exc),
            tool_not_called=is_tool_not_called,
        )

    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return RetailerBatchResult(
            ocr_name=ocr_name,
            success=False,
            confirmed_code=None,
            lookup_basis=None,
            tool_call_count=0,
            lookup_call_count=0,
            confirm_call_count=0,
            turns_used=0,
            max_turns_hit=False,
            elapsed_ms=elapsed_ms,
            error=f"[UNEXPECTED] {exc}",
        )


# ── 공개 Batch 실험 함수 ──────────────────────────────────────────────────────

async def run_batch_retailer_experiment(
    ocr_names: list[str],
    mappings_dir: Path,
    form_definitions_dir: Path | None = None,
    form_id: str = "form_01",
    *,
    scenario: str = SCENARIO_SUCCESS,
    max_turns: int = 5,
    allow_side_effects: bool = True,
    reset_metrics_before: bool = True,
    model: str = "claude-haiku-4-5-20251001",
    concurrency: int = 1,
    client: Any = None,
) -> BatchExperimentResult:
    """N건의 retailer에 대해 tool_use 실험을 실행하고 집계 결과를 반환한다.

    client:
      None     → 각 _run_one에서 _make_scenario_client(scenario, ...) 사용 (테스트 전용)
      non-None → 모든 _run_one에 동일 client 전달 (운영 경로 — 실제 Claude API)
    """
    """N건의 retailer에 대해 tool_use 실험을 순차 실행하고 집계 결과를 반환한다.

    Args:
        ocr_names:            OCR 거래처명 목록 (N건)
        mappings_dir:         mappings/ 디렉토리 경로
        form_definitions_dir: form_definitions/ 경로
        form_id:              양식 ID
        scenario:             시나리오 (SCENARIO_* 상수)
        max_turns:            단건 실험의 최대 turn 수
        allow_side_effects:   True = confirm_mapping 허용
        reset_metrics_before: True = 실험 전 metrics 초기화

    Returns:
        BatchExperimentResult (per_retailer 결과 + 집계 stats)
    """
    if reset_metrics_before:
        reset_metrics()

    batch_size = len(ocr_names)
    per_retailer: list[RetailerBatchResult] = []
    total_start = time.perf_counter()

    _sem = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(ocr_name: str) -> RetailerBatchResult:
        async with _sem:
            return await _run_one(
                ocr_name=ocr_name,
                form_id=form_id,
                mappings_dir=mappings_dir,
                form_definitions_dir=form_definitions_dir,
                scenario=scenario,
                max_turns=max_turns,
                allow_side_effects=allow_side_effects,
                model=model,
                client=client,
            )

    async with _dispatch_patch_ctx(scenario):
        # _run_one는 내부에서 모든 예외를 처리하므로 return_exceptions 불필요
        per_retailer = list(await asyncio.gather(*[
            _bounded(name) for name in ocr_names
        ]))

    total_elapsed_ms = (time.perf_counter() - total_start) * 1000
    metrics_snap = get_metrics()  # 실험 후 전체 snapshot

    # ── 집계 ─────────────────────────────────────────────────────────────────
    success_count        = sum(1 for r in per_retailer if r.success)
    failure_count        = sum(1 for r in per_retailer if not r.success)
    max_turns_count      = sum(1 for r in per_retailer if r.max_turns_hit)
    not_found_count      = sum(1 for r in per_retailer if r.lookup_basis == "not_found")
    tool_not_called_count = sum(1 for r in per_retailer if r.tool_not_called)
    total_tool_calls  = sum(r.tool_call_count   for r in per_retailer)
    total_lookup      = sum(r.lookup_call_count  for r in per_retailer)
    total_confirm     = sum(r.confirm_call_count for r in per_retailer)
    total_turns       = sum(r.turns_used         for r in per_retailer if r.success)
    avg_turns = total_turns / success_count if success_count else 0.0

    stats = BatchStats(
        batch_size=batch_size,
        success_count=success_count,
        failure_count=failure_count,
        max_turns_hit_count=max_turns_count,
        not_found_count=not_found_count,
        tool_not_called_count=tool_not_called_count,
        total_tool_calls=total_tool_calls,
        total_lookup_calls=total_lookup,
        total_confirm_calls=total_confirm,
        total_turns=total_turns,
        avg_turns=round(avg_turns, 2),
        elapsed_ms=round(total_elapsed_ms, 1),
        metrics_snapshot=metrics_snap,
        total_input_tokens=sum(r.input_tokens  for r in per_retailer),
        total_output_tokens=sum(r.output_tokens for r in per_retailer),
        total_api_calls=sum(r.api_call_count    for r in per_retailer),
    )

    return BatchExperimentResult(
        scenario=scenario,
        batch_size=batch_size,
        stats=stats,
        per_retailer=per_retailer,
    )


# ── 보고서 포매터 ─────────────────────────────────────────────────────────────

def format_batch_report(results: list[BatchExperimentResult]) -> str:
    """batch 실험 결과를 텍스트 표 형식으로 반환한다."""
    lines = [
        "┌─────────────────────────────────────────────────────────────────────────────┐",
        "│ Batch Retailer Tool Use Experiment Report                                   │",
        "└─────────────────────────────────────────────────────────────────────────────┘",
        "",
        f"{'SIZE':>6} {'SCENARIO':<18} {'OK':>4} {'FAIL':>5} {'MAX_T':>5} "
        f"{'LOOKUP':>7} {'CONFIRM':>8} {'AVG_T':>6} {'ELAPSED':>9}",
        "─" * 80,
    ]
    for r in results:
        s = r.stats
        lines.append(
            f"{s.batch_size:>6} {r.scenario:<18} {s.success_count:>4} {s.failure_count:>5} "
            f"{s.max_turns_hit_count:>5} {s.total_lookup_calls:>7} {s.total_confirm_calls:>8} "
            f"{s.avg_turns:>6.1f} {s.elapsed_ms:>8.1f}ms"
        )

    lines += [
        "─" * 80,
        "",
        "Metrics Snapshot (lookup_retailer):",
    ]
    if results:
        lr_m = results[-1].stats.metrics_snapshot.get("lookup_retailer")
        if lr_m:
            lines.append(
                f"  calls={lr_m.calls}  success={lr_m.success}  "
                f"cache_hits={lr_m.cache_hits}  not_found={lr_m.not_found}  "
                f"failures={lr_m.failures}"
            )
    return "\n".join(lines)
