"""claude_retry.py — Claude API 호출 retry/backoff 유틸리티

Anthropic API는 일시적 오류(rate limit, 서버 과부하, 연결 실패)를 반환할 수 있다.
이 모듈은 exponential backoff + jitter 재시도 계층을 제공한다.

공개 API:
  call_with_retry()        — 동기 API 호출 (phase3.py의 asyncio.to_thread 내부)
  async_call_with_retry()  — 비동기 API 호출 (experiment tool_use loop)

Retry 대상 (일시적 오류):
  RateLimitError           — 429: API quota 초과
  APITimeoutError          — 요청 타임아웃 (APIConnectionError의 서브클래스)
  APIConnectionError       — 연결 실패 / 네트워크 오류
  APIStatusError (5xx)     — 서버 오류 (InternalServerError 등)

Retry 제외 (영속적 오류 — 재시도해도 의미 없음):
  AuthenticationError      — 401: API 키 오류
  PermissionDeniedError    — 403: 권한 없음
  BadRequestError          — 400: 잘못된 요청 / schema 오류
  UnprocessableEntityError — 422: 처리 불가 입력
  NotFoundError            — 404: 존재하지 않는 리소스
  ConflictError            — 409: 충돌
  APIStatusError (4xx)     — 기타 클라이언트 오류
  local validation/coercion 오류 — TypeError, ValueError 등
"""
import asyncio
import calendar
import logging
import random
import time
from email.utils import parsedate
from typing import Any, Awaitable, Callable

import anthropic

log = logging.getLogger(__name__)

__all__ = ["call_with_retry", "async_call_with_retry"]

# ── Retry 정책 ─────────────────────────────────────────────────────────────────

# 일시적 오류 — 재시도 대상
# APITimeoutError는 APIConnectionError의 서브클래스이므로 명시하지 않아도 포함되지만,
# 가독성을 위해 함께 열거한다.
_RETRYABLE_NETWORK = (
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)


def _is_retryable(exc: BaseException) -> bool:
    """재시도 가능한 예외인지 판별한다.

    - _RETRYABLE_NETWORK 예외 → True
    - APIStatusError 중 status_code >= 500 (5xx 서버 오류) → True
    - 그 외 (4xx, 로컬 오류 등) → False
    """
    if isinstance(exc, _RETRYABLE_NETWORK):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        try:
            return exc.response.status_code >= 500
        except AttributeError:
            return False
    return False


def _compute_delay(attempt: int, initial_delay: float, max_delay: float) -> float:
    """exponential backoff 지연 시간 계산 (+ jitter).

    base = min(initial_delay × 2^attempt, max_delay)
    jitter = uniform(0, min(base × 0.2, 1.0))
    반환값: base + jitter
    """
    base = min(initial_delay * (2 ** attempt), max_delay)
    jitter = random.uniform(0, min(base * 0.2, 1.0))
    return base + jitter


def _extract_retry_after(exc: BaseException, *, _now: float | None = None) -> float | None:
    """APIError response에서 Retry-After 헤더를 초 단위로 추출한다.

    지원 형식:
      - seconds:   "30"  → 30.0
      - HTTP-date: "Wed, 21 Oct 2015 07:28:00 GMT" → (date - now) seconds

    음수/0은 0.0으로 보정한다 (즉시 재시도).
    파싱 실패 시 None을 반환한다 (기존 backoff 사용).

    Args:
        exc:   APIError 인스턴스 (response.headers 접근)
        _now:  테스트용 현재 시각 override (기본값: time.time())
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    # httpx.Headers는 case-insensitive 조회 지원
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None

    raw = str(raw).strip()

    # ── 1. seconds 형식 ─────────────────────────────────────────────────────
    try:
        return max(0.0, float(raw))
    except (ValueError, TypeError):
        pass

    # ── 2. HTTP-date 형식 (RFC 7231 / RFC 2822) ─────────────────────────────
    try:
        parsed = parsedate(raw)
        if parsed is not None:
            target_ts = float(calendar.timegm(parsed))
            now_ts = _now if _now is not None else time.time()
            return max(0.0, target_ts - now_ts)
    except Exception:
        pass

    return None


def _get_retry_delay(
    exc: BaseException,
    attempt: int,
    initial_delay: float,
    max_delay: float,
) -> float:
    """retry 대기 시간 결정.

    Retry-After 헤더가 있으면 그 값을 max_delay로 cap해서 사용.
    없거나 파싱 실패 시 기존 exponential backoff (+ jitter) 사용.
    """
    ra = _extract_retry_after(exc)
    if ra is not None:
        return min(ra, max_delay)
    return _compute_delay(attempt, initial_delay, max_delay)


# ── 동기 버전 (phase3.py의 asyncio.to_thread 내부용) ──────────────────────────

def call_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    _sleep_fn: Callable[[float], None] | None = None,
    **kwargs: Any,
) -> Any:
    """동기 Claude API 호출에 exponential backoff + jitter 재시도를 적용한다.

    Args:
        fn:            호출할 함수 (예: client.messages.create)
        *args:         fn에 전달할 위치 인자
        max_retries:   최대 재시도 횟수 (기본값 5). 초과 시 마지막 예외 raise.
        initial_delay: 첫 재시도 전 대기 시간(초, 기본값 1.0)
        max_delay:     대기 시간 상한(초, 기본값 30.0)
        _sleep_fn:     sleep 구현체 (기본값: time.sleep, 테스트 시 교체 가능)
        **kwargs:      fn에 전달할 키워드 인자

    Returns:
        fn의 반환값

    Raises:
        마지막 retryable 예외 (max_retries 초과 시)
        non-retryable 예외 (즉시 raise)
    """
    if _sleep_fn is None:
        _sleep_fn = time.sleep

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt == max_retries:
                log.error(
                    "[retry] %s: max_retries(%d) 초과 — 최종 실패",
                    type(exc).__name__, max_retries,
                )
                break
            delay = _get_retry_delay(exc, attempt, initial_delay, max_delay)
            log.warning(
                "[retry] %s (attempt %d/%d) → %.2fs 후 재시도",
                type(exc).__name__, attempt + 1, max_retries, delay,
            )
            _sleep_fn(delay)

    assert last_exc is not None
    raise last_exc


# ── 비동기 버전 (experiment tool_use loop용) ──────────────────────────────────

async def async_call_with_retry(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    _sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> Any:
    """비동기 Claude API 호출에 exponential backoff + jitter 재시도를 적용한다.

    Args:
        fn:            호출할 async 함수 (예: async_client.messages.create)
        *args:         fn에 전달할 위치 인자
        max_retries:   최대 재시도 횟수 (기본값 5)
        initial_delay: 첫 재시도 전 대기 시간(초)
        max_delay:     대기 시간 상한(초)
        _sleep_fn:     async sleep 구현체 (기본값: asyncio.sleep, 테스트 시 교체 가능)
        **kwargs:      fn에 전달할 키워드 인자

    Returns:
        fn의 반환값

    Raises:
        마지막 retryable 예외 (max_retries 초과 시)
        non-retryable 예외 (즉시 raise)
    """
    if _sleep_fn is None:
        _sleep_fn = asyncio.sleep

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except BaseException as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt == max_retries:
                log.error(
                    "[retry] %s: max_retries(%d) 초과 — 최종 실패",
                    type(exc).__name__, max_retries,
                )
                break
            delay = _get_retry_delay(exc, attempt, initial_delay, max_delay)
            log.warning(
                "[retry] %s (attempt %d/%d) → %.2fs 후 재시도",
                type(exc).__name__, attempt + 1, max_retries, delay,
            )
            await _sleep_fn(delay)

    assert last_exc is not None
    raise last_exc
