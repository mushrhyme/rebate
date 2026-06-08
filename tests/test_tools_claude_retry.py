"""test_tools_claude_retry.py — Retry/Backoff 유틸리티 단위 테스트

실행: pytest tests/test_tools_claude_retry.py -v
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from backend.tools.claude_retry import (
    _compute_delay,
    _extract_retry_after,
    _get_retry_delay,
    _is_retryable,
    async_call_with_retry,
    call_with_retry,
)


# ── 테스트용 exception factory ────────────────────────────────────────────────

def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_exc(status_code: int) -> anthropic.APIStatusError:
    """status_code 지정 가능한 APIStatusError 생성."""
    resp = httpx.Response(status_code, request=_req())
    cls_map = {
        400: anthropic.BadRequestError,
        401: anthropic.AuthenticationError,
        403: anthropic.PermissionDeniedError,
        404: anthropic.NotFoundError,
        409: anthropic.ConflictError,
        422: anthropic.UnprocessableEntityError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
    }
    cls = cls_map.get(status_code, anthropic.APIStatusError)
    return cls("test error", response=resp, body=None)


class _FakeConnectionError(anthropic.APIConnectionError):
    """테스트용: request 없이 생성 가능한 APIConnectionError."""
    def __init__(self, msg: str = "fake connection error"):
        Exception.__init__(self, msg)


class _FakeTimeoutError(anthropic.APITimeoutError):
    """테스트용: request 없이 생성 가능한 APITimeoutError."""
    def __init__(self, msg: str = "fake timeout"):
        Exception.__init__(self, msg)


# ── 1. _is_retryable() ────────────────────────────────────────────────────────

class TestIsRetryable:
    def test_rate_limit_error_is_retryable(self):
        assert _is_retryable(_status_exc(429)) is True

    def test_connection_error_is_retryable(self):
        assert _is_retryable(_FakeConnectionError()) is True

    def test_timeout_error_is_retryable(self):
        assert _is_retryable(_FakeTimeoutError()) is True

    def test_500_internal_server_error_is_retryable(self):
        assert _is_retryable(_status_exc(500)) is True

    def test_503_service_unavailable_is_retryable(self):
        """5xx APIStatusError는 모두 retry 대상이다."""
        resp = httpx.Response(503, request=_req())
        exc = anthropic.APIStatusError.__new__(anthropic.APIStatusError)
        exc.response = resp
        assert _is_retryable(exc) is True

    def test_authentication_error_not_retryable(self):
        assert _is_retryable(_status_exc(401)) is False

    def test_permission_denied_not_retryable(self):
        assert _is_retryable(_status_exc(403)) is False

    def test_bad_request_not_retryable(self):
        assert _is_retryable(_status_exc(400)) is False

    def test_unprocessable_entity_not_retryable(self):
        assert _is_retryable(_status_exc(422)) is False

    def test_not_found_not_retryable(self):
        assert _is_retryable(_status_exc(404)) is False

    def test_409_conflict_not_retryable(self):
        assert _is_retryable(_status_exc(409)) is False

    def test_local_type_error_not_retryable(self):
        """로컬 validation 오류(TypeError)는 재시도하지 않는다."""
        assert _is_retryable(TypeError("bad type")) is False

    def test_local_value_error_not_retryable(self):
        assert _is_retryable(ValueError("bad value")) is False

    def test_generic_exception_not_retryable(self):
        assert _is_retryable(Exception("generic")) is False


# ── 2. _compute_delay() ───────────────────────────────────────────────────────

class TestComputeDelay:
    def test_delay_increases_with_attempt(self):
        d0 = _compute_delay(0, initial_delay=1.0, max_delay=30.0)
        d1 = _compute_delay(1, initial_delay=1.0, max_delay=30.0)
        d2 = _compute_delay(2, initial_delay=1.0, max_delay=30.0)
        # jitter 포함이므로 엄격한 순서 대신 base 값 검사
        assert 1.0 <= d0 <= 1.5  # base=1.0 + jitter <= 0.2
        assert 2.0 <= d1 <= 2.5  # base=2.0
        assert 4.0 <= d2 <= 4.9  # base=4.0

    def test_delay_capped_at_max_delay(self):
        large_attempt = 20
        d = _compute_delay(large_attempt, initial_delay=1.0, max_delay=30.0)
        assert d <= 30.0 + 1.0  # max_delay + 최대 jitter

    def test_delay_is_positive(self):
        for attempt in range(5):
            d = _compute_delay(attempt, initial_delay=0.1, max_delay=5.0)
            assert d > 0


# ── 3. call_with_retry() — 동기 버전 ─────────────────────────────────────────

class TestCallWithRetry:
    def test_first_call_success(self):
        """첫 호출 성공 시 재시도 없이 반환한다."""
        fn = MagicMock(return_value="result")
        sleep_fn = MagicMock()

        result = call_with_retry(fn, max_retries=3, _sleep_fn=sleep_fn)

        assert result == "result"
        assert fn.call_count == 1
        sleep_fn.assert_not_called()

    def test_retry_on_connection_error_then_success(self):
        """연결 오류 후 성공하면 재시도 후 결과를 반환한다."""
        fn = MagicMock(side_effect=[_FakeConnectionError(), "success"])
        sleep_fn = MagicMock()

        result = call_with_retry(fn, max_retries=3, initial_delay=0.1, _sleep_fn=sleep_fn)

        assert result == "success"
        assert fn.call_count == 2
        sleep_fn.assert_called_once()  # 한 번만 대기

    def test_retry_on_rate_limit_then_success(self):
        """RateLimitError 후 성공하면 재시도 후 반환한다."""
        fn = MagicMock(side_effect=[_status_exc(429), _status_exc(429), "ok"])
        sleep_fn = MagicMock()

        result = call_with_retry(fn, max_retries=5, initial_delay=0.1, _sleep_fn=sleep_fn)

        assert result == "ok"
        assert fn.call_count == 3
        assert sleep_fn.call_count == 2

    def test_max_retries_exceeded_raises_last_exception(self):
        """max_retries 초과 시 마지막 예외를 raise한다."""
        exc = _FakeConnectionError("persistent failure")
        fn = MagicMock(side_effect=exc)
        sleep_fn = MagicMock()

        with pytest.raises(_FakeConnectionError, match="persistent failure"):
            call_with_retry(fn, max_retries=2, initial_delay=0.01, _sleep_fn=sleep_fn)

        assert fn.call_count == 3  # 최초 1회 + 재시도 2회
        assert sleep_fn.call_count == 2

    def test_non_retryable_raises_immediately(self):
        """non-retryable 예외는 재시도 없이 즉시 raise한다."""
        fn = MagicMock(side_effect=_status_exc(401))
        sleep_fn = MagicMock()

        with pytest.raises(anthropic.AuthenticationError):
            call_with_retry(fn, max_retries=5, _sleep_fn=sleep_fn)

        assert fn.call_count == 1
        sleep_fn.assert_not_called()

    def test_non_retryable_4xx_raises_immediately(self):
        """4xx BadRequest는 재시도하지 않는다."""
        fn = MagicMock(side_effect=_status_exc(400))
        sleep_fn = MagicMock()

        with pytest.raises(anthropic.BadRequestError):
            call_with_retry(fn, max_retries=3, _sleep_fn=sleep_fn)

        assert fn.call_count == 1

    def test_5xx_status_error_is_retried(self):
        """InternalServerError(500)는 재시도한다."""
        fn = MagicMock(side_effect=[_status_exc(500), "ok"])
        sleep_fn = MagicMock()

        result = call_with_retry(fn, max_retries=3, initial_delay=0.01, _sleep_fn=sleep_fn)

        assert result == "ok"
        assert fn.call_count == 2
        sleep_fn.assert_called_once()

    def test_local_type_error_not_retried(self):
        """로컬 TypeError(coercion 실패 등)는 재시도하지 않는다."""
        fn = MagicMock(side_effect=TypeError("wrong type"))
        sleep_fn = MagicMock()

        with pytest.raises(TypeError):
            call_with_retry(fn, max_retries=5, _sleep_fn=sleep_fn)

        assert fn.call_count == 1

    def test_sleep_called_between_retries(self):
        """retry 사이에 sleep이 호출된다."""
        fn = MagicMock(side_effect=[
            _FakeConnectionError(), _FakeConnectionError(), "ok"
        ])
        sleep_calls = []

        def record_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        call_with_retry(fn, max_retries=5, initial_delay=1.0, _sleep_fn=record_sleep)

        assert len(sleep_calls) == 2
        assert all(d > 0 for d in sleep_calls)
        # exponential: 두 번째 대기가 첫 번째보다 크거나 같아야 한다 (jitter 고려)
        # 엄격한 검사 대신 양수 검사만
        assert sleep_calls[0] >= 1.0  # base=1.0
        assert sleep_calls[1] >= 2.0  # base=2.0

    def test_kwargs_passed_to_fn(self):
        """fn에 kwargs가 정상 전달된다."""
        fn = MagicMock(return_value="ok")

        call_with_retry(fn, model="haiku", max_tokens=100, _sleep_fn=MagicMock())

        fn.assert_called_once_with(model="haiku", max_tokens=100)

    def test_args_passed_to_fn(self):
        """fn에 positional args가 정상 전달된다."""
        fn = MagicMock(return_value="ok")

        call_with_retry(fn, "arg1", "arg2", _sleep_fn=MagicMock())

        fn.assert_called_once_with("arg1", "arg2")


# ── 4. async_call_with_retry() — 비동기 버전 ─────────────────────────────────

class TestAsyncCallWithRetry:
    async def test_first_call_success(self):
        """첫 호출 성공 시 재시도 없이 반환한다."""
        fn = AsyncMock(return_value="async_result")
        sleep_fn = AsyncMock()

        result = await async_call_with_retry(fn, max_retries=3, _sleep_fn=sleep_fn)

        assert result == "async_result"
        assert fn.call_count == 1
        sleep_fn.assert_not_called()

    async def test_retry_on_connection_error_then_success(self):
        """비동기 연결 오류 후 성공."""
        fn = AsyncMock(side_effect=[_FakeConnectionError(), "async_ok"])
        sleep_fn = AsyncMock()

        result = await async_call_with_retry(
            fn, max_retries=3, initial_delay=0.01, _sleep_fn=sleep_fn
        )

        assert result == "async_ok"
        assert fn.call_count == 2
        sleep_fn.assert_called_once()

    async def test_max_retries_exceeded_raises_last_exception(self):
        """비동기 max_retries 초과 시 마지막 예외 raise."""
        fn = AsyncMock(side_effect=_FakeTimeoutError("timeout"))
        sleep_fn = AsyncMock()

        with pytest.raises(_FakeTimeoutError):
            await async_call_with_retry(
                fn, max_retries=2, initial_delay=0.01, _sleep_fn=sleep_fn
            )

        assert fn.call_count == 3
        assert sleep_fn.call_count == 2

    async def test_non_retryable_raises_immediately(self):
        """비동기 non-retryable 예외는 즉시 raise."""
        fn = AsyncMock(side_effect=_status_exc(401))
        sleep_fn = AsyncMock()

        with pytest.raises(anthropic.AuthenticationError):
            await async_call_with_retry(fn, max_retries=5, _sleep_fn=sleep_fn)

        assert fn.call_count == 1
        sleep_fn.assert_not_called()

    async def test_5xx_retried_async(self):
        """비동기: 5xx 오류는 재시도한다."""
        fn = AsyncMock(side_effect=[_status_exc(500), "ok"])
        sleep_fn = AsyncMock()

        result = await async_call_with_retry(
            fn, max_retries=3, initial_delay=0.01, _sleep_fn=sleep_fn
        )

        assert result == "ok"
        assert fn.call_count == 2

    async def test_4xx_not_retried_async(self):
        """비동기: 4xx 오류는 재시도하지 않는다."""
        fn = AsyncMock(side_effect=_status_exc(400))
        sleep_fn = AsyncMock()

        with pytest.raises(anthropic.BadRequestError):
            await async_call_with_retry(fn, max_retries=5, _sleep_fn=sleep_fn)

        assert fn.call_count == 1

    async def test_sleep_called_between_retries_async(self):
        """비동기 retry 사이에 sleep이 호출된다."""
        fn = AsyncMock(side_effect=[
            _FakeConnectionError(), _FakeConnectionError(), "ok"
        ])
        sleep_delays = []

        async def record_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        await async_call_with_retry(
            fn, max_retries=5, initial_delay=1.0, _sleep_fn=record_sleep
        )

        assert len(sleep_delays) == 2
        assert all(d > 0 for d in sleep_delays)

    async def test_kwargs_passed_to_async_fn(self):
        """비동기 fn에 kwargs가 정상 전달된다."""
        fn = AsyncMock(return_value="ok")

        await async_call_with_retry(
            fn, model="sonnet", max_tokens=1024, _sleep_fn=AsyncMock()
        )

        fn.assert_called_once_with(model="sonnet", max_tokens=1024)


# ── 5. Phase3 / Experiment 통합: retry가 연결됐는지 ───────────────────────────

class TestRetryIntegration:
    def test_phase3_uses_call_with_retry(self):
        """phase3._call_claude가 call_with_retry를 통해 Claude를 호출한다.

        call_with_retry를 patch해 실제 API 없이 동작을 검증한다.
        """
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text='{"retailers": [], "products": []}')]
        mock_message.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        )

        with patch(
            "backend.pipeline.phase3.call_with_retry",
            return_value=mock_message,
        ) as mock_retry:
            from backend.pipeline.phase3 import _call_claude
            import anthropic as ant
            client = MagicMock(spec=ant.Anthropic)

            _call_claude(client, system=[], user_payload={})

            # call_with_retry가 호출되었어야 한다
            mock_retry.assert_called_once()
            # 첫 번째 인자가 client.messages.create이어야 한다
            assert mock_retry.call_args.args[0] is client.messages.create

    async def test_experiment_uses_async_call_with_retry(self):
        """experiment가 async_call_with_retry를 통해 Claude를 호출한다."""
        from unittest.mock import MagicMock as MM
        from backend.experiments.phase3_tool_use_experiment import (
            run_retailer_mapping_experiment,
        )
        import tempfile

        # lookup_retailer → end_turn (tool_not_called 방지)
        def _tb(name):
            b = MM(); b.type = "tool_use"; b.id = "t1"; b.name = name
            b.input = {"ocr_name": "テスト"}; return b

        lookup_resp = MM()
        lookup_resp.stop_reason = "tool_use"
        lookup_resp.content = [_tb("lookup_retailer")]

        end_block = MM()
        end_block.type = "text"
        end_block.text = "완료"
        end_resp = MM()
        end_resp.stop_reason = "end_turn"
        end_resp.content = [end_block]

        with patch(
            "backend.experiments.phase3_tool_use_experiment.async_call_with_retry",
            new=AsyncMock(side_effect=[lookup_resp, end_resp]),
        ) as mock_retry:
            with tempfile.TemporaryDirectory() as td:
                mappings = Path(td) / "mappings"
                form_defs = Path(td) / "form_defs"
                mappings.mkdir()
                form_defs.mkdir()

                await run_retailer_mapping_experiment(
                    "テスト", "form_01", mappings, form_defs,
                    client=MM(),  # never called directly
                )

            # async_call_with_retry가 Claude 호출에 사용되었어야 한다 (2회: lookup + end)
            assert mock_retry.call_count >= 1


# ── 6. _extract_retry_after() ────────────────────────────────────────────────

def _rate_limit_exc_with_header(value: str) -> anthropic.RateLimitError:
    """retry-after 헤더를 포함한 RateLimitError 생성."""
    resp = httpx.Response(429, headers={"retry-after": value}, request=_req())
    return anthropic.RateLimitError("rate limited", response=resp, body=None)


class TestExtractRetryAfter:
    def test_seconds_integer_returns_float(self):
        """retry-after: 30 → 30.0"""
        exc = _rate_limit_exc_with_header("30")
        assert _extract_retry_after(exc) == 30.0

    def test_seconds_float_returns_float(self):
        """retry-after: 1.5 → 1.5"""
        exc = _rate_limit_exc_with_header("1.5")
        assert _extract_retry_after(exc) == 1.5

    def test_zero_seconds_returns_zero(self):
        """retry-after: 0 → 0.0 (즉시 재시도)"""
        exc = _rate_limit_exc_with_header("0")
        assert _extract_retry_after(exc) == 0.0

    def test_negative_seconds_clamped_to_zero(self):
        """retry-after: -5 → 0.0 (음수 보정)"""
        exc = _rate_limit_exc_with_header("-5")
        assert _extract_retry_after(exc) == 0.0

    def test_http_date_future_returns_positive_delay(self):
        """HTTP-date 형식: 미래 날짜 → 양수 delay."""
        from email.utils import formatdate
        fake_now = 1_700_000_000.0
        target_ts = fake_now + 45.0
        http_date = formatdate(timeval=target_ts, usegmt=True)

        exc = _rate_limit_exc_with_header(http_date)
        delay = _extract_retry_after(exc, _now=fake_now)

        assert delay is not None
        assert abs(delay - 45.0) < 1.0  # 반올림 오차 허용

    def test_http_date_past_clamped_to_zero(self):
        """HTTP-date 형식: 과거 날짜 → 0.0 (보정)"""
        from email.utils import formatdate
        fake_now = 1_700_000_100.0
        past_ts  = fake_now - 60.0  # 60초 전
        http_date = formatdate(timeval=past_ts, usegmt=True)

        exc = _rate_limit_exc_with_header(http_date)
        delay = _extract_retry_after(exc, _now=fake_now)

        assert delay == 0.0

    def test_invalid_header_returns_none(self):
        """파싱 불가한 값 → None (기존 backoff 사용)"""
        exc = _rate_limit_exc_with_header("not-a-number-or-date")
        assert _extract_retry_after(exc) is None

    def test_no_response_returns_none(self):
        """response 없는 예외 → None"""
        exc = _FakeConnectionError("no response")
        assert _extract_retry_after(exc) is None

    def test_no_retry_after_header_returns_none(self):
        """retry-after 헤더 없음 → None"""
        resp = httpx.Response(429, request=_req())  # 헤더 없음
        exc = anthropic.RateLimitError("rate limited", response=resp, body=None)
        assert _extract_retry_after(exc) is None

    def test_5xx_exc_with_retry_after_header_extracted(self):
        """5xx 오류에도 retry-after 헤더가 있으면 추출한다."""
        resp = httpx.Response(503, headers={"retry-after": "10"}, request=_req())
        exc = anthropic.InternalServerError("overloaded", response=resp, body=None)
        assert _extract_retry_after(exc) == 10.0

    def test_connection_error_no_response_returns_none(self):
        """APIConnectionError(response 없음) → None"""
        exc = _FakeConnectionError()
        assert _extract_retry_after(exc) is None


# ── 7. _get_retry_delay() ─────────────────────────────────────────────────────

class TestGetRetryDelay:
    def test_uses_retry_after_when_present(self):
        """Retry-After 헤더가 있으면 그 값을 사용한다."""
        exc = _rate_limit_exc_with_header("20")
        delay = _get_retry_delay(exc, attempt=0, initial_delay=1.0, max_delay=30.0)
        assert delay == 20.0

    def test_retry_after_capped_at_max_delay(self):
        """Retry-After 값이 max_delay보다 크면 max_delay로 cap한다."""
        exc = _rate_limit_exc_with_header("999")
        delay = _get_retry_delay(exc, attempt=0, initial_delay=1.0, max_delay=30.0)
        assert delay == 30.0

    def test_falls_back_to_backoff_when_no_header(self):
        """Retry-After 헤더가 없으면 backoff를 사용한다."""
        exc = _FakeConnectionError()
        delay = _get_retry_delay(exc, attempt=0, initial_delay=1.0, max_delay=30.0)
        # backoff: base=1.0 + jitter
        assert 1.0 <= delay <= 1.5

    def test_falls_back_to_backoff_on_invalid_header(self):
        """Retry-After 헤더가 파싱 불가면 backoff를 사용한다."""
        exc = _rate_limit_exc_with_header("not-a-date-or-number")
        delay = _get_retry_delay(exc, attempt=0, initial_delay=1.0, max_delay=30.0)
        assert 1.0 <= delay <= 1.5

    def test_retry_after_zero_returns_zero(self):
        """Retry-After: 0 → delay=0.0 (cap 적용 안 함)."""
        exc = _rate_limit_exc_with_header("0")
        delay = _get_retry_delay(exc, attempt=0, initial_delay=1.0, max_delay=30.0)
        assert delay == 0.0


# ── 8. Retry-After 통합 — sync/async 루프 ────────────────────────────────────

class TestRetryAfterIntegration:
    def test_rate_limit_uses_retry_after_header_sync(self):
        """sync: RateLimitError + retry-after → Retry-After 값으로 sleep."""
        exc = _rate_limit_exc_with_header("12")
        fn = MagicMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        result = call_with_retry(fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
                                 _sleep_fn=record_sleep)

        assert result == "ok"
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 12.0   # Retry-After 값 사용

    def test_rate_limit_retry_after_capped_sync(self):
        """sync: Retry-After 값이 max_delay 초과 → max_delay로 cap."""
        exc = _rate_limit_exc_with_header("9999")
        fn = MagicMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        call_with_retry(fn, max_retries=3, initial_delay=1.0, max_delay=25.0,
                        _sleep_fn=sleep_calls.append)

        assert sleep_calls[0] == 25.0

    def test_no_retry_after_uses_backoff_sync(self):
        """sync: Retry-After 없으면 backoff delay 사용."""
        exc = _FakeConnectionError()
        fn = MagicMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        call_with_retry(fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
                        _sleep_fn=sleep_calls.append)

        assert len(sleep_calls) == 1
        assert sleep_calls[0] >= 1.0  # backoff base=1.0

    async def test_rate_limit_uses_retry_after_header_async(self):
        """async: RateLimitError + retry-after → Retry-After 값으로 sleep."""
        exc = _rate_limit_exc_with_header("8")
        fn = AsyncMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        async def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        result = await async_call_with_retry(
            fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
            _sleep_fn=record_sleep,
        )

        assert result == "ok"
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 8.0

    async def test_rate_limit_retry_after_capped_async(self):
        """async: Retry-After 값이 max_delay 초과 → max_delay로 cap."""
        exc = _rate_limit_exc_with_header("9999")
        fn = AsyncMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        async def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        await async_call_with_retry(
            fn, max_retries=3, initial_delay=1.0, max_delay=20.0,
            _sleep_fn=record_sleep,
        )

        assert sleep_calls[0] == 20.0

    async def test_no_retry_after_uses_backoff_async(self):
        """async: Retry-After 없으면 backoff delay 사용."""
        exc = _FakeConnectionError()
        fn = AsyncMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        async def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        await async_call_with_retry(
            fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
            _sleep_fn=record_sleep,
        )

        assert len(sleep_calls) == 1
        assert sleep_calls[0] >= 1.0

    async def test_invalid_retry_after_falls_back_to_backoff_async(self):
        """async: invalid Retry-After → backoff 사용."""
        exc = _rate_limit_exc_with_header("garbage-date")
        fn = AsyncMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        async def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        await async_call_with_retry(
            fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
            _sleep_fn=record_sleep,
        )

        assert sleep_calls[0] >= 1.0  # backoff, not retry-after

    async def test_5xx_retry_after_header_used_async(self):
        """async: 5xx 오류에 Retry-After 헤더 있으면 사용한다."""
        resp = httpx.Response(503, headers={"retry-after": "5"}, request=_req())
        exc = anthropic.InternalServerError("overloaded", response=resp, body=None)
        fn = AsyncMock(side_effect=[exc, "ok"])
        sleep_calls: list[float] = []

        async def record_sleep(d: float) -> None:
            sleep_calls.append(d)

        await async_call_with_retry(
            fn, max_retries=3, initial_delay=1.0, max_delay=30.0,
            _sleep_fn=record_sleep,
        )

        assert sleep_calls[0] == 5.0

    def test_auth_error_still_not_retried_with_retry_after(self):
        """401 AuthenticationError는 Retry-After가 있어도 재시도하지 않는다."""
        resp = httpx.Response(401, headers={"retry-after": "60"}, request=_req())
        exc = anthropic.AuthenticationError("bad key", response=resp, body=None)
        fn = MagicMock(side_effect=exc)
        sleep_fn = MagicMock()

        with pytest.raises(anthropic.AuthenticationError):
            call_with_retry(fn, max_retries=5, _sleep_fn=sleep_fn)

        assert fn.call_count == 1
        sleep_fn.assert_not_called()
