"""phase3_fallback.py — Tool Use Fallback 래퍼

## 개요

Claude Tool Use 경로가 실패해도 기존 CSV 주입 방식(run_phase3)으로
결과를 낼 수 있도록 보장하는 fallback 계층.

## Tool Use Success Path (현재)

enable_tool_use=True이고 Tool Use가 성공하면:
  1. _attempt_tool_use_phase() → BatchExperimentResult 반환
     └─ retailer token usage를 partial_token_stats로 빌드 (validation 실패 시 exception에 첨부)
  2. _execute_success_path():
       a. batch_result.stats에서 retailer token을 _token_acc에 누적
       b. BatchExperimentResult → RetailerMappingDecision 변환
       c. dist_code resolver 적용 (파일 I/O 없음 — pre-load된 캐시 사용)
       d. search_product 캐시 조회로 ProductMappingDecision 생성
          └─ product token usage를 _token_acc에 누적
       e. convert_tool_use_result_to_phase3_output() 호출
       f. phase3_output.json 저장 (실패 시 ToolUseDispatchError → fallback)
       g. confirm_mapping 호출 (저장 성공 후 — fallback 시 미호출)
  3. _record_tool_use_token_usage() → DB 기록 (성공/fallback 모두)

## Side-effect 안전성

Tool Use 경로에서는 _attempt_tool_use_phase() 내에서 confirm_mapping을 호출하지 않는다.
(allow_side_effects=False)

성공 시: _execute_success_path()가 phase3_output.json 저장 후 confirm_mapping 1회 호출
실패 시: legacy run_phase3()가 정상 경로로 1회 저장

→ 어떤 경우에도 confirm_mapping 중복 호출 없음.

## Token Usage 보존 정책

retailer token:
  - 성공 시: _execute_success_path()에서 batch_result.stats → _token_acc에 누적
  - fallback 시: _attempt_tool_use_phase()에서 exception.partial_token_stats에 첨부
                 → run_phase3_with_tool_use_or_fallback()에서 stats.token_usage에 복사
  - API response 자체가 없는 경우(ToolUseApiError, partial_token_stats=None) → usage=0

product token:
  - _run_single_product_mapping()에서 매 호출마다 _token_acc에 즉시 누적
  - dispatch error, JSON parse error 등 이후에도 누적된 값 보존됨

## Fallback 발생 조건

  ToolUseMaxTurnsError      — max_turns 초과
  ToolUseDispatchError      — tool dispatch 오류 (N회 이상) 또는 저장 실패
  ToolUseApiError           — Claude API retry 최종 실패
  ToolUseParseError         — tool_result JSON 파싱 실패
  ToolUseContractError      — Tool Use 결과 contract 위반

## Fallback 비발생 조건

  lookup_retailer not_found — 정상 결과, fallback 아님
  정상 후보 없음            — 정상 결과, fallback 아님
  Claude가 매핑 불가 판단   — 정상 결과, fallback 아님
  dist 1:N                 — 정상 결과 (pending 생성), fallback 아님
"""
import asyncio
import inspect
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── 전역 세마포어: 문서 간 동시 Tool Use Claude API 호출 수 제한 ───────────────
# 여러 사용자가 동시에 문서를 올릴 때 API Rate Limit 방지.
# 동일 이벤트 루프 내에서 lazy 초기화되며, 설정값이 바뀌면 재생성된다.
# 개별 문서 내부 concurrency(_concurrency)와 독립적으로 동작한다.
_GLOBAL_TOOL_USE_SEM: asyncio.Semaphore | None = None
_GLOBAL_TOOL_USE_SEM_CAPACITY: int = 0


def _get_global_tool_use_sem(capacity: int) -> asyncio.Semaphore:
    """앱 전체 Tool Use 동시 문서 처리 수를 제한하는 세마포어를 반환한다.

    capacity가 변경됐을 때만 새 세마포어를 생성한다.
    Python 3.11에서 asyncio.Semaphore는 event loop에 바인딩되지 않으므로
    lazy 초기화가 안전하다.
    """
    global _GLOBAL_TOOL_USE_SEM, _GLOBAL_TOOL_USE_SEM_CAPACITY
    effective = max(1, capacity)
    if _GLOBAL_TOOL_USE_SEM is None or _GLOBAL_TOOL_USE_SEM_CAPACITY != effective:
        _GLOBAL_TOOL_USE_SEM = asyncio.Semaphore(effective)
        _GLOBAL_TOOL_USE_SEM_CAPACITY = effective
    return _GLOBAL_TOOL_USE_SEM

from ..core.config import get_settings
from ..experiments.batch_tool_use_experiment import (
    SCENARIO_SUCCESS,
    run_batch_retailer_experiment,
)
from ..tools.claude_adapter import build_claude_tools, coerce_tool_arguments
from ..tools.claude_retry import async_call_with_retry
from ..tools.mapping import _read_csv, confirm_mapping, search_product
from ..tools.registry import get_tool
from .phase3 import _build_issuer_fingerprint, _parse_fingerprint_fields, run_phase3
from .phase3_dist_resolver import DistResolution, build_dist_resolution_from_cache
from .phase3_tool_result_adapter import (
    ProductMappingDecision,
    RetailerMappingDecision,
    convert_tool_use_result_to_phase3_output,
)

log = logging.getLogger(__name__)

__all__ = [
    # 예외
    "ToolUseFallbackTrigger",
    "ToolUseMaxTurnsError",
    "ToolUseContractError",
    "ToolUseDispatchError",
    "ToolUseApiError",
    "ToolUseParseError",
    # 데이터 타입
    "Phase3FallbackStats",
    "ToolUseTokenStats",
    # 공개 함수
    "run_phase3_with_tool_use_or_fallback",
    "_record_tool_use_token_usage",  # 테스트 mock용 노출
    "dispatch_tool_call",            # 테스트 mock용 노출
]


# ── Fallback 예외 계층 ────────────────────────────────────────────────────────

class ToolUseFallbackTrigger(Exception):
    """Tool Use 실패로 legacy fallback이 필요함을 알리는 기본 예외.

    partial_token_stats: Claude response를 받은 뒤 발생한 실패 시
      이미 사용된 token usage를 첨부한다. fallback handler에서 stats에 복사.
      API response 자체를 못 받은 경우(ToolUseApiError) 등은 None 그대로 둔다.
    """
    def __init__(self, reason: str, *, partial_token_stats: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.partial_token_stats = partial_token_stats  # ToolUseTokenStats | None


class ToolUseMaxTurnsError(ToolUseFallbackTrigger):
    """max_turns 초과 — 루프가 end_turn 없이 종료됨."""


class ToolUseContractError(ToolUseFallbackTrigger):
    """Tool Use 결과가 expected contract를 위반함."""



class ToolUseDispatchError(ToolUseFallbackTrigger):
    """tool dispatch 실행 중 예외 발생, 또는 phase3_output.json 저장 실패."""


class ToolUseApiError(ToolUseFallbackTrigger):
    """Claude API retry 최종 실패."""


class ToolUseParseError(ToolUseFallbackTrigger):
    """tool_result JSON 직렬화/역직렬화 실패."""


# ── Tool Use 공용 상수 ─────────────────────────────────────────────────────────

# Tool Use 경로에서 사용하는 Claude 모델명. DB 기록 및 로그에 공통 사용.
_TOOL_USE_MODEL = "claude-haiku-4-5-20251001"


# ── Token Usage 수집 타입 ─────────────────────────────────────────────────────

@dataclass
class ToolUseTokenStats:
    """Tool Use 경로의 Claude API token 사용량.

    retailer: _attempt_tool_use_phase()에서 즉시 누적 (fallback 발생 전에 보존됨)
    product:  _run_single_product_mapping()에서 매 호출마다 누적
    dist:     _run_single_dist_mapping()에서 매 호출마다 누적 (dist 1:N Claude 결정)

    Fallback 보존 정책:
      - retailer token은 validation 검사 전에 누적 → max_turns, contract error 시에도 보존
      - product/dist token은 루프 내 매 호출마다 즉시 누적
      - API response 자체가 없는 경우(ToolUseApiError)만 usage=0 허용
    """
    retailer_input_tokens:          int = 0
    retailer_output_tokens:         int = 0
    retailer_cache_read_tokens:     int = 0
    retailer_cache_creation_tokens: int = 0
    retailer_api_calls:             int = 0
    product_input_tokens:           int = 0
    product_output_tokens:          int = 0
    product_cache_read_tokens:      int = 0
    product_cache_creation_tokens:  int = 0
    product_api_calls:              int = 0
    dist_input_tokens:              int = 0
    dist_output_tokens:             int = 0
    dist_cache_read_tokens:         int = 0
    dist_cache_creation_tokens:     int = 0
    dist_api_calls:                 int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.retailer_input_tokens + self.product_input_tokens + self.dist_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self.retailer_output_tokens + self.product_output_tokens + self.dist_output_tokens

    @property
    def total_tokens(self) -> int:
        """input + output 합계."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_api_calls(self) -> int:
        return self.retailer_api_calls + self.product_api_calls + self.dist_api_calls

    @property
    def call_count(self) -> int:
        """total_api_calls alias (schema 통일용)."""
        return self.total_api_calls

    @property
    def tool_use_call_count(self) -> int:
        """tool_use block이 발생한 API 호출 수."""
        return self.total_api_calls

    @property
    def total_cache_read_tokens(self) -> int:
        return self.retailer_cache_read_tokens + self.product_cache_read_tokens + self.dist_cache_read_tokens

    @property
    def total_cache_creation_tokens(self) -> int:
        return (self.retailer_cache_creation_tokens + self.product_cache_creation_tokens
                + self.dist_cache_creation_tokens)


# ── DB 기록 — module-level (테스트에서 patch 가능) ───────────────────────────

async def _record_tool_use_token_usage(
    doc_id: str,
    run_id: str,
    token_stats: ToolUseTokenStats,
    *,
    model: str = _TOOL_USE_MODEL,
) -> None:
    """Tool Use token usage를 DB에 기록한다.

    success path와 fallback path 모두에서 호출된다.
    usage가 없으면(total_api_calls=0) 기록 없이 반환한다.
    DB 오류는 warning만 남기고 pipeline 흐름을 유지한다.

    module-level 함수이므로 테스트에서
    `patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage")`로 mock 가능.
    """
    if token_stats.total_api_calls <= 0:
        return
    try:
        from ..db.queries import accumulate_token_usage
        await accumulate_token_usage(
            doc_id, "phase3_tool_use",
            token_stats.total_input_tokens,
            token_stats.total_output_tokens,
            model,
            run_id=run_id,
        )
    except Exception as _e:
        log.warning("[%s] Tool Use token usage DB 기록 실패 (무시): %s", doc_id, _e)


# ── Observability 타입 ────────────────────────────────────────────────────────

@dataclass
class Phase3FallbackStats:
    """Phase 3 fallback 실행 통계."""
    enable_tool_use: bool
    used_tool_use: bool
    fallback_triggered: bool
    fallback_reason: str | None
    fallback_class: str | None
    tool_use_elapsed_ms: float
    legacy_elapsed_ms: float
    total_elapsed_ms: float
    max_turns_hit: bool
    api_retry_failed: bool
    batch_size: int
    batch_failure_count: int
    extra: dict = field(default_factory=dict)
    token_usage: ToolUseTokenStats = field(default_factory=ToolUseTokenStats)
    # ^ success/fallback 모두에서 보존되는 Tool Use token 사용량


# ── Tool Use 시도 경로 ────────────────────────────────────────────────────────

async def _attempt_tool_use_phase(
    phase2_result: dict,
    mappings_dir: Path,
    form_definitions_dir: Path | None,
    form_id: str,
    max_turns: int,
    stats: Phase3FallbackStats,
    model: str = _TOOL_USE_MODEL,
    concurrency: int = 1,
    anthropic_api_key: str = "",
) -> Any:  # BatchExperimentResult | None
    """Tool Use 경로로 retailer 매핑을 시도한다.

    운영 경로:
      anthropic_api_key 있음 → 실제 Anthropic AsyncAnthropic client 생성 후 전달
      anthropic_api_key 없음 → warning 후 batch_result=None (retailer 전량 pending)

    테스트 경로:
      run_batch_retailer_experiment를 mock patch하거나,
      run_batch_retailer_experiment의 client=None 경로(scenario mock)를 사용.
    """
    unique_retailers = list({
        i["customer"] for i in phase2_result.get("items", []) if i.get("customer")
    })
    stats.batch_size = len(unique_retailers)

    if not unique_retailers:
        log.info("[phase3_fallback] retailer 없음 — Tool Use 생략")
        return None

    # ── 운영 경로: 실제 Anthropic client 생성 ────────────────────────────────
    import anthropic as _anthropic
    _retailer_client: Any = None
    if anthropic_api_key:
        try:
            _retailer_client = _anthropic.AsyncAnthropic(api_key=anthropic_api_key)
        except Exception as exc:
            raise ToolUseApiError(
                f"Anthropic client 생성 실패: {type(exc).__name__}: {exc}"
            ) from exc
    else:
        log.warning(
            "[phase3_fallback] ANTHROPIC_API_KEY 없음 — retailer Tool Use 불가, 전량 pending 처리"
        )
        return None  # retailers go to pending in _execute_success_path

    try:
        batch_result = await run_batch_retailer_experiment(
            ocr_names=unique_retailers,
            mappings_dir=mappings_dir,
            form_definitions_dir=form_definitions_dir,
            form_id=form_id,
            scenario=SCENARIO_SUCCESS,
            max_turns=max_turns,
            allow_side_effects=False,
            reset_metrics_before=False,
            model=model,
            concurrency=concurrency,
            client=_retailer_client,   # 실제 Claude client
        )

    except Exception as exc:
        import anthropic
        if isinstance(exc, anthropic.APIError):
            stats.api_retry_failed = True
            raise ToolUseApiError(
                f"Claude API 최종 실패: {type(exc).__name__}: {exc}"
            ) from exc
        raise ToolUseDispatchError(f"Tool Use batch 실행 오류: {exc}") from exc

    s = batch_result.stats
    stats.batch_failure_count = s.failure_count

    # ── retailer partial token stats (validation 전 — fallback 시 exception에 첨부) ──
    # 성공 시는 _execute_success_path()에서 batch_result.stats → _token_acc에 누적.
    # 실패(fallback) 시는 exception에 첨부 → run_phase3_with_tool_use_or_fallback에서 복사.
    _partial = ToolUseTokenStats(
        retailer_input_tokens=          getattr(s, "total_input_tokens",          0) or 0,
        retailer_output_tokens=         getattr(s, "total_output_tokens",         0) or 0,
        retailer_cache_read_tokens=     getattr(s, "total_cache_read_tokens",     0) or 0,
        retailer_cache_creation_tokens= getattr(s, "total_cache_creation_tokens", 0) or 0,
        retailer_api_calls=             getattr(s, "total_api_calls",             0) or 0,
    )

    # ── 검증 1: max_turns ───────────────────────────────────────────────────
    if s.max_turns_hit_count > 0:
        stats.max_turns_hit = True
        raise ToolUseMaxTurnsError(
            f"max_turns({max_turns}) 초과 — {s.max_turns_hit_count}/{s.batch_size}건",
            partial_token_stats=_partial,
        )

    # ── 검증 1b: tool 미호출 (lookup_retailer 없이 end_turn) ─────────────
    # tool_choice 강제에도 불구하고 Claude가 tool 없이 종료한 경우.
    # 결과를 신뢰할 수 없으므로 legacy fallback으로 처리한다.
    tool_not_called = getattr(s, "tool_not_called_count", 0) or 0
    if tool_not_called > 0:
        raise ToolUseContractError(
            f"tool 미호출 {tool_not_called}/{s.batch_size}건 — "
            f"Claude가 lookup_retailer를 호출하지 않고 end_turn 반환",
            partial_token_stats=_partial,
        )

    # ── 검증 2: dispatch/tool 실패 ────────────────────────────────────────
    if s.failure_count > 0:
        raise ToolUseDispatchError(
            f"Tool Use 실패 {s.failure_count}/{s.batch_size}건",
            partial_token_stats=_partial,
        )

    # ── 검증 3: contract ──────────────────────────────────────────────────
    for r in batch_result.per_retailer:
        if not r.success:
            raise ToolUseContractError(
                f"'{r.ocr_name}': success=False (error={r.error!r})",
                partial_token_stats=_partial,
            )

    log.info(
        "[phase3_fallback] Tool Use 검증 완료 — %d건, avg_turns=%.1f",
        s.batch_size, s.avg_turns,
    )
    stats.extra["tool_use_batch_stats"] = {
        "avg_turns": s.avg_turns,
        "total_lookup": s.total_lookup_calls,
        "total_confirm": s.total_confirm_calls,
        "not_found": s.not_found_count,
    }
    return batch_result


# ── Tool dispatch helper (테스트에서 patch 가능) ─────────────────────────────

async def dispatch_tool_call(spec: Any, args: dict) -> Any:
    """tool spec.callable을 실행한다.

    module-level 함수이므로 테스트에서
    `patch("backend.pipeline.phase3_fallback.dispatch_tool_call")`로 mock 가능.
    """
    return await spec.callable(**args)


# ── Product Tool Use ──────────────────────────────────────────────────────────

_PRODUCT_ALLOWED_TOOLS = frozenset({"search_product"})
_PRODUCT_PATH_FIELDS   = frozenset({"mappings_dir"})


def _build_product_tools() -> list[dict]:
    """product 매핑용 Claude tool schema — search_product만 포함."""
    result = []
    for tool in build_claude_tools():
        if tool["name"] != "search_product":
            continue
        schema   = tool["input_schema"]
        required = [r for r in schema.get("required", []) if r not in _PRODUCT_PATH_FIELDS]
        props    = {k: v for k, v in schema.get("properties", {}).items()
                    if k not in _PRODUCT_PATH_FIELDS}
        result.append({
            "name":         tool["name"],
            "description":  tool["description"],
            "input_schema": {"type": "object", "required": required, "properties": props},
        })
    return result


def _parse_product_decision_json(text: str) -> dict:
    """Claude의 product 결정 final text에서 JSON을 추출·파싱한다."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    text = text.strip()
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    if not text:
        raise ToolUseParseError("product 결정 JSON 없음 — 빈 응답")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolUseParseError(
            f"product 결정 JSON 파싱 실패: {e}\nraw: {text[:200]}"
        ) from e
    if "decision" not in data:
        raise ToolUseParseError(
            f"product 결정 JSON에 'decision' 필드 없음: {list(data.keys())}"
        )
    return data


def _build_product_decision_from_json(
    ocr_name: str,
    data: dict,
    valid_codes: set[str],
    candidates: list | None = None,
) -> ProductMappingDecision:
    """파싱된 JSON에서 ProductMappingDecision을 생성한다."""
    decision = data.get("decision", "not_found")
    if decision != "confirmed":
        return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                      product_name="", basis="not_found", confidence=0.0)

    product_code = (data.get("product_code") or "").strip()
    master_name  = (data.get("master_name")  or "").strip()
    confidence   = float(data.get("confidence", 1.0))

    if not product_code:
        log.warning("[product_tool_use] '%s': product_code 비어 있음 → pending", ocr_name)
        return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                      product_name="", basis="not_found", confidence=0.0)

    if valid_codes and product_code not in valid_codes:
        log.warning("[product_tool_use] '%s': product_code '%s' 후보 밖 → pending",
                    ocr_name, product_code)
        return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                      product_name="", basis="not_found", confidence=0.0)

    if not master_name:
        for c in (candidates or []):
            if isinstance(c, dict) and c.get("product_code") == product_code:
                name = (c.get("product_name") or "").strip()
                if name:
                    log.info("[product_tool_use] '%s': master_name 보완 '%s'", ocr_name, name)
                    master_name = name
                    break
        if not master_name:
            log.warning("[product_tool_use] '%s': master_name 없음 → pending", ocr_name)
            return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                          product_name="", basis="not_found", confidence=0.0)

    return ProductMappingDecision(ocr_name=ocr_name, product_code=product_code,
                                  product_name=master_name, basis="tool_use",
                                  confidence=confidence)


async def _run_single_product_mapping(
    ocr_name: str,
    candidates: list,
    mappings_dir: Path,
    *,
    client: Any,
    max_turns: int = 3,
    model: str = _TOOL_USE_MODEL,
    _token_acc: ToolUseTokenStats | None = None,
) -> ProductMappingDecision:
    """단일 product에 대해 Claude Tool Use 루프를 실행한다.

    Token 누적: 매 async_call_with_retry 호출 후 즉시 _token_acc에 누적.
    dispatch/parse error 이후에도 누적된 값 보존됨.
    """
    product_tools = _build_product_tools()
    ctx           = {"mappings_dir": mappings_dir}
    valid_codes: set[str] = {
        c["product_code"] for c in candidates
        if isinstance(c, dict) and c.get("product_code")
    }
    messages: list[dict] = [{
        "role": "user",
        "content": (
            f"제품명 '{ocr_name}'의 제품코드를 찾아라.\n\n"
            f"처리 순서:\n"
            f"1. search_product 도구로 후보를 조회한다.\n"
            f"2. 조회 완료 후 최종 응답을 출력한다.\n\n"
            f"최종 응답 형식 (순수 JSON 객체만 — 설명/markdown/code fence 금지):\n\n"
            f"후보가 있으면:\n"
            f'{{"decision": "confirmed", "product_code": "<코드>", "master_name": "<이름>"}}\n\n'
            f"후보가 없으면:\n"
            f'{{"decision": "not_found", "reason": "<이유>"}}\n\n'
            f"주의: product_code는 search_product 후보 중 하나여야 하며, master_name은 빈 값 금지."
        ),
    }]

    for turn in range(max_turns):
        response = await async_call_with_retry(
            client.messages.create,
            model=model,
            max_tokens=256,
            temperature=0,
            tools=product_tools,
            messages=messages,
        )

        # product token usage 수집 (매 호출 후 즉시 — fallback/parse error 이후에도 보존)
        if _token_acc is not None:
            _u = getattr(response, "usage", None)
            if _u is not None:
                _token_acc.product_input_tokens          += getattr(_u, "input_tokens",  0) or 0
                _token_acc.product_output_tokens         += getattr(_u, "output_tokens", 0) or 0
                _token_acc.product_cache_read_tokens     += getattr(_u, "cache_read_input_tokens",    0) or 0
                _token_acc.product_cache_creation_tokens += getattr(_u, "cache_creation_input_tokens", 0) or 0
                _token_acc.product_api_calls             += 1

        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content
                 if hasattr(b, "type") and b.type == "text"),
                "",
            )
            data = _parse_product_decision_json(final_text)
            return _build_product_decision_from_json(ocr_name, data, valid_codes, candidates)

        if response.stop_reason != "tool_use":
            log.warning("[product_tool_use] '%s': stop_reason=%s → pending",
                        ocr_name, response.stop_reason)
            break

        tool_results: list[dict] = []
        for block in response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            name        = block.name
            claude_args = dict(block.input) if block.input else {}
            if name not in _PRODUCT_ALLOWED_TOOLS:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "is_error": True,
                    "content": f"허용되지 않은 tool: {name!r}. 허용: search_product",
                })
                continue
            try:
                spec         = get_tool(name)
                valid_params = set(inspect.signature(spec.callable).parameters.keys())
                full_args    = {k: v for k, v in ctx.items() if k in valid_params}
                full_args.update(claude_args)
                coerced      = coerce_tool_arguments(spec, full_args)
                sr           = await dispatch_tool_call(spec, coerced)
                for c in getattr(sr, "candidates", []):
                    if isinstance(c, dict) and c.get("product_code"):
                        valid_codes.add(c["product_code"])
                content = json.dumps(asdict(sr), ensure_ascii=False, default=str)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id, "content": content,
                })
            except Exception as exc:
                raise ToolUseDispatchError(
                    f"search_product 실행 오류 ('{ocr_name}'): {exc}"
                ) from exc

        messages.append({"role": "assistant", "content": list(response.content)})
        messages.append({"role": "user",      "content": tool_results})

    log.warning("[product_tool_use] '%s': end_turn 없이 루프 종료 → pending", ocr_name)
    return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                  product_name="", basis="not_found", confidence=0.0)


async def _build_product_decisions_with_tool_use(
    unique_products: list[str],
    mappings_dir: Path,
    *,
    product_client: Any = None,
    max_product_turns: int = 3,
    model: str = _TOOL_USE_MODEL,
    concurrency: int = 1,
    _token_acc: ToolUseTokenStats | None = None,
) -> list[ProductMappingDecision]:
    """search_product 캐시 + Tool Use로 ProductMappingDecision 목록 생성.

    concurrency=1(기본)이면 순차 실행과 동일.
    concurrency>1이면 Claude 호출을 semaphore로 제한하여 병렬 실행.
    결과 순서는 unique_products 입력 순서를 보장.

    Token 누적 정책:
      각 task가 로컬 ToolUseTokenStats에 누적 → task 완료/실패 시 공유 _token_acc에 병합.
      asyncio 단일 스레드 보장으로 merge 시 race condition 없음.
      fallback 발생 시에도 이미 완료된 호출의 token이 보존됨.
    """
    _sem = asyncio.Semaphore(max(1, concurrency))

    async def _process_one(ocr_name: str) -> ProductMappingDecision:
        """단일 product 처리. token은 local_acc에 누적 후 _token_acc에 병합."""
        local_acc = ToolUseTokenStats()

        def _merge():
            if _token_acc is not None:
                _token_acc.product_input_tokens          += local_acc.product_input_tokens
                _token_acc.product_output_tokens         += local_acc.product_output_tokens
                _token_acc.product_cache_read_tokens     += local_acc.product_cache_read_tokens
                _token_acc.product_cache_creation_tokens += local_acc.product_cache_creation_tokens
                _token_acc.product_api_calls             += local_acc.product_api_calls

        try:
            sp = await search_product(ocr_name=ocr_name, mappings_dir=mappings_dir)
        except Exception as exc:
            _merge()
            raise ToolUseDispatchError(
                f"search_product 실행 오류 ('{ocr_name}'): {exc}"
            ) from exc

        if sp.basis == "cache":
            _merge()
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code=sp.product_code,
                product_name="", basis="cache", confidence=1.0,
            )

        if sp.basis == "candidate" and product_client is not None:
            try:
                async with _sem:
                    decision = await _run_single_product_mapping(
                        ocr_name, list(sp.candidates), mappings_dir,
                        client=product_client, max_turns=max_product_turns,
                        model=model,
                        _token_acc=local_acc,
                    )
            except ToolUseFallbackTrigger:
                _merge()
                raise
            except Exception as exc:
                _merge()
                import anthropic as _anthropic
                if isinstance(exc, _anthropic.APIError):
                    raise ToolUseApiError(
                        f"Product Tool Use API 실패 ('{ocr_name}'): {exc}"
                    ) from exc
                raise ToolUseDispatchError(
                    f"Product Tool Use 실행 오류 ('{ocr_name}'): {exc}"
                ) from exc
            _merge()
            return decision

        if sp.basis == "candidate" and product_client is None:
            _reason = "product_client 없음 (API 키 미설정 또는 Tool Use 비활성화)"
            log.warning("[product_tool_use] '%s': 후보 있음이지만 client 없음 → pending (%s)",
                        ocr_name, _reason)
            _merge()
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code=None, product_name="",
                basis="not_found", confidence=0.0, error=_reason,
            )

        _merge()
        return ProductMappingDecision(
            ocr_name=ocr_name, product_code=None, product_name="",
            basis="not_found", confidence=0.0,
        )

    # 입력 순서 보장: asyncio.gather는 입력 순서대로 결과 반환
    raw = await asyncio.gather(
        *[_process_one(name) for name in unique_products],
        return_exceptions=True,
    )

    decisions: list[ProductMappingDecision] = []
    first_error: BaseException | None = None

    for item in raw:
        if isinstance(item, BaseException):
            if first_error is None:
                first_error = item
        else:
            decisions.append(item)  # type: ignore[arg-type]

    if first_error is not None:
        # fallback 트리거: 종류에 따라 이미 올바른 타입이거나 ToolUseDispatchError로 래핑됨
        raise first_error

    return decisions


def _batch_result_to_retailer_decisions(
    per_retailer: list,
    *,
    form_id: str,
    issuer_fingerprint: str,
    cached_dist: dict,
    retail_user_rows: list[dict],
) -> tuple[list[RetailerMappingDecision], dict[str, DistResolution], list[dict]]:
    """RetailerBatchResult 목록을 RetailerMappingDecision 목록으로 변환한다.

    파일 I/O 없음 — pre-load된 cached_dist / retail_user_rows를 사용.
    """
    decisions: list[RetailerMappingDecision] = []
    dist_resolutions: dict[str, DistResolution] = {}
    dist_pending: list[dict] = []

    for r in per_retailer:
        if not r.success or r.confirmed_code is None:
            basis = "not_found" if r.lookup_basis == "not_found" else "error"
            decisions.append(RetailerMappingDecision(
                ocr_name=r.ocr_name, retailer_code=None, dist_code="",
                basis=basis, confidence=0.0,
            ))
            continue

        lb = r.lookup_basis or ""
        basis = lb if lb in {"cache", "bracket_code"} else "tool_use"

        retailer_code = r.confirmed_code
        dist_res = build_dist_resolution_from_cache(
            retailer_code, cached_dist, retail_user_rows,
            form_id=form_id, issuer_fingerprint=issuer_fingerprint,
        )
        dist_resolutions[r.ocr_name] = dist_res

        if dist_res.needs_confirmation:
            dist_pending.append({
                "mapping_type": "dist", "ocrName": r.ocr_name,
                "retailer_code": retailer_code,   # dist 1:N Tool Use에서 컨텍스트로 사용
                "candidates": dist_res.candidates, "page_number": None,
            })
            dist_code = ""
        else:
            dist_code = dist_res.dist_code or ""

        decisions.append(RetailerMappingDecision(
            ocr_name=r.ocr_name, retailer_code=retailer_code,
            dist_code=dist_code, basis=basis, confidence=1.0,
        ))

    return decisions, dist_resolutions, dist_pending


# ── Dist 1:N Tool Use ─────────────────────────────────────────────────────────

_DIST_ALLOWED_CHOICE = frozenset({"confirmed", "pending"})


def _parse_dist_decision_json(text: str) -> dict:
    """Claude의 dist 결정 final text에서 JSON을 추출·파싱한다."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    text = text.strip()
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    if not text:
        raise ToolUseParseError("dist 결정 JSON 없음 — 빈 응답")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolUseParseError(
            f"dist 결정 JSON 파싱 실패: {e}\nraw: {text[:200]}"
        ) from e
    if "decision" not in data:
        raise ToolUseParseError(
            f"dist 결정 JSON에 'decision' 필드 없음: {list(data.keys())}"
        )
    return data


async def _run_single_dist_mapping(
    ocr_name: str,
    retailer_code: str,
    candidates: list[dict],
    *,
    form_id: str,
    issuer_fingerprint: str,
    retailer_name: str = "",
    client: Any,
    model: str = _TOOL_USE_MODEL,
    _token_acc: ToolUseTokenStats | None = None,
) -> "DistResolution":
    """1:N dist 후보에 대해 Claude가 판매처코드를 결정한다. (단일 API 호출)

    candidates = [{"dist_code": str, "dist_name": str}]  (2개 이상)

    반환: DistResolution
      basis="tool_use"           — Claude가 후보 내 dist_code 선택
      basis="needs_confirmation" — Claude가 결정 불가 판단 (pending 유지)
    """
    from .phase3_dist_resolver import DistResolution

    valid_codes = {c["dist_code"] for c in candidates if c.get("dist_code")}

    # 후보 목록 문자열 생성
    candidates_str = "\n".join(
        f"  {i+1}. dist_code={c.get('dist_code','')}  dist_name={c.get('dist_name','')}"
        for i, c in enumerate(candidates)
    )

    prompt = (
        f"다음 소매처의 판매처(販売先)를 후보 목록에서 선택해라.\n\n"
        f"소매처명: {ocr_name}\n"
        f"소매처코드: {retailer_code}\n"
        f"양식 ID: {form_id}\n"
        f"발행처: {issuer_fingerprint or ''}\n\n"
        f"판매처 후보 ({len(candidates)}건):\n{candidates_str}\n\n"
        f"처리 기준:\n"
        f"1. 소매처명·코드·발행처 정보를 기반으로 가장 적합한 판매처를 선택한다.\n"
        f"2. 확신이 없거나 구분이 불가능하면 \"pending\"을 선택한다.\n"
        f"3. 최종 응답은 아래 JSON만 출력한다. 설명·markdown·code fence 금지.\n\n"
        f"선택 케이스:\n"
        f'{{"decision": "confirmed", "dist_code": "<후보_코드>", "reason": "<한 줄 이유>"}}\n\n'
        f"미확정 케이스:\n"
        f'{{"decision": "pending", "reason": "<판단 불가 이유>"}}\n\n'
        f"주의: dist_code는 위 후보 목록에 있는 코드만 선택 가능. 회계 계산 금지."
    )

    messages: list[dict] = [{"role": "user", "content": prompt}]

    try:
        response = await async_call_with_retry(
            client.messages.create,
            model=model,
            max_tokens=256,
            temperature=0,
            messages=messages,
        )
    except Exception as exc:
        import anthropic as _anthropic
        if isinstance(exc, _anthropic.APIError):
            raise ToolUseApiError(
                f"Dist 판매처 결정 API 실패 ('{ocr_name}'): {exc}"
            ) from exc
        raise ToolUseDispatchError(
            f"Dist 판매처 결정 실행 오류 ('{ocr_name}'): {exc}"
        ) from exc

    # token 수집 (즉시 누적)
    if _token_acc is not None:
        _u = getattr(response, "usage", None)
        if _u is not None:
            _token_acc.dist_input_tokens          += getattr(_u, "input_tokens",  0) or 0
            _token_acc.dist_output_tokens         += getattr(_u, "output_tokens", 0) or 0
            _token_acc.dist_cache_read_tokens     += getattr(_u, "cache_read_input_tokens",    0) or 0
            _token_acc.dist_cache_creation_tokens += getattr(_u, "cache_creation_input_tokens", 0) or 0
            _token_acc.dist_api_calls             += 1

    # 응답 파싱
    if response.stop_reason != "end_turn":
        log.warning("[dist_tool_use] '%s': stop_reason=%s → pending", ocr_name, response.stop_reason)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True,
                              reason="Claude 응답 이상 (stop_reason != end_turn)")

    final_text = next(
        (b.text for b in response.content if hasattr(b, "type") and b.type == "text"),
        "",
    )

    try:
        data = _parse_dist_decision_json(final_text)
    except ToolUseParseError as e:
        log.warning("[dist_tool_use] '%s': JSON 파싱 실패 → pending: %s", ocr_name, e)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True,
                              reason=f"JSON 파싱 실패: {e}")

    decision = data.get("decision", "pending")
    reason   = data.get("reason", "")

    if decision == "pending":
        log.info("[dist_tool_use] '%s': Claude pending 선택 → pending (%s)", ocr_name, reason)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True, reason=reason)

    if decision != "confirmed":
        log.warning("[dist_tool_use] '%s': 알 수 없는 decision=%r → pending", ocr_name, decision)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True,
                              reason=f"알 수 없는 decision: {decision!r}")

    dist_code = (data.get("dist_code") or "").strip()
    if not dist_code:
        log.warning("[dist_tool_use] '%s': dist_code 비어 있음 → pending", ocr_name)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True,
                              reason="dist_code 비어 있음")

    if valid_codes and dist_code not in valid_codes:
        log.warning("[dist_tool_use] '%s': dist_code '%s' 후보 밖 → pending (계약 위반)",
                    ocr_name, dist_code)
        return DistResolution(dist_code=None, basis="needs_confirmation",
                              candidates=candidates, needs_confirmation=True,
                              reason=f"후보 외 dist_code 선택 거부: {dist_code!r}")

    # dist_name 보완 (후보에서)
    dist_name = ""
    for c in candidates:
        if c.get("dist_code") == dist_code:
            dist_name = c.get("dist_name", "")
            break

    log.info("[dist_tool_use] '%s': dist_code=%s (%s) → tool_use 확정 | %s",
             ocr_name, dist_code, dist_name, reason)
    return DistResolution(
        dist_code=dist_code,
        basis="tool_use",
        candidates=candidates,
        needs_confirmation=False,
        reason=reason,
    )


async def _build_dist_decisions_with_tool_use(
    dist_pending: list[dict],
    *,
    form_id: str,
    issuer_fingerprint: str,
    retail_user_rows: list[dict],
    dist_client: Any,
    model: str = _TOOL_USE_MODEL,
    concurrency: int = 1,
    _token_acc: ToolUseTokenStats | None = None,
) -> "tuple[dict[str, DistResolution], list[dict]]":
    """dist_pending의 1:N 항목들을 Claude Tool Use로 결정한다.

    dist_pending: [{"mapping_type":"dist","ocrName":str,"candidates":[...],"page_number":None}]

    반환:
      (resolved: {ocr_name: DistResolution},  # tool_use 또는 needs_confirmation
       remaining_pending: [dict])              # 여전히 pending인 항목
    """
    from .phase3_dist_resolver import DistResolution

    # retailer_name 조회용 (retail_user_rows에서)
    retailer_name_by_code: dict[str, str] = {
        r.get("소매처코드", ""): r.get("소매처명", "")
        for r in retail_user_rows if r.get("소매처코드")
    }
    # retailer_code를 ocr_name에서 역추적하기 위해 dist_pending에서 추출
    # dist_pending 항목에는 retailer_code가 없으므로 candidates 컨텍스트로만 판단
    # (ocr_name은 거래처명, candidates는 [{"dist_code","dist_name"}])

    _sem = asyncio.Semaphore(max(1, concurrency))

    async def _resolve_one(item: dict) -> "tuple[str, DistResolution | None]":
        ocr_name   = item.get("ocrName", "")
        candidates = item.get("candidates", [])
        # retailer_code: dist_pending에 저장 안 됨 → ocr_name을 key로만 사용
        # context에서 가져올 수 없으므로 "" 사용 (Claude는 ocr_name과 issuer_fingerprint 활용)
        retailer_code = item.get("retailer_code", "")
        retailer_name = retailer_name_by_code.get(retailer_code, "")

        local_acc = ToolUseTokenStats()
        try:
            async with _sem:
                res = await _run_single_dist_mapping(
                    ocr_name=ocr_name,
                    retailer_code=retailer_code,
                    candidates=candidates,
                    form_id=form_id,
                    issuer_fingerprint=issuer_fingerprint,
                    retailer_name=retailer_name,
                    client=dist_client,
                    model=model,
                    _token_acc=local_acc,
                )
        except ToolUseFallbackTrigger:
            # API 오류 등: 개별 dist는 pending으로 처리 (전체 fallback 아님)
            log.warning("[dist_tool_use] '%s': API 오류 → pending 유지", ocr_name)
            res = DistResolution(
                dist_code=None, basis="needs_confirmation",
                candidates=candidates, needs_confirmation=True,
                reason="API 오류로 결정 불가",
            )
        finally:
            if _token_acc is not None:
                _token_acc.dist_input_tokens          += local_acc.dist_input_tokens
                _token_acc.dist_output_tokens         += local_acc.dist_output_tokens
                _token_acc.dist_cache_read_tokens     += local_acc.dist_cache_read_tokens
                _token_acc.dist_cache_creation_tokens += local_acc.dist_cache_creation_tokens
                _token_acc.dist_api_calls             += local_acc.dist_api_calls

        return ocr_name, res

    tasks = [_resolve_one(item) for item in dist_pending]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    resolved:          dict[str, "DistResolution"] = {}
    remaining_pending: list[dict]                   = []

    for idx, item in enumerate(dist_pending):
        ocr_name = item.get("ocrName", "")
        task_result = raw_results[idx]

        if isinstance(task_result, BaseException):
            log.warning("[dist_tool_use] '%s': 예외 발생 → pending 유지: %s",
                        ocr_name, task_result)
            remaining_pending.append(item)
            continue

        _, res = task_result
        resolved[ocr_name] = res
        if res.needs_confirmation:
            # Claude가 pending 선택 → pending 유지
            remaining_pending.append(item)
        # else: tool_use 확정 → remaining_pending에서 제외

    return resolved, remaining_pending


async def _execute_success_path(
    batch_result: Any,
    *,
    doc_id: str,
    form_id: str,
    hatsu_month: str,
    phase2_result: dict,
    output_dir: Path,
    mappings_dir: Path,
    form_definitions_dir: Path,
    run_id: str = "",
    product_client: Any = None,
    max_product_turns: int = 3,
    model: str = _TOOL_USE_MODEL,
    concurrency: int = 1,
    _token_acc: ToolUseTokenStats | None = None,
) -> tuple[dict, list[dict]]:
    """Tool Use 성공 시 실제 phase3 출력을 생성한다."""
    # ── CSV 사전 로드 (파일 I/O 1회, 이후 재사용) ─────────────────────────────
    retail_user_path = mappings_dir / "retail_user.csv"
    retail_user_rows = _read_csv(retail_user_path) if retail_user_path.exists() else []
    retailer_name_by_code = {r["소매처코드"]: r["소매처명"] for r in retail_user_rows if r.get("소매처코드")}
    dist_name_by_code     = {r["판매처코드"]: r["판매처명"] for r in retail_user_rows if r.get("판매처코드")}

    ocr_dist_path = mappings_dir / "ocr_dist.csv"
    cached_dist: dict = {}
    if ocr_dist_path.exists():
        for _row in _read_csv(ocr_dist_path):
            _k = (_row.get("form_id", ""), _row.get("issuer_fingerprint", ""), _row.get("retailer_code", ""))
            cached_dist[_k] = _row.get("dist_code", "")

    # ── issuer 추출 ──────────────────────────────────────────────────────────
    issuer: dict = {}
    for page in phase2_result.get("pages", []):
        if page.get("role") == "cover" and page.get("issuer"):
            issuer = page["issuer"]
            break

    form_path = form_definitions_dir / f"{form_id}.md"
    form_md = form_path.read_text(encoding="utf-8") if form_path.exists() else ""
    fp_fields = _parse_fingerprint_fields(form_md)
    issuer_fingerprint = _build_issuer_fingerprint(issuer, fp_fields)

    # ── Retailer token usage 누적 (batch_result.stats에서) ────────────────────
    # success path에서만 호출됨. fallback 시는 exception.partial_token_stats 경로를 사용.
    if batch_result is not None and _token_acc is not None:
        _s = batch_result.stats
        _token_acc.retailer_input_tokens          += getattr(_s, "total_input_tokens",          0) or 0
        _token_acc.retailer_output_tokens         += getattr(_s, "total_output_tokens",         0) or 0
        _token_acc.retailer_cache_read_tokens     += getattr(_s, "total_cache_read_tokens",     0) or 0
        _token_acc.retailer_cache_creation_tokens += getattr(_s, "total_cache_creation_tokens", 0) or 0
        _token_acc.retailer_api_calls             += getattr(_s, "total_api_calls",             0) or 0

    # ── Retailer decisions ────────────────────────────────────────────────────
    if batch_result is not None and batch_result.per_retailer:
        retailer_decisions, dist_resolutions, dist_pending = _batch_result_to_retailer_decisions(
            batch_result.per_retailer,
            form_id=form_id,
            issuer_fingerprint=issuer_fingerprint,
            cached_dist=cached_dist,
            retail_user_rows=retail_user_rows,
        )
    else:
        retailer_decisions = []
        dist_resolutions: dict[str, DistResolution] = {}
        dist_pending: list[dict] = []

    # ── Dist 1:N Tool Use (후보 2건 이상 → Claude 판단) ─────────────────────────
    if dist_pending and product_client is not None:
        dist_updates, dist_pending = await _build_dist_decisions_with_tool_use(
            dist_pending=dist_pending,
            form_id=form_id,
            issuer_fingerprint=issuer_fingerprint,
            retail_user_rows=retail_user_rows,
            dist_client=product_client,
            model=model,
            concurrency=concurrency,
            _token_acc=_token_acc,
        )
        # dist_resolutions 업데이트 (tool_use 확정 또는 needs_confirmation 유지)
        dist_resolutions.update(dist_updates)
        # retailer_decisions의 dist_code를 확정값으로 반영
        from dataclasses import replace as _dc_replace
        for i, rd in enumerate(retailer_decisions):
            new_res = dist_updates.get(rd.ocr_name)
            if new_res and new_res.dist_code:
                retailer_decisions[i] = _dc_replace(rd, dist_code=new_res.dist_code)
        log.info(
            "[%s] Dist 1:N Tool Use 완료 — 확정=%d건, pending=%d건",
            doc_id,
            sum(1 for r in dist_updates.values() if r.dist_code),
            len(dist_pending),
        )

    # ── Product decisions ────────────────────────────────────────────────────
    items = phase2_result.get("items", [])
    unique_products: list[str] = list(dict.fromkeys(
        i["product"] for i in items if i.get("product")
    ))
    product_decisions = await _build_product_decisions_with_tool_use(
        unique_products, mappings_dir,
        product_client=product_client,
        max_product_turns=max_product_turns,
        model=model,
        concurrency=concurrency,
        _token_acc=_token_acc,
    )

    # ── phase3 output 조립 ────────────────────────────────────────────────────
    result, pending = convert_tool_use_result_to_phase3_output(
        doc_id=doc_id, form_id=form_id, hatsu_month=hatsu_month,
        issuer=issuer, phase2_result=phase2_result,
        retailer_decisions=retailer_decisions,
        product_decisions=product_decisions,
    )
    pending.extend(dist_pending)

    # ── phase3_output.json 저장 ────────────────────────────────────────────────
    try:
        out_path = output_dir / "phase3_output.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        raise ToolUseDispatchError(f"phase3_output.json 저장 실패: {exc}") from exc

    # ── confirm_mapping ────────────────────────────────────────────────────────
    for rd in retailer_decisions:
        if rd.retailer_code and rd.basis in {"bracket_code", "tool_use"}:
            await confirm_mapping(
                mapping_type="retailer", ocr_name=rd.ocr_name,
                confirmed_code=rd.retailer_code,
                context={"retailer_name": retailer_name_by_code.get(rd.retailer_code, "")},
                mappings_dir=mappings_dir,
            )
        dist_res = dist_resolutions.get(rd.ocr_name)
        # auto_1_to_1 또는 tool_use(Claude 확정) 모두 저장
        if dist_res and dist_res.basis in {"auto_1_to_1", "tool_use"} and dist_res.dist_code:
            await confirm_mapping(
                mapping_type="dist", ocr_name=rd.ocr_name,
                confirmed_code=dist_res.dist_code,
                context={
                    "form_id": form_id, "issuer_fingerprint": issuer_fingerprint,
                    "retailer_code": rd.retailer_code,
                    "dist_name": dist_name_by_code.get(dist_res.dist_code, ""),
                },
                mappings_dir=mappings_dir,
            )

    for pd in product_decisions:
        if pd.product_code and pd.basis == "tool_use":
            await confirm_mapping(
                mapping_type="product", ocr_name=pd.ocr_name,
                confirmed_code=pd.product_code,
                context={"product_name": pd.product_name},
                mappings_dir=mappings_dir,
            )

    log.info(
        "[%s] Tool Use success path 완료 — retailers=%d, products=%d, dist_1n_confirmed=%d, pending=%d",
        doc_id, len(retailer_decisions), len(product_decisions),
        sum(1 for r in dist_resolutions.values() if r.basis == "tool_use"),
        len(pending),
    )
    return result, pending


# ── 공개 Fallback 래퍼 ────────────────────────────────────────────────────────

async def run_phase3_with_tool_use_or_fallback(
    doc_id: str,
    phase2_result: dict,
    output_dir: Path,
    form_id: str,
    hatsu_month: str = "",
    run_id: str = "",
    *,
    enable_tool_use: bool = False,
    max_turns: int = 5,
    settings: Any = None,
) -> tuple[dict, list[dict], Phase3FallbackStats]:
    """run_phase3()의 fallback-enabled 래퍼.

    Token Usage 기록:
        성공 시: stats.token_usage에 수집 후 _record_tool_use_token_usage() 호출
        Fallback 시: 이미 누적된 token usage를 _record_tool_use_token_usage() 호출 후 legacy 실행
        API response 없음(ToolUseApiError): usage=0 유지
    """
    if settings is None:
        settings = get_settings()

    # settings에서 model / concurrency 결정
    _model = getattr(settings, "phase3_tool_use_model", _TOOL_USE_MODEL) or _TOOL_USE_MODEL
    _raw_conc = getattr(settings, "phase3_tool_use_concurrency", 1)
    _concurrency = max(1, _raw_conc) if isinstance(_raw_conc, int) else 1
    _raw_global = getattr(settings, "phase3_tool_use_global_concurrency", 3)
    _global_conc = max(1, _raw_global) if isinstance(_raw_global, int) else 3

    stats = Phase3FallbackStats(
        enable_tool_use=enable_tool_use,
        used_tool_use=False, fallback_triggered=False,
        fallback_reason=None, fallback_class=None,
        tool_use_elapsed_ms=0.0, legacy_elapsed_ms=0.0, total_elapsed_ms=0.0,
        max_turns_hit=False, api_retry_failed=False,
        batch_size=0, batch_failure_count=0,
    )
    wall_start = time.perf_counter()

    # ── Feature flag OFF → legacy 직접 호출 ──────────────────────────────────
    if not enable_tool_use:
        t0 = time.perf_counter()
        result, pending = await run_phase3(
            doc_id, phase2_result, output_dir, form_id, hatsu_month, run_id
        )
        stats.legacy_elapsed_ms = (time.perf_counter() - t0) * 1000
        stats.total_elapsed_ms = (time.perf_counter() - wall_start) * 1000
        return result, pending, stats

    # ── Tool Use 시도 (전역 세마포어로 동시 문서 수 제한) ─────────────────────────
    # 여러 사용자가 동시에 문서를 올릴 때 Claude API Rate Limit 방지.
    # 전역 세마포어: 앱 전체에서 동시에 Tool Use Claude API를 호출할 수 있는 문서 수 상한.
    # legacy fallback은 Claude API를 사용하지 않으므로 세마포어 외부에서 실행.
    stats.used_tool_use = True
    tu_start = time.perf_counter()

    try:
        async with _get_global_tool_use_sem(_global_conc):
            batch_result = await _attempt_tool_use_phase(
                phase2_result=phase2_result,
                mappings_dir=settings.mappings_dir,
                form_definitions_dir=settings.form_definitions_dir,
                form_id=form_id,
                max_turns=max_turns,
                stats=stats,
                model=_model,
                concurrency=_concurrency,
                anthropic_api_key=getattr(settings, "anthropic_api_key", "") or "",
            )
            stats.tool_use_elapsed_ms = (time.perf_counter() - tu_start) * 1000
            log.info("[%s] Tool Use 성공 (%.0fms) → success path", doc_id, stats.tool_use_elapsed_ms)

            import anthropic as _anthropic
            _product_client = (
                _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                if settings.anthropic_api_key else None
            )

            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id=doc_id, form_id=form_id, hatsu_month=hatsu_month,
                phase2_result=phase2_result, output_dir=output_dir,
                mappings_dir=settings.mappings_dir,
                form_definitions_dir=settings.form_definitions_dir,
                run_id=run_id, product_client=_product_client,
                model=_model,
                concurrency=_concurrency,
                _token_acc=stats.token_usage,
            )

        # async with 블록 밖 (세마포어 해제 후)
        stats.total_elapsed_ms = (time.perf_counter() - wall_start) * 1000

        # ── token usage DB 기록 (success path) ────────────────────────────────
        await _record_tool_use_token_usage(doc_id, run_id, stats.token_usage, model=_model)

        log.info(
            "[%s] Phase 3 완료 (Tool Use) — tool_use=%.0fms / total=%.0fms / "
            "tokens=in%d+out%d (retailer:%d, product:%d)",
            doc_id, stats.tool_use_elapsed_ms, stats.total_elapsed_ms,
            stats.token_usage.total_input_tokens, stats.token_usage.total_output_tokens,
            stats.token_usage.retailer_api_calls, stats.token_usage.product_api_calls,
        )
        return result, pending, stats

    except ToolUseFallbackTrigger as exc:
        # 세마포어는 async with 블록 탈출 시 자동 해제됨
        if stats.tool_use_elapsed_ms == 0.0:
            stats.tool_use_elapsed_ms = (time.perf_counter() - tu_start) * 1000
        stats.fallback_triggered = True
        stats.fallback_reason = exc.reason
        stats.fallback_class = type(exc).__name__
        if isinstance(exc, ToolUseMaxTurnsError):
            stats.max_turns_hit = True
        if isinstance(exc, ToolUseApiError):
            stats.api_retry_failed = True
        log.warning(
            "[%s] Tool Use 실패 → Legacy fallback. 원인: [%s] %s (%.0fms)",
            doc_id, type(exc).__name__, exc.reason, stats.tool_use_elapsed_ms,
        )

        # ── fallback 시 token usage 보존 ─────────────────────────────────────
        _partial = exc.partial_token_stats
        if _partial is not None:
            stats.token_usage.retailer_input_tokens          += _partial.retailer_input_tokens
            stats.token_usage.retailer_output_tokens         += _partial.retailer_output_tokens
            stats.token_usage.retailer_cache_read_tokens     += _partial.retailer_cache_read_tokens
            stats.token_usage.retailer_cache_creation_tokens += _partial.retailer_cache_creation_tokens
            stats.token_usage.retailer_api_calls             += _partial.retailer_api_calls
            stats.token_usage.product_input_tokens           += _partial.product_input_tokens
            stats.token_usage.product_output_tokens          += _partial.product_output_tokens
            stats.token_usage.product_cache_read_tokens      += _partial.product_cache_read_tokens
            stats.token_usage.product_cache_creation_tokens  += _partial.product_cache_creation_tokens
            stats.token_usage.product_api_calls              += _partial.product_api_calls

    # ── fallback 시에도 누적 token usage를 DB에 기록 ─────────────────────────
    await _record_tool_use_token_usage(doc_id, run_id, stats.token_usage, model=_model)

    # ── Legacy run_phase3() (세마포어 외부 — Claude Tool Use API 미사용) ────────
    t1 = time.perf_counter()
    result, pending = await run_phase3(
        doc_id, phase2_result, output_dir, form_id, hatsu_month, run_id
    )
    stats.legacy_elapsed_ms = (time.perf_counter() - t1) * 1000
    stats.total_elapsed_ms = (time.perf_counter() - wall_start) * 1000

    log.info(
        "[%s] Phase 3 완료 (fallback) — tool_use=%.0fms / legacy=%.0fms / "
        "preserved_tokens=in%d+out%d",
        doc_id, stats.tool_use_elapsed_ms, stats.legacy_elapsed_ms,
        stats.token_usage.total_input_tokens, stats.token_usage.total_output_tokens,
    )
    return result, pending, stats
