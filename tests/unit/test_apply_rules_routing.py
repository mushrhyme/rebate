"""apply_rules 라우팅 게이트(방법 C·1-call) 결정적 분기 검증.

LLM 분류 정확도가 아니라, route_and_build 도구의 출력(boolean·block)에 따라
코드가 **결정적으로** 분기하는지를 본다. Claude 호출은 목킹한다.

검증:
  1. requires_block_change=false → 블록 미변경(unchanged), apply_block_update 호출 안 함
  2. true + 유효 block(기존과 다름) → apply_block_update 호출, 결과 반환
  3. true + block 없음 → 422 (무음 누락 대신 시끄럽게 실패 — true 편향)
  4. true + block==기존블록 → unchanged (파일 안 씀)
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.routes import form_manage
from backend.api.routes.form_manage import ChatMessage, ChatRequest, apply_rules

_ROOT = Path(__file__).resolve().parents[2]


def _mock_client(decision: dict):
    """messages.create가 route_and_build tool_use를 반환하는 AsyncAnthropic 목."""
    resp = SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=decision)])
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=resp)
    return client


def _settings():
    return MagicMock(
        form_definitions_dir=_ROOT / "form_definitions",
        workspace_root=_ROOT,
        anthropic_api_key="test-key",
    )


def _body():
    return ChatRequest(
        form_id="form_03",
        messages=[ChatMessage(role="user", content="条件 컬럼을 ÷100 해줘")],
    )


@pytest.mark.asyncio
async def _run(decision: dict, apply_mock: MagicMock):
    with patch.object(form_manage, "get_settings", return_value=_settings()), \
         patch.object(form_manage.anthropic, "AsyncAnthropic", return_value=_mock_client(decision)), \
         patch("backend.api.routes.forms.apply_block_update", apply_mock):
        return await apply_rules(_body(), user={"id": "tester"})


@pytest.mark.asyncio
async def test_false_skips_block():
    apply_mock = MagicMock()
    res = await _run({"requires_block_change": False, "reason": "추출 규칙이라 블록 불필요"}, apply_mock)
    assert res["unchanged"] is True
    assert "추출 규칙" in res["note"]
    apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_true_with_block_applies():
    apply_mock = MagicMock(return_value={"ok": True, "form_id": "form_03", "wiring": {}})
    new_block = {"form_id": "form_03", "label": "x", "net": {"formula_type": "expr", "expr": "shikiri - c1", "vars": {"c1": "条件"}}}
    res = await _run({"requires_block_change": True, "reason": "NET 수식 변경", "block": new_block}, apply_mock)
    apply_mock.assert_called_once()
    assert apply_mock.call_args[0][1] == new_block
    assert res["note"] == "NET 수식 변경"


@pytest.mark.asyncio
async def test_true_without_block_raises():
    from fastapi import HTTPException
    apply_mock = MagicMock()
    with pytest.raises(HTTPException) as ei:
        await _run({"requires_block_change": True, "reason": "바꿔야 함"}, apply_mock)
    assert ei.value.status_code == 422
    apply_mock.assert_not_called()


@pytest.mark.asyncio
async def test_true_identical_block_is_unchanged():
    from scripts.build_form_types import extract_config_block
    cur = extract_config_block((_ROOT / "form_definitions" / "form_03.md").read_text(encoding="utf-8"), "form_03.md")
    apply_mock = MagicMock()
    res = await _run({"requires_block_change": True, "reason": "동일", "block": cur}, apply_mock)
    assert res["unchanged"] is True
    apply_mock.assert_not_called()
