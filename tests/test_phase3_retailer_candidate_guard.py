"""test_phase3_retailer_candidate_guard.py — retailer Tool Use 후보 외 코드 거부

실행: pytest tests/test_phase3_retailer_candidate_guard.py -v

배경 (2026-06-12 감사):
  운영 retailer Tool Use 루프(run_retailer_mapping_experiment)는 Claude가
  confirm_mapping에 넘긴 confirmed_code를 검증 없이 캡처해, lookup_retailer가
  반환한 적 없는 코드(hallucination)가 그대로 확정·캐시 기록될 수 있었다.
  tool_contracts.md의 "후보외거부" 계약을 루프에 실제로 구현하고 회귀 방지한다.

검증 항목:
  1. lookup이 반환한 코드로 confirm → 정상 캡처
  2. 후보 외 코드로 confirm → 거부(is_error), confirmed_code는 None 유지
  3. 거부 후 Claude가 올바른 코드로 재시도 → 캡처 성공
"""
import csv
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.experiments.phase3_tool_use_experiment import (  # noqa: E402
    run_retailer_mapping_experiment,
)
from backend.tools.metrics import reset_metrics  # noqa: E402


@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def dirs(tmp_path: Path):
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()
    # 캐시 히트로 lookup이 retailer_code "10101"을 반환하도록 구성
    with (mappings / "ocr_retailer.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ocr_name", "retailer_code", "retailer_name"])
        w.writeheader()
        w.writerow({"ocr_name": "イオン", "retailer_code": "10101", "retailer_name": "イオン"})
    return mappings, form_defs


def _tool_block(tool_id: str, name: str, input_dict: dict) -> MagicMock:
    b = MagicMock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = input_dict
    return b


def _text_block(text: str) -> MagicMock:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _resp(stop_reason: str, *blocks: MagicMock) -> MagicMock:
    r = MagicMock()
    r.stop_reason = stop_reason
    r.content = list(blocks)
    return r


def _client(*responses) -> MagicMock:
    c = MagicMock()
    c.messages.create = AsyncMock(side_effect=list(responses))
    return c


def _confirm_block(tool_id: str, code: str) -> MagicMock:
    return _tool_block(tool_id, "confirm_mapping", {
        "mapping_type": "retailer",
        "ocr_name": "イオン",
        "confirmed_code": code,
    })


_LOOKUP = lambda: _resp("tool_use", _tool_block("tu_1", "lookup_retailer", {"ocr_name": "イオン"}))  # noqa: E731


@pytest.mark.asyncio
async def test_confirm_with_looked_up_code_is_captured(dirs):
    mappings, form_defs = dirs
    client = _client(
        _LOOKUP(),
        _resp("tool_use", _confirm_block("tu_2", "10101")),
        _resp("end_turn", _text_block("매핑 완료")),
    )
    result = await run_retailer_mapping_experiment(
        ocr_name="イオン", form_id="form_01",
        mappings_dir=mappings, form_definitions_dir=form_defs,
        allow_side_effects=False, client=client,
    )
    assert result.confirmed_code == "10101"


@pytest.mark.asyncio
async def test_confirm_with_unlisted_code_is_rejected(dirs):
    mappings, form_defs = dirs
    client = _client(
        _LOOKUP(),
        _resp("tool_use", _confirm_block("tu_2", "99999")),  # lookup이 반환한 적 없는 코드
        _resp("end_turn", _text_block("매핑 불가")),
    )
    result = await run_retailer_mapping_experiment(
        ocr_name="イオン", form_id="form_01",
        mappings_dir=mappings, form_definitions_dir=form_defs,
        allow_side_effects=False, client=client,
    )
    assert result.confirmed_code is None

    # 거부가 is_error tool_result로 Claude에게 전달되었는지 확인
    third_call_messages = client.messages.create.call_args_list[2].kwargs["messages"]
    rejection_results = [
        tr for tr in third_call_messages[-1]["content"]
        if tr.get("is_error") and "후보 외 코드" in tr.get("content", "")
    ]
    assert rejection_results, "후보 외 코드 거부 tool_result가 전달되지 않음"


@pytest.mark.asyncio
async def test_rejected_then_retry_with_valid_code_succeeds(dirs):
    mappings, form_defs = dirs
    client = _client(
        _LOOKUP(),
        _resp("tool_use", _confirm_block("tu_2", "99999")),  # 1차: 후보 외 → 거부
        _resp("tool_use", _confirm_block("tu_3", "10101")),  # 2차: 올바른 코드
        _resp("end_turn", _text_block("매핑 완료")),
    )
    result = await run_retailer_mapping_experiment(
        ocr_name="イオン", form_id="form_01",
        mappings_dir=mappings, form_definitions_dir=form_defs,
        allow_side_effects=False, client=client,
    )
    assert result.confirmed_code == "10101"
