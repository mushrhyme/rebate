"""test_phase3_product_tool_use.py — Product Tool Use Runtime 테스트

검증 항목:
  1. product cache hit 유지 (기존 동작)
  2. product cache miss 시 search_product tool_use 발생
  3. Claude final JSON confirmed → confirmed_products 생성
  4. items[].product_code 반영
  5. product 후보 없음 → pending (fallback 아님)
  6. Claude final JSON not_found → pending (fallback 아님)
  7. search_product runtime error → ToolUseDispatchError → fallback
  8. product basis="cache" 저장 안 함
  9. product basis="tool_use" 저장함
  10. confirm_mapping이 product tool 목록에 없음
  11. Claude final JSON 파싱 테스트
  12. JSON 파싱 실패 → ToolUseParseError → fallback
  13. product_code 빈 값 → pending
  14. master_name 빈 값 → 경고 후 저장 가능
  15. product_code가 후보 목록에 없음 → pending
  16. search_product 정상 not_found → pending
  17. retailer Tool Use 기존 동작 회귀 방지

실행: pytest tests/test_phase3_product_tool_use.py -v
"""
import csv
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    ToolUseApiError,
    ToolUseDispatchError,
    ToolUseParseError,
    _build_product_decisions_with_tool_use,
    _build_product_decision_from_json,
    _build_product_tools,
    _execute_success_path,
    _parse_product_decision_json,
    _run_single_product_mapping,
)
from backend.pipeline.phase3_tool_result_adapter import ProductMappingDecision
from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def dirs(tmp_path: Path):
    mappings  = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()
    return tmp_path, mappings, form_defs


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _tb(tid, name, inp):
    b = MagicMock(); b.type = "tool_use"; b.id = tid; b.name = name; b.input = inp; return b

def _text(t):
    b = MagicMock(); b.type = "text"; b.text = t; return b

def _resp(stop, *blocks):
    r = MagicMock(); r.stop_reason = stop; r.content = list(blocks); return r


# ── 1. _build_product_tools() — confirm_mapping 제외 검증 ───────────────────

class TestBuildProductTools:
    def test_search_product_included(self):
        """search_product가 product tool 목록에 있다."""
        tools = _build_product_tools()
        names = {t["name"] for t in tools}
        assert "search_product" in names

    def test_confirm_mapping_excluded(self):
        """confirm_mapping은 product tool 목록에 없다 (의미 혼용 방지)."""
        tools = _build_product_tools()
        names = {t["name"] for t in tools}
        assert "confirm_mapping" not in names, (
            "confirm_mapping이 product tools에 포함됨 — 저장/결정 혼용 위험"
        )

    def test_only_search_product_in_tools(self):
        """product tool 목록에는 search_product 하나만 있다."""
        tools = _build_product_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "search_product"

    def test_no_path_fields_in_required(self):
        tools = {t["name"]: t for t in _build_product_tools()}
        for name, tool in tools.items():
            assert "mappings_dir" not in tool["input_schema"].get("required", [])


# ── 2. JSON 파싱 헬퍼 테스트 ─────────────────────────────────────────────────

class TestParseProductDecisionJson:
    def test_confirmed_decision_parsed(self):
        text = '{"decision": "confirmed", "product_code": "P001", "master_name": "辛ラーメン"}'
        data = _parse_product_decision_json(text)
        assert data["decision"]      == "confirmed"
        assert data["product_code"]  == "P001"
        assert data["master_name"]   == "辛ラーメン"

    def test_not_found_decision_parsed(self):
        text = '{"decision": "not_found", "reason": "후보 없음"}'
        data = _parse_product_decision_json(text)
        assert data["decision"] == "not_found"

    def test_code_fence_json_parsed(self):
        text = '```json\n{"decision": "confirmed", "product_code": "P001", "master_name": "辛ラーメン"}\n```'
        data = _parse_product_decision_json(text)
        assert data["product_code"] == "P001"

    def test_json_with_leading_text_parsed(self):
        text = '제품 매핑 완료:\n{"decision": "confirmed", "product_code": "P001", "master_name": "X"}'
        data = _parse_product_decision_json(text)
        assert data["decision"] == "confirmed"

    def test_invalid_json_raises_parse_error(self):
        with pytest.raises(ToolUseParseError, match="파싱 실패"):
            _parse_product_decision_json("not valid json")

    def test_missing_decision_field_raises_parse_error(self):
        with pytest.raises(ToolUseParseError, match="'decision' 필드"):
            _parse_product_decision_json('{"product_code": "P001"}')

    def test_empty_text_raises_parse_error(self):
        with pytest.raises(ToolUseParseError, match="빈 응답"):
            _parse_product_decision_json("")


# ── 3. Decision JSON → ProductMappingDecision 변환 ───────────────────────────

class TestBuildProductDecisionFromJson:
    def test_confirmed_returns_tool_use(self):
        data  = {"decision": "confirmed", "product_code": "P001", "master_name": "辛ラーメン"}
        d     = _build_product_decision_from_json("OCR名", data, {"P001"}, [])
        assert d.basis        == "tool_use"
        assert d.product_code == "P001"
        assert d.product_name == "辛ラーメン"

    def test_not_found_returns_pending(self):
        data = {"decision": "not_found", "reason": "후보 없음"}
        d    = _build_product_decision_from_json("OCR名", data, set(), [])
        assert d.basis        == "not_found"
        assert d.product_code is None

    def test_empty_product_code_returns_pending(self):
        """product_code 빈 값 → pending."""
        data = {"decision": "confirmed", "product_code": "", "master_name": "テスト"}
        d    = _build_product_decision_from_json("OCR名", data, set(), [])
        assert d.basis        == "not_found"
        assert d.product_code is None

    def test_product_code_not_in_candidates_returns_pending(self):
        """product_code가 후보 목록에 없음 → pending."""
        data = {"decision": "confirmed", "product_code": "P999", "master_name": "テスト"}
        d    = _build_product_decision_from_json("OCR名", data, {"P001", "P002"}, [])
        assert d.basis == "not_found"

    def test_product_code_in_candidates_returns_tool_use(self):
        """product_code가 후보 목록에 있음 → tool_use."""
        data = {"decision": "confirmed", "product_code": "P001", "master_name": "テスト"}
        d    = _build_product_decision_from_json("OCR名", data, {"P001"}, [])
        assert d.basis        == "tool_use"
        assert d.product_code == "P001"

    def test_empty_valid_codes_allows_any_code(self):
        """valid_codes 비어 있으면 어떤 코드도 허용."""
        data = {"decision": "confirmed", "product_code": "P999", "master_name": "テスト"}
        d    = _build_product_decision_from_json("OCR名", data, set(), [])
        # valid_codes가 비어 있으므로 검증 스킵
        assert d.basis        == "tool_use"
        assert d.product_code == "P999"

    def test_empty_master_name_no_candidates_returns_pending(self):
        """master_name 비어 있고 후보 목록도 없으면 → pending (빈 이름으로 저장 금지)."""
        data = {"decision": "confirmed", "product_code": "P001", "master_name": ""}
        d    = _build_product_decision_from_json("OCR名", data, {"P001"}, candidates=[])
        assert d.basis        == "not_found"
        assert d.product_code is None

    def test_empty_master_name_supplemented_from_candidates(self):
        """master_name 비어 있어도 후보에 이름이 있으면 보완해서 저장한다."""
        data       = {"decision": "confirmed", "product_code": "P001", "master_name": ""}
        candidates = [{"product_code": "P001", "product_name": "辛ラーメン 120g", "similarity": 0.9}]
        d          = _build_product_decision_from_json("OCR名", data, {"P001"}, candidates)
        assert d.basis        == "tool_use"
        assert d.product_name == "辛ラーメン 120g"
        assert d.product_code == "P001"


# ── 4. _run_single_product_mapping — Claude 루프 ─────────────────────────────

class TestRunSingleProductMapping:
    async def test_claude_json_confirmed_returns_tool_use(self, dirs):
        """Claude가 search_product 후 JSON confirmed 응답 → tool_use decision."""
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "search_product", {"ocr_name": "農心 辛ラーメン"})),
            _resp("end_turn",
                _text('{"decision": "confirmed", "product_code": "P001", "master_name": "辛ラーメン 120g"}')),
        ])

        result = await _run_single_product_mapping(
            "農心 辛ラーメン", [], mappings, client=mock_client
        )
        assert result.basis        == "tool_use"
        assert result.product_code == "P001"
        assert result.product_name == "辛ラーメン 120g"

    async def test_claude_json_not_found_returns_pending(self, dirs):
        """Claude가 JSON not_found 응답 → pending (fallback 아님)."""
        _, mappings, _ = dirs

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "search_product", {"ocr_name": "存在しない製品"})),
            _resp("end_turn",
                _text('{"decision": "not_found", "reason": "해당 제품 없음"}')),
        ])

        result = await _run_single_product_mapping(
            "存在しない製品", [], mappings, client=mock_client
        )
        assert result.basis        == "not_found"
        assert result.product_code is None

    def test_confirm_mapping_tool_not_in_product_tools(self):
        """product Tool Use 루프에서 confirm_mapping이 허용 tool 목록에 없다."""
        # _PRODUCT_ALLOWED_TOOLS를 직접 확인
        from backend.pipeline.phase3_fallback import _PRODUCT_ALLOWED_TOOLS
        assert "confirm_mapping" not in _PRODUCT_ALLOWED_TOOLS
        assert _PRODUCT_ALLOWED_TOOLS == frozenset({"search_product"})

    async def test_invalid_tool_call_is_blocked(self, dirs):
        """Claude가 confirm_mapping 등 비허용 tool을 호출하면 차단된다."""
        _, mappings, _ = dirs

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "confirm_mapping", {
                    "mapping_type": "product", "ocr_name": "テスト", "confirmed_code": "P001"
                })),
            _resp("end_turn", _text('{"decision": "not_found", "reason": "불가"}')),
        ])

        result = await _run_single_product_mapping(
            "テスト", [], mappings, client=mock_client
        )
        assert result.basis == "not_found"

    async def test_malformed_json_in_final_text_raises_parse_error(self, dirs):
        """end_turn에서 JSON 파싱 실패 → ToolUseParseError → fallback."""
        _, mappings, _ = dirs

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("end_turn", _text("이것은 JSON이 아닙니다")),
        ])

        with pytest.raises(ToolUseParseError):
            await _run_single_product_mapping(
                "テスト", [], mappings, client=mock_client
            )

    async def test_search_product_runtime_error_raises_dispatch_error(self, dirs):
        """search_product tool 실행 오류 → ToolUseDispatchError → fallback."""
        _, mappings, _ = dirs

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "search_product", {"ocr_name": "テスト"})),
        ])

        with patch("backend.pipeline.phase3_fallback.dispatch_tool_call",
                   side_effect=RuntimeError("dispatch failed")):
            with pytest.raises(ToolUseDispatchError, match="search_product 실행 오류"):
                await _run_single_product_mapping(
                    "テスト", [], mappings, client=mock_client
                )

    async def test_max_turns_exceeded_returns_pending(self, dirs):
        """max_turns 초과 → pending (fallback 아님)."""
        _, mappings, _ = dirs

        # always tool_use, no end_turn
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb(f"tu_{i}", "search_product", {"ocr_name": "テスト"}))
            for i in range(5)
        ])

        result = await _run_single_product_mapping(
            "テスト", [], mappings, client=mock_client, max_turns=2
        )
        assert result.basis == "not_found"


# ── 5. _build_product_decisions_with_tool_use ────────────────────────────────

class TestBuildProductDecisions:
    async def test_cache_hit_no_tool_use(self, dirs):
        """캐시 히트 → Tool Use 미발생."""
        _, mappings, _ = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心 辛ラーメン", "product_code": "P001", "product_name": ""},
        ])
        mock_client = AsyncMock()

        decisions = await _build_product_decisions_with_tool_use(
            ["農心 辛ラーメン"], mappings, product_client=mock_client
        )
        mock_client.messages.create.assert_not_called()
        assert decisions[0].basis == "cache"

    async def test_candidate_with_client_calls_tool_use(self, dirs):
        """후보 있음 + client → _run_single_product_mapping 호출."""
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])

        call_count = [0]

        async def mock_single(ocr_name, candidates, mappings_dir, *, client, max_turns, **kw):
            call_count[0] += 1
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code="P001",
                product_name="辛ラーメン", basis="tool_use", confidence=1.0
            )

        with patch("backend.pipeline.phase3_fallback._run_single_product_mapping", mock_single):
            decisions = await _build_product_decisions_with_tool_use(
                ["農心 辛ラーメン"], mappings, product_client=AsyncMock()
            )

        assert call_count[0] == 1
        assert decisions[0].basis == "tool_use"

    async def test_candidate_passed_to_single_mapping(self, dirs):
        """_run_single_product_mapping에 candidates가 전달된다."""
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])

        received_candidates: list = []

        async def capture(ocr_name, candidates, mappings_dir, *, client, max_turns, **kw):
            received_candidates.extend(candidates)
            return ProductMappingDecision(ocr_name=ocr_name, product_code=None,
                                          product_name="", basis="not_found", confidence=0.0)

        with patch("backend.pipeline.phase3_fallback._run_single_product_mapping", capture):
            await _build_product_decisions_with_tool_use(
                ["農心 辛ラーメン"], mappings, product_client=AsyncMock()
            )

        # search_product로 찾은 후보가 전달됨
        assert len(received_candidates) > 0

    async def test_candidate_no_client_returns_pending_with_warning(self, dirs, caplog):
        """후보 있지만 client=None → pending + 경고 로그."""
        import logging
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])

        with caplog.at_level(logging.WARNING):
            decisions = await _build_product_decisions_with_tool_use(
                ["農心 辛ラーメン"], mappings, product_client=None
            )

        assert decisions[0].basis == "not_found"
        assert any("client 없음" in r.message for r in caplog.records), (
            "경고 로그가 출력되지 않음"
        )

    async def test_normal_not_found_returns_pending(self, dirs):
        """unit_price.csv 후보 없음 → pending (fallback 아님)."""
        _, mappings, _ = dirs
        mock_client = AsyncMock()

        decisions = await _build_product_decisions_with_tool_use(
            ["존재하지않는제품ZZZZ"], mappings, product_client=mock_client
        )
        mock_client.messages.create.assert_not_called()
        assert decisions[0].basis == "not_found"

    async def test_search_product_runtime_error_raises_dispatch_error(self, dirs):
        """search_product 자체의 runtime 오류 → ToolUseDispatchError → fallback."""
        _, mappings, _ = dirs

        with patch("backend.pipeline.phase3_fallback.search_product",
                   side_effect=RuntimeError("IO error")):
            with pytest.raises(ToolUseDispatchError, match="search_product 실행 오류"):
                await _build_product_decisions_with_tool_use(
                    ["テスト"], mappings, product_client=AsyncMock()
                )

    async def test_api_error_propagates_as_tool_use_api_error(self, dirs):
        """API 오류 → ToolUseApiError → fallback."""
        import anthropic as _ant, httpx
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "テスト", "시키리": "100", "본부장": "90"},
        ])

        req  = httpx.Request("POST", "https://api.anthropic.com")
        resp = httpx.Response(529, request=req)
        api_err = _ant.InternalServerError("overloaded", response=resp, body=None)

        async def fail_run(*args, **kwargs):
            raise api_err

        with patch("backend.pipeline.phase3_fallback._run_single_product_mapping", fail_run):
            with pytest.raises(ToolUseApiError):
                await _build_product_decisions_with_tool_use(
                    ["テスト"], mappings, product_client=AsyncMock()
                )


# ── 6. confirmed_products + items 반영 ──────────────────────────────────────

class TestProductResultInOutput:
    async def test_tool_use_product_goes_to_confirmed_products(self, dirs):
        """tool_use basis product → confirmed_products 기록."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "農心 辛ラーメン",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="農心 辛ラーメン", product_code="P001",
            product_name="辛ラーメン 120g", basis="tool_use", confidence=1.0
        )]

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            result, _ = await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2,
                output_dir=tmp_path, mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        entry = result["confirmed_products"].get("農心 辛ラーメン")
        assert entry is not None
        assert entry["code"]  == "P001"
        assert entry["basis"] == "tool_use"

    async def test_product_code_applied_to_items(self, dirs):
        """product_code가 items[].product_code에 반영된다."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "農心 辛ラーメン",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="農心 辛ラーメン", product_code="P001",
            product_name="辛ラーメン", basis="tool_use", confidence=1.0
        )]

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            result, _ = await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2,
                output_dir=tmp_path, mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        item = next(i for i in result["items"] if i.get("product") == "農心 辛ラーメン")
        assert item["product_code"] == "P001"

    async def test_not_found_product_goes_to_pending(self, dirs):
        """not_found product → pending (fallback 아님)."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "未知の製品",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="未知の製品", product_code=None,
            product_name="", basis="not_found", confidence=0.0
        )]

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            _, pending = await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2,
                output_dir=tmp_path, mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        product_pending = [p for p in pending if p.get("mapping_type") == "product"]
        assert len(product_pending) == 1


# ── 7. confirm_mapping 저장 정책 ─────────────────────────────────────────────

class TestProductConfirmMappingPolicy:
    async def test_cache_basis_no_confirm_mapping(self, dirs):
        """basis='cache' → confirm_mapping 미호출."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "農心 辛ラーメン",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="農心 辛ラーメン", product_code="P001",
            product_name="辛ラーメン", basis="cache", confidence=1.0
        )]
        confirm_calls: list = []

        async def capture(**kwargs):
            if kwargs.get("mapping_type") == "product":
                confirm_calls.append(kwargs)

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", capture):
            await _execute_success_path(
                batch_result=None, doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        assert len(confirm_calls) == 0, "cache basis product에 confirm_mapping 호출됨"

    async def test_tool_use_basis_calls_confirm_mapping_once(self, dirs):
        """basis='tool_use' → confirm_mapping 1회 호출."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "農心 辛ラーメン",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="農心 辛ラーメン", product_code="P001",
            product_name="辛ラーメン 120g", basis="tool_use", confidence=1.0
        )]
        confirm_calls: list = []

        async def capture(**kwargs):
            if kwargs.get("mapping_type") == "product":
                confirm_calls.append(kwargs)

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", capture):
            await _execute_success_path(
                batch_result=None, doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        assert len(confirm_calls) == 1
        assert confirm_calls[0]["confirmed_code"] == "P001"

    async def test_product_confirm_mapping_called_only_in_success_path(self, dirs):
        """_run_single_product_mapping 내부에서 confirm_mapping이 호출되지 않는다."""
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "テスト", "시키리": "100", "본부장": "90"},
        ])

        confirm_calls: list = []

        async def capture(**kwargs):
            confirm_calls.append(kwargs)

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp("tool_use",
                _tb("tu_1", "search_product", {"ocr_name": "テスト"})),
            _resp("end_turn",
                _text('{"decision": "confirmed", "product_code": "P001", "master_name": "テスト"}')),
        ])

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", capture):
            result = await _run_single_product_mapping(
                "テスト", [], mappings, client=mock_client
            )

        # _run_single_product_mapping 내부에서 confirm_mapping 미호출
        assert len(confirm_calls) == 0
        # 결정만 반환
        assert result.basis        == "tool_use"
        assert result.product_code == "P001"


# ── 8. retailer 기존 동작 회귀 방지 ─────────────────────────────────────────

class TestRetailerRegressionPrevent:
    def test_retailer_decisions_function_still_works(self, tmp_path):
        """product 변경 후 retailer 결정 로직이 정상 동작한다."""
        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        from backend.pipeline.phase3_fallback import _batch_result_to_retailer_decisions

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
            )
        ]
        decisions, _, _ = _batch_result_to_retailer_decisions(
            per_retailer,
            form_id="form_01", issuer_fingerprint="fp1",
            cached_dist={}, retail_user_rows=[],
        )
        assert decisions[0].retailer_code == "R001"

    async def test_retailer_confirm_mapping_unaffected(self, dirs):
        """product Tool Use 추가 후 retailer confirm_mapping 정책 불변."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        tmp_path, mappings, form_defs = dirs
        write_csv(mappings / "retail_user.csv", [
            {"소매처코드": "R001", "소매처명": "テスト",
             "판매처코드": "D001", "판매처명": "東日本"},
        ])

        per_retailer = [RetailerBatchResult(
            ocr_name="テスト店", success=True, confirmed_code="R001",
            lookup_basis="candidate", tool_call_count=2, lookup_call_count=1,
            confirm_call_count=1, turns_used=3, max_turns_hit=False, elapsed_ms=100.0,
        )]
        batch_result = BatchExperimentResult(
            scenario="success", batch_size=1,
            stats=BatchStats(
                batch_size=1, success_count=1, failure_count=0,
                max_turns_hit_count=0, not_found_count=0,
                total_tool_calls=2, total_lookup_calls=1, total_confirm_calls=1,
                total_turns=3, avg_turns=3.0, elapsed_ms=100.0,
            ),
            per_retailer=per_retailer,
        )
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "商品A", "item_type": "条件", "columns": {}},
        ]}

        retailer_confirms: list = []

        async def capture(**kwargs):
            if kwargs.get("mapping_type") == "retailer":
                retailer_confirms.append(kwargs)

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", capture), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        assert len(retailer_confirms) == 1
        assert retailer_confirms[0]["confirmed_code"] == "R001"


# ── 9. master_name 보완 + client None reason 신규 테스트 ─────────────────────

class TestMasterNameFallback:
    """master_name 보완 로직과 client=None reason 테스트."""

    def test_empty_master_name_with_candidate_name_supplemented(self):
        """master_name 비어 있어도 후보에 이름이 있으면 자동 보완된다."""
        data       = {"decision": "confirmed", "product_code": "P001", "master_name": ""}
        candidates = [{"product_code": "P001", "product_name": "辛ラーメン 120g", "similarity": 0.9}]
        d          = _build_product_decision_from_json("OCR名", data, {"P001"}, candidates)
        assert d.basis        == "tool_use"
        assert d.product_name == "辛ラーメン 120g"

    def test_empty_master_name_no_candidate_name_returns_pending(self):
        """master_name 비어 있고 후보에도 이름 없음 → pending (저장 금지)."""
        data       = {"decision": "confirmed", "product_code": "P001", "master_name": ""}
        candidates = [{"product_code": "P001", "product_name": "", "similarity": 0.9}]
        d          = _build_product_decision_from_json("OCR名", data, {"P001"}, candidates)
        assert d.basis        == "not_found"
        assert d.product_code is None

    def test_empty_master_name_no_matching_candidate_returns_pending(self):
        """master_name 비어 있고 같은 product_code 후보 없음 → pending."""
        data       = {"decision": "confirmed", "product_code": "P001", "master_name": ""}
        candidates = [{"product_code": "P999", "product_name": "別の製品", "similarity": 0.9}]
        d          = _build_product_decision_from_json("OCR名", data, {"P001"}, candidates)
        assert d.basis == "not_found"

    async def test_tool_use_stored_with_non_empty_code_and_name(self, dirs):
        """basis='tool_use' 저장 시 product_code와 master_name이 모두 non-empty임을 보장한다."""
        tmp_path, mappings, form_defs = dirs
        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "農心 辛ラーメン",
             "item_type": "条件", "columns": {}},
        ]}
        decisions = [ProductMappingDecision(
            ocr_name="農心 辛ラーメン", product_code="P001",
            product_name="辛ラーメン 120g",  # non-empty
            basis="tool_use", confidence=1.0
        )]

        stored_args: list = []

        async def capture(**kwargs):
            if kwargs.get("mapping_type") == "product":
                stored_args.append(kwargs)

        with patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=decisions)), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping", capture):
            await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
            )

        assert len(stored_args) == 1
        assert stored_args[0]["confirmed_code"]              # non-empty
        assert stored_args[0]["context"].get("product_name") # non-empty


class TestProductClientNoneReason:
    """product_client=None 케이스의 reason 명시 테스트."""

    async def test_client_none_with_candidate_has_error_reason(self, dirs):
        """후보 있지만 client=None이면 ProductMappingDecision.error에 reason이 있다."""
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "テスト製品", "시키리": "100", "본부장": "90"},
        ])

        decisions = await _build_product_decisions_with_tool_use(
            ["テスト製品"], mappings, product_client=None
        )

        assert len(decisions) == 1
        d = decisions[0]
        assert d.basis == "not_found"
        assert d.error is not None, "error reason이 None — 조용한 pending 방지 위반"
        assert "client" in d.error.lower() or "API" in d.error

    async def test_client_none_no_candidate_no_error_needed(self, dirs):
        """후보가 없으면 (정상 not_found) error reason 없어도 된다."""
        _, mappings, _ = dirs
        # unit_price.csv 없음 → not_found

        decisions = await _build_product_decisions_with_tool_use(
            ["존재하지않는제품"], mappings, product_client=None
        )

        assert decisions[0].basis == "not_found"
        # 이 케이스는 정상 not_found — error는 없어도 됨 (정책 위반 아님)

    async def test_client_none_candidate_warning_logged(self, dirs, caplog):
        """client=None + 후보 있음 → 경고 로그가 출력된다."""
        import logging
        _, mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "テスト製品", "시키리": "100", "본부장": "90"},
        ])

        with caplog.at_level(logging.WARNING):
            await _build_product_decisions_with_tool_use(
                ["テスト製品"], mappings, product_client=None
            )

        assert any("client 없음" in r.message for r in caplog.records), (
            "client=None 경고 로그가 없음"
        )
