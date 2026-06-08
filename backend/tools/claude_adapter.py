"""claude_adapter.py — Claude tool_use 형식 어댑터

TOOL_REGISTRY → Claude tool_use API 형식 변환 및 tool call 실행 dispatcher.

현재 상태:
  - production phase3.py에는 연결하지 않음 (실험용)
  - Claude API 호출 없음 — schema 생성과 dispatch만 구현
  - MCP 연결 없음

향후:
  - phase3.py의 Claude 호출 시 tools=build_claude_tools() 활용
  - Claude tool_use loop 구현 시 dispatch_tool_call을 tool_use block 처리에 사용

Path Coercion:
  Claude tool_use input은 JSON이므로 Path 객체를 직접 보낼 수 없다.
  dispatch_tool_call()은 Tool callable의 파라미터 서명을 검사해
  str → Path 변환을 자동으로 수행한다. 이 변환은 Adapter 레이어의 책임이다.
"""
import inspect
import logging
import pathlib
import types
import typing
from pathlib import Path
from typing import Any

from .registry import ToolSpec, get_tool, list_tools

log = logging.getLogger(__name__)

__all__ = ["build_claude_tools", "coerce_tool_arguments", "dispatch_tool_call", "inject_tool_context"]

# ToolSpec별 inspect.Signature 캐시 (name 기준)
_SIG_CACHE: dict[str, inspect.Signature] = {}


def _get_signature(spec: "ToolSpec") -> inspect.Signature:
    if spec.name not in _SIG_CACHE:
        _SIG_CACHE[spec.name] = inspect.signature(spec.callable)
    return _SIG_CACHE[spec.name]


def inject_tool_context(tool_name: str, claude_args: dict, ctx: dict) -> dict:
    """Tool callable 서명 기준으로 ctx를 필터링한 뒤 claude_args와 병합한다.

    Claude가 제공한 값(claude_args)이 컨텍스트(ctx)보다 우선한다.
    Tool이 받지 않는 ctx 필드는 자동으로 제외된다.

    Args:
        tool_name:   Tool 이름 (TOOL_REGISTRY 키)
        claude_args: Claude가 제공한 인자 dict
        ctx:         서버 컨텍스트 (경로, form_id 등 Tool에 주입할 값)

    Returns:
        병합된 인자 dict (dispatch_tool_call에 그대로 전달 가능)

    Raises:
        KeyError: 등록되지 않은 tool_name
    """
    spec = get_tool(tool_name)
    valid_params = set(_get_signature(spec).parameters.keys())
    merged = {k: v for k, v in ctx.items() if k in valid_params}
    merged.update(claude_args)
    return merged


# ── Path 타입 판별 헬퍼 ───────────────────────────────────────────────────────

def _is_path_annotation(annotation: Any) -> bool:
    """파라미터 annotation이 Path 타입을 포함하는지 판별한다.

    처리 대상:
      Path                    → True
      Path | None             → True  (Python 3.10+ types.UnionType)
      Optional[Path]          → True  (typing.Union[Path, None])
      int / str / 기타        → False
    """
    if annotation is inspect.Parameter.empty:
        return False
    if annotation is pathlib.Path:
        return True
    # Python 3.10+ 리터럴 union: Path | None
    if isinstance(annotation, types.UnionType):
        return pathlib.Path in annotation.__args__
    # typing.Optional[Path] == typing.Union[Path, None]
    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        return pathlib.Path in annotation.__args__
    return False


def _allows_none(annotation: Any) -> bool:
    """파라미터 annotation이 None을 허용하는지 판별한다."""
    if annotation is inspect.Parameter.empty:
        return False
    if isinstance(annotation, types.UnionType):
        return type(None) in annotation.__args__
    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        return type(None) in annotation.__args__
    return False


# ── Coercion Layer ────────────────────────────────────────────────────────────

def coerce_tool_arguments(spec: ToolSpec, arguments: dict) -> dict:
    """Tool callable 서명을 기반으로 arguments를 올바른 Python 타입으로 변환한다.

    Claude tool_use input은 JSON이므로 Path 파라미터가 string으로 전달된다.
    이 함수가 str → Path 변환을 담당한다. Tool 서명을 단일 출처로 사용한다.

    변환 규칙:
      - Path 파라미터: str → Path()
      - Path | None 파라미터: str → Path(), None → None
      - 이미 Path이면 그대로 유지
      - Tool이 받지 않는 여분의 인자: 경고 후 제거 (Claude가 추가 필드를 보낼 수 있음)

    Args:
        spec:      ToolSpec (callable 서명 포함)
        arguments: Claude 또는 호출자가 제공한 인자 dict

    Returns:
        변환된 인자 dict (Tool callable에 **kwargs로 전달 가능)

    Raises:
        TypeError: Path 파라미터에 str/Path/None 이외의 타입이 들어온 경우
    """
    sig = _get_signature(spec)
    valid_params = set(sig.parameters.keys())

    # 1. Tool이 받지 않는 인자 제거 (Claude가 schema에 없는 필드를 보낼 경우 방어)
    extra = set(arguments) - valid_params
    if extra:
        log.warning(
            "[coerce] Tool '%s'에 알 수 없는 인자 제거: %s",
            spec.name, sorted(extra),
        )
    coerced = {k: v for k, v in arguments.items() if k in valid_params}

    # 2. Path 타입 변환
    for param_name, param in sig.parameters.items():
        if param_name not in coerced:
            continue
        value = coerced[param_name]
        annotation = param.annotation

        if not _is_path_annotation(annotation):
            continue

        if value is None:
            if not _allows_none(annotation):
                raise TypeError(
                    f"Tool '{spec.name}': 파라미터 '{param_name}'은 None을 허용하지 않습니다 "
                    f"(Path 필수, 받은 값: None)"
                )
            # None 허용 → 그대로 유지
        elif isinstance(value, pathlib.Path):
            pass  # 이미 Path — 변환 없음
        elif isinstance(value, str):
            coerced[param_name] = pathlib.Path(value)
        else:
            raise TypeError(
                f"Tool '{spec.name}': 파라미터 '{param_name}'의 타입이 잘못됨. "
                f"str 또는 Path 필요, 받은 타입: {type(value).__name__!r}"
            )

    return coerced


# ── 공개 API ──────────────────────────────────────────────────────────────────

def build_claude_tools() -> list[dict]:
    """TOOL_REGISTRY를 Anthropic Claude tool_use 형식의 tools 배열로 변환한다.

    각 Tool의 name, description, input_schema를 Anthropic API가 요구하는
    형식으로 조합한다. input_schema는 이미 JSON Schema 형식이므로 변환 없이 그대로 사용.

    Returns:
        Anthropic messages.create(tools=...) 에 넣을 수 있는 list[dict]

    Example (미래 phase3.py):
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            tools=build_claude_tools(),
            messages=[...],
        )
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        }
        for spec in list_tools()
    ]


async def dispatch_tool_call(name: str, arguments: dict) -> Any:
    """Claude tool_use 응답의 tool call을 실제 callable로 실행한다.

    Claude가 tool_use content block을 반환했을 때,
    block의 name과 input dict를 받아 대응하는 Tool을 실행한다.

    Path Coercion:
      Claude tool_use input은 JSON이므로 Path 파라미터가 string으로 전달된다.
      coerce_tool_arguments()가 str → Path 변환을 자동으로 수행한다.
      Python에서 직접 Path 객체를 전달해도 그대로 동작한다.

    Args:
        name:      실행할 Tool 이름 (예: "lookup_retailer")
        arguments: Tool 입력 인자 dict.
                   str 경로 또는 Path 객체 모두 허용.
                   Tool이 받지 않는 여분의 인자는 자동으로 제거.

    Returns:
        Tool callable의 반환값
          lookup_retailer  → LookupRetailerResult
          search_product   → SearchProductResult
          confirm_mapping  → None

    Raises:
        KeyError:  등록되지 않은 tool name
        TypeError: Path 파라미터에 str/Path/None 이외의 타입

    Logging:
        side_effects=True tool은 INFO 레벨로 기록 (파일 쓰기 추적용).
        side_effects=False tool은 DEBUG 레벨로 기록.
    """
    spec = get_tool(name)  # 없는 name이면 KeyError
    coerced = coerce_tool_arguments(spec, arguments)

    if spec.side_effects:
        log.info(
            "[tool_dispatch] %s — side_effects=True, idempotent=%s",
            name, spec.idempotent,
        )
    else:
        log.debug("[tool_dispatch] %s", name)

    return await spec.callable(**coerced)
