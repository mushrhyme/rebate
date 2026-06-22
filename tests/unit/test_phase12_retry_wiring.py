"""test_phase12_retry_wiring.py — Phase 1·2가 Claude 호출을 retry로 감싸는 패턴 검증.

배경: Phase 3은 claude_retry를 쓰지만 Phase 1·2는 raw AsyncAnthropic이라
동시 분석 시 Anthropic 429/overloaded로 분석이 실패할 수 있었다.
이제 두 phase 모두 `async_call_with_retry(_factory)` 형태로 감싼다.

이 테스트는 phase1/phase2가 채택한 '래핑 패턴 자체'를 검증한다:
  1. RateLimitError(429)는 재시도되어 결국 성공한다
  2. asyncio.TimeoutError(= wait_for per-attempt 타임아웃)는 재시도되지 않고
     즉시 전파된다 (기존 '타임아웃=실패' 의미 보존)
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
import anthropic

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.tools.claude_retry import async_call_with_retry  # noqa: E402


def _rate_limit_exc() -> anthropic.RateLimitError:
    resp = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.RateLimitError("rate limited", response=resp, body=None)


@pytest.mark.asyncio
async def test_factory_retries_on_rate_limit():
    """phase1/2 패턴: wait_for(create)를 감싼 팩토리가 429에 재시도해 성공."""
    calls = {"n": 0}

    async def _factory():
        # phase1의 _do_create / phase2의 _consume_with_timeout 자리에 해당
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limit_exc()
        return "ok"

    # sleep을 무력화해 빠르게
    out = await async_call_with_retry(_factory, _sleep_fn=lambda _d: asyncio.sleep(0))
    assert out == "ok"
    assert calls["n"] == 3  # 429 두 번 후 세 번째 성공


@pytest.mark.asyncio
async def test_factory_does_not_retry_on_timeout():
    """per-attempt 타임아웃(asyncio.TimeoutError)은 비재시도 — 즉시 실패."""
    calls = {"n": 0}

    async def _factory():
        calls["n"] += 1
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await async_call_with_retry(_factory, _sleep_fn=lambda _d: asyncio.sleep(0))
    assert calls["n"] == 1  # 재시도 없이 1회만
