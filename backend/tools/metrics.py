"""metrics.py — Tool 사용량 및 품질 Metrics Layer

Claude tool_use 전환 전에 Tool 사용량과 결과 품질을 측정 가능하게 만든다.
비즈니스 로직은 변경하지 않으며 observability만 추가한다.

공개 API:
  ToolMetrics           — Tool별 집계 데이터
  get_metrics()         — 메트릭 스냅샷 조회
  reset_metrics()       — 메트릭 초기화

내부 기록 함수 (mapping.py에서만 호출):
  _record_lookup_retailer()
  _record_search_product()
  _record_confirm_mapping_success()
  _record_confirm_mapping_failure()

집계 구조:
  calls    = success + failures + not_found  (lookup/search)
  calls    = success + failures              (confirm_mapping)
  cache_hits ⊆ success
"""
from dataclasses import dataclass, replace

__all__ = ["ToolMetrics", "get_metrics", "reset_metrics"]


@dataclass
class ToolMetrics:
    """Tool별 누적 메트릭.

    calls:      총 호출 횟수
    success:    결과를 찾은 호출 수 (cache_hit + bracket + candidate; confirm_mapping은 예외 없는 완료)
    failures:   예외가 발생한 호출 수
    cache_hits: success 중 캐시(ocr_*.csv) 또는 괄호코드 직접매칭으로 확정된 수
    not_found:  결과를 찾지 못한 호출 수 (lookup/search 전용; confirm_mapping은 항상 0)
    """
    calls: int = 0
    success: int = 0
    failures: int = 0
    cache_hits: int = 0
    not_found: int = 0


# ── 내부 Registry ─────────────────────────────────────────────────────────────

_METRICS: dict[str, ToolMetrics] = {
    "lookup_retailer": ToolMetrics(),
    "search_product":  ToolMetrics(),
    "confirm_mapping": ToolMetrics(),
}


# ── 공개 API ──────────────────────────────────────────────────────────────────

def get_metrics(tool_name: str | None = None) -> "dict[str, ToolMetrics] | ToolMetrics":
    """메트릭 스냅샷을 반환한다.

    Args:
        tool_name: Tool 이름. None이면 모든 Tool의 스냅샷 dict 반환.

    Returns:
        tool_name 지정 시: ToolMetrics 스냅샷 (복사본)
        tool_name=None 시: {name: ToolMetrics 스냅샷} dict

    Raises:
        KeyError: 등록되지 않은 tool_name
    """
    if tool_name is None:
        return {k: replace(v) for k, v in _METRICS.items()}
    if tool_name not in _METRICS:
        raise KeyError(f"알 수 없는 Tool: {tool_name!r}. 등록된 Tool: {sorted(_METRICS)}")
    return replace(_METRICS[tool_name])


def reset_metrics(tool_name: str | None = None) -> None:
    """메트릭을 초기화한다.

    Args:
        tool_name: Tool 이름. None이면 모든 Tool 초기화.

    Raises:
        KeyError: 등록되지 않은 tool_name
    """
    if tool_name is None:
        for k in _METRICS:
            _METRICS[k] = ToolMetrics()
    else:
        if tool_name not in _METRICS:
            raise KeyError(f"알 수 없는 Tool: {tool_name!r}")
        _METRICS[tool_name] = ToolMetrics()


# ── 내부 기록 함수 (mapping.py 전용) ─────────────────────────────────────────

def _record_lookup_retailer(
    basis: str,  # "cache" | "bracket_code" | "candidate" | "not_found"
) -> None:
    """lookup_retailer 결과를 기록한다.

    cache / bracket_code → cache_hits + success 증가
    candidate            → success 증가
    not_found            → not_found 증가
    """
    m = _METRICS["lookup_retailer"]
    m.calls += 1
    if basis in ("cache", "bracket_code"):
        m.cache_hits += 1
        m.success += 1
    elif basis == "candidate":
        m.success += 1
    else:  # "not_found"
        m.not_found += 1


def _record_search_product(
    basis: str,  # "cache" | "candidate" | "not_found"
) -> None:
    """search_product 결과를 기록한다."""
    m = _METRICS["search_product"]
    m.calls += 1
    if basis == "cache":
        m.cache_hits += 1
        m.success += 1
    elif basis == "candidate":
        m.success += 1
    else:  # "not_found"
        m.not_found += 1


def _record_confirm_mapping_success() -> None:
    """confirm_mapping 성공(예외 없음)을 기록한다."""
    m = _METRICS["confirm_mapping"]
    m.calls += 1
    m.success += 1


def _record_confirm_mapping_failure() -> None:
    """confirm_mapping 실패(예외 발생)를 기록한다."""
    m = _METRICS["confirm_mapping"]
    m.calls += 1
    m.failures += 1
