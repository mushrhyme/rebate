"""phase3_tool_use_experiment.py — Claude tool_use runtime 실험

production phase3.py와 완전히 독립된 실험 파일.
retailer 매핑 1건에 대해 Claude가 lookup_retailer / confirm_mapping tool을
실제로 tool_use로 호출하는 루프를 검증한다.

현재 상태:
  - production phase3.py 미연결 (실험 전용)
  - Claude API 호출 포함 — 실제 LLM 호출 발생
  - search_product 미사용 (retailer 매핑 1건 범위)
  - MCP 미연결, Agent planner 없음

안전장치:
  - max_turns: 루프 무한 실행 방지
  - allowed_tools allowlist: 등록 외 tool 차단
  - allow_side_effects: confirm_mapping 등 쓰기 tool을 명시적으로 허용할 때만 실행
  - 실행 실패 시 error tool_result를 Claude에게 전달 후 계속
"""
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anthropic

from ..tools.claude_adapter import build_claude_tools, dispatch_tool_call, inject_tool_context
from ..tools.claude_retry import async_call_with_retry
from ..tools.registry import get_tool

log = logging.getLogger(__name__)

__all__ = [
    "ExperimentResult",
    "ToolCallRecord",
    "run_retailer_mapping_experiment",
]

# ── 설정 상수 ─────────────────────────────────────────────────────────────────

_ALLOWED_TOOLS: frozenset[str] = frozenset({"lookup_retailer", "confirm_mapping"})
_PATH_FIELDS:   frozenset[str] = frozenset({"mappings_dir", "form_definitions_dir"})
_DEFAULT_MAX_TURNS = 5


# ── 결과 타입 ─────────────────────────────────────────────────────────────────

@dataclass
class ToolCallRecord:
    """단일 tool 실행 기록."""
    name: str
    input: dict        # Claude가 제공한 인자 (경로 제외)
    output: Any        # Tool 반환값
    error: str | None = None


@dataclass
class ExperimentResult:
    """tool_use 루프 전체 결과."""
    tool_calls: list[ToolCallRecord]
    final_text: str | None
    turns_used: int
    # Claude API token usage (defensive — mock 환경에서는 0)
    input_tokens:         int = 0
    output_tokens:        int = 0
    cache_read_tokens:    int = 0
    cache_creation_tokens: int = 0
    api_call_count:       int = 0
    # allow_side_effects=False 시 Claude의 confirm_mapping 결정을 캡처
    # (실제 CSV 쓰기는 차단, 결정만 기록)
    decided_code: str | None = None

    @property
    def confirmed_code(self) -> str | None:
        """Claude가 결정한 확정 코드를 반환한다.

        우선순위:
          1. decided_code — allow_side_effects=False 시 캡처된 결정
          2. tool_calls   — allow_side_effects=True 시 실제 실행된 confirm_mapping
        """
        if self.decided_code:
            return self.decided_code
        for tc in self.tool_calls:
            if tc.name == "confirm_mapping" and tc.error is None:
                return tc.input.get("confirmed_code")
        return None

    @property
    def lookup_basis(self) -> str | None:
        """lookup_retailer 결과의 basis 값을 반환한다."""
        for tc in self.tool_calls:
            if tc.name == "lookup_retailer" and tc.error is None and tc.output is not None:
                return getattr(tc.output, "basis", None)
        return None


# ── 내부 헬퍼 ─────────────────────────────────────────────────────────────────

def _build_experiment_tools() -> list[dict]:
    """build_claude_tools() 기반으로 실험용 Claude schema 생성.

    build_claude_tools()로 기본 스키마를 가져온 뒤:
    1. _ALLOWED_TOOLS 외 tool 제거
    2. _PATH_FIELDS(mappings_dir, form_definitions_dir)를 required / properties에서 제거
       → Claude가 시맨틱 파라미터만 제공, 경로는 서버 컨텍스트에서 주입
    """
    result = []
    for tool in build_claude_tools():   # registry에서 로드
        if tool["name"] not in _ALLOWED_TOOLS:
            continue
        schema = tool["input_schema"]
        required = [r for r in schema.get("required", []) if r not in _PATH_FIELDS]
        props = {
            k: v for k, v in schema.get("properties", {}).items()
            if k not in _PATH_FIELDS
        }
        result.append({
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": {
                "type": "object",
                "required": required,
                "properties": props,
            },
        })
    return result


def _inject_context(tool_name: str, claude_args: dict, ctx: dict) -> dict:
    """Claude 인자에 서버 컨텍스트(경로, form_id)를 주입한다.

    inject_tool_context()로 기본 병합 후,
    confirm_mapping의 context 키 기본값만 이 실험 파일에서 추가 처리한다.
    """
    merged = inject_tool_context(tool_name, claude_args, ctx)
    if tool_name == "confirm_mapping" and "context" not in merged:
        merged["context"] = {}
    return merged


def _serialize_result(result: Any) -> str:
    """Tool 결과를 Claude가 읽을 수 있는 JSON 문자열로 변환한다."""
    if result is None:
        return '{"status": "success"}'
    try:
        if hasattr(result, "__dataclass_fields__"):
            return json.dumps(asdict(result), ensure_ascii=False, default=str)
    except Exception:
        pass
    return json.dumps(str(result), ensure_ascii=False)


# ── 공개 실험 함수 ─────────────────────────────────────────────────────────────

async def run_retailer_mapping_experiment(
    ocr_name: str,
    form_id: str,
    mappings_dir: Path,
    form_definitions_dir: Path | None = None,
    *,
    model: str = "claude-haiku-4-5-20251001",
    max_turns: int = _DEFAULT_MAX_TURNS,
    allow_side_effects: bool = False,
    client: Any = None,
) -> ExperimentResult:
    """retailer 매핑 1건에 대해 Claude tool_use 루프를 실행한다.

    Args:
        ocr_name:            OCR 거래처명 원문
        form_id:             양식 ID (예: "form_01")
        mappings_dir:        mappings/ 디렉토리 경로
        form_definitions_dir: form_definitions/ 경로 (None이면 settings에서 로드)
        model:               사용할 Claude 모델
        max_turns:           최대 루프 횟수 (초과 시 RuntimeError)
        allow_side_effects:  True = confirm_mapping 등 쓰기 tool 허용
        client:              Anthropic AsyncAnthropic 클라이언트 (None이면 자동 생성)

    Returns:
        ExperimentResult (tool_calls 기록, final_text, turns_used 포함)

    Raises:
        RuntimeError: max_turns 초과 시
    """
    if client is None:
        from ..core.config import get_settings
        client = anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key)

    # 서버 컨텍스트 — Claude에게 노출하지 않고 dispatch 시점에 주입
    _ctx = {
        "form_id": form_id,
        "mappings_dir": mappings_dir,
        "form_definitions_dir": form_definitions_dir,
    }

    experiment_tools = _build_experiment_tools()

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"OCR 거래처명 '{ocr_name}'의 소매처코드를 매핑해라.\n"
                f"1. lookup_retailer 도구로 소매처코드 후보를 조회한다.\n"
                f"2. 후보가 있으면 가장 유사도가 높은 것으로 confirm_mapping을 호출해 확정한다.\n"
                f"3. 후보가 없으면 매핑 불가 이유를 설명한다."
            ),
        }
    ]

    tool_calls: list[ToolCallRecord] = []
    _in_tok = _out_tok = _cr_tok = _cc_tok = _calls = 0
    _decided_code: str | None = None  # allow_side_effects=False 시 캡처된 결정
    # lookup_retailer가 반환한 코드만 확정 허용 (후보 외 코드 = hallucination 거부)
    # tool_contracts.md "후보외거부" 계약의 실행 지점
    _allowed_codes: set[str] = set()

    for turn in range(max_turns):
        # 첫 번째 turn에서 lookup_retailer 강제 호출 (Claude가 tool을 생략하는 현상 방지)
        # 이후 turn에서는 auto (Claude가 confirm_mapping 또는 end_turn 자유 선택)
        _extra: dict = {}
        if turn == 0:
            _extra["tool_choice"] = {"type": "tool", "name": "lookup_retailer"}

        response = await async_call_with_retry(
            client.messages.create,
            model=model,
            max_tokens=1024,
            tools=experiment_tools,
            messages=messages,
            **_extra,
        )

        # ── token usage 수집 (defensive — mock 환경은 usage 없을 수 있음) ──────
        _u = getattr(response, "usage", None)
        if _u is not None:
            _in_tok  += getattr(_u, "input_tokens",  0) or 0
            _out_tok += getattr(_u, "output_tokens", 0) or 0
            _cr_tok  += getattr(_u, "cache_read_input_tokens",    0) or 0
            _cc_tok  += getattr(_u, "cache_creation_input_tokens", 0) or 0
            _calls   += 1

        # ── end_turn: 최종 텍스트 응답 → 루프 종료 ───────────────────────────
        if response.stop_reason == "end_turn":
            # ── tool 미호출 감지: turn 0에서 lookup_retailer 없이 end_turn ──────
            # tool_choice={"type": "tool", "name": "lookup_retailer"}를 보냈음에도
            # Claude가 tool 없이 종료하면 계약 위반으로 처리한다.
            has_lookup = any(tc.name == "lookup_retailer" for tc in tool_calls)
            if turn == 0 and not has_lookup and not _decided_code:
                raise RuntimeError(
                    f"tool_not_called: Claude가 lookup_retailer를 호출하지 않고 종료 "
                    f"(ocr_name={ocr_name!r}, turn={turn})"
                )
            final_text = next(
                (b.text for b in response.content
                 if hasattr(b, "type") and b.type == "text"),
                None,
            )
            return ExperimentResult(
                tool_calls=tool_calls,
                final_text=final_text,
                turns_used=turn + 1,
                input_tokens=_in_tok,
                output_tokens=_out_tok,
                cache_read_tokens=_cr_tok,
                cache_creation_tokens=_cc_tok,
                api_call_count=_calls,
                decided_code=_decided_code,
            )

        if response.stop_reason != "tool_use":
            log.warning("[experiment] 예상치 못한 stop_reason: %s", response.stop_reason)
            break

        # ── tool_use: 각 block 실행 ────────────────────────────────────────────
        tool_results: list[dict] = []

        for block in response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue

            name      = block.name
            claude_args = dict(block.input) if block.input else {}

            # 1. Allowlist 확인
            if name not in _ALLOWED_TOOLS:
                log.warning("[experiment] 허용되지 않은 tool 호출: %s", name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "is_error": True,
                    "content": (
                        f"허용되지 않은 tool: {name!r}. "
                        f"허용 목록: {sorted(_ALLOWED_TOOLS)}"
                    ),
                })
                continue

            # 2. 후보 외 코드 거부 — lookup_retailer가 반환한 적 없는 코드는
            #    allow_side_effects 여부와 무관하게 확정·저장 모두 차단한다.
            if name == "confirm_mapping":
                _proposed = (claude_args.get("confirmed_code") or "").strip()
                if _proposed and _proposed not in _allowed_codes:
                    log.warning(
                        "[experiment] confirm_mapping 후보 외 코드 거부 — "
                        "ocr_name=%r, confirmed_code=%s (허용 %d개)",
                        ocr_name, _proposed, len(_allowed_codes),
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": (
                            "후보 외 코드는 확정할 수 없습니다. "
                            "lookup_retailer가 반환한 retailer_code 또는 candidates의 "
                            "retailer_code 중에서만 선택하세요. "
                            "적합한 후보가 없으면 confirm 없이 매핑 불가 이유를 설명하고 종료하세요."
                        ),
                    })
                    continue

            # 3. Side effects 확인
            spec = get_tool(name)
            if spec.side_effects and not allow_side_effects:
                if name == "confirm_mapping":
                    # confirm_mapping: CSV 쓰기는 차단하되 Claude의 결정(confirmed_code)은 캡처.
                    # Claude에게 성공 응답 반환 → 정상 end_turn 유도.
                    # 실제 저장은 _execute_success_path()에서 1회만 수행한다.
                    _decided_code = (claude_args.get("confirmed_code") or "").strip() or _decided_code
                    log.info(
                        "[experiment] confirm_mapping 결정 캡처 (CSV 차단) → confirmed_code=%s",
                        _decided_code,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f'{{"status": "captured", "confirmed_code": "{_decided_code or ""}"}}',
                    })
                else:
                    # 기타 side_effects tool: error 반환 (기존 동작 유지)
                    log.info(
                        "[experiment] side_effects tool '%s' 차단 (allow_side_effects=False)", name
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "is_error": True,
                        "content": (
                            f"'{name}'은 side_effects=True입니다. "
                            f"실행하려면 allow_side_effects=True로 설정하세요."
                        ),
                    })
                continue

            # 4. 컨텍스트 주입 후 실행
            full_args = _inject_context(name, claude_args, _ctx)
            try:
                result = await dispatch_tool_call(name, full_args)
                if name == "lookup_retailer" and result is not None:
                    # 이 lookup이 반환한 코드들을 확정 허용 집합에 등록
                    _code = getattr(result, "retailer_code", None)
                    if _code:
                        _allowed_codes.add(_code)
                    for _c in getattr(result, "candidates", None) or []:
                        if isinstance(_c, dict) and _c.get("retailer_code"):
                            _allowed_codes.add(_c["retailer_code"])
                record = ToolCallRecord(name=name, input=claude_args, output=result)
                tool_calls.append(record)
                serialized = _serialize_result(result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": serialized,
                })
                log.info("[experiment] tool '%s' 성공 → %s", name, serialized[:120])

            except Exception as exc:
                record = ToolCallRecord(name=name, input=claude_args, output=None, error=str(exc))
                tool_calls.append(record)
                error_msg = f"실행 오류: {exc}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "is_error": True,
                    "content": error_msg,
                })
                log.error("[experiment] tool '%s' 오류: %s", name, exc)

        # 다음 turn: 이번 응답 + tool_results를 메시지에 추가
        messages.append({"role": "assistant", "content": list(response.content)})
        messages.append({"role": "user",      "content": tool_results})

    raise RuntimeError(
        f"max_turns({max_turns}) 초과 — "
        f"tool_use 루프가 end_turn 없이 종료되지 않음"
    )
