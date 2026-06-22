"""test_ocr_retry.py — OCR(Azure/httpx) 일시 장애 backoff 검증.

배경: OCR submit/poll가 raise_for_status만 하고 backoff가 없어, 동시 분석 시
Azure 429/5xx로 분석이 실패할 수 있었다. _send_with_retry로 재시도를 추가한다.

검증:
  1. 429는 재시도되어 결국 성공
  2. 5xx도 재시도 대상
  3. 네트워크 일시 오류(TransportError)는 재시도
  4. 비재시도 4xx(404)는 즉시 raise
  5. max_retries 초과 시 마지막 예외 raise
"""
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import backend.pipeline.ocr as ocr  # noqa: E402


def _resp(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://azure.example/analyze"))


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def _noop(_d):
        return None
    monkeypatch.setattr(ocr.asyncio, "sleep", _noop)


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    seq = [_resp(429), _resp(429), _resp(200)]
    calls = {"n": 0}

    async def _send():
        calls["n"] += 1
        return seq.pop(0)

    out = await ocr._send_with_retry(_send, what="submit")
    assert out.status_code == 200
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retries_on_503():
    seq = [_resp(503), _resp(200)]

    async def _send():
        return seq.pop(0)

    out = await ocr._send_with_retry(_send, what="poll")
    assert out.status_code == 200


@pytest.mark.asyncio
async def test_retries_on_transport_error():
    state = {"n": 0}

    async def _send():
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("boom")  # TransportError 서브클래스
        return _resp(200)

    out = await ocr._send_with_retry(_send, what="submit")
    assert out.status_code == 200
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_non_retryable_4xx_raises_immediately():
    calls = {"n": 0}

    async def _send():
        calls["n"] += 1
        return _resp(404)

    with pytest.raises(httpx.HTTPStatusError):
        await ocr._send_with_retry(_send, what="submit")
    assert calls["n"] == 1  # 재시도 없음


@pytest.mark.asyncio
async def test_exhausts_retries_then_raises(monkeypatch):
    monkeypatch.setattr(ocr, "_OCR_MAX_RETRIES", 2)

    async def _send():
        return _resp(429)

    with pytest.raises(httpx.HTTPStatusError):
        await ocr._send_with_retry(_send, what="submit")
