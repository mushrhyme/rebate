"""test_phase3_tool_result_adapter.py — Tool Use 결과 → phase3 출력 변환 테스트

검증 항목:
  1. retailer cache hit → confirmed_retailers 엔트리 생성
  2. retailer not_found → pending 엔트리 생성
  3. Claude tool_use 결정 → "tool_use" basis로 confirmed
  4. confidence/source/basis 필드 보존
  5. malformed input → ToolUseContractError
  6. adapter는 confirm_mapping 호출 없음
  7. adapter는 파일을 쓰지 않음
  8. Golden contract: legacy phase3 result와 동일한 key 구조

실행: pytest tests/test_phase3_tool_result_adapter.py -v
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_tool_result_adapter import (
    ProductMappingDecision,
    RetailerMappingDecision,
    ToolUseContractError,
    convert_tool_use_result_to_phase3_output,
    product_decision_from_search_result,
    retailer_decision_from_lookup_result,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

_MINIMAL_PHASE2 = {
    "pages": [],
    "items": [
        {
            "customer":   "テスト店舗A",
            "product":    "農心 辛ラーメン 120g",
            "item_type":  "条件",
            "columns":    {"金額": 1000},
            "source_pages": [2],
        },
    ],
}

_MINIMAL_PHASE2_MULTI = {
    "pages": [],
    "items": [
        {"customer": "テスト店舗A", "product": "商品A", "item_type": "条件", "columns": {}},
        {"customer": "テスト店舗B", "product": "商品B", "item_type": "条件", "columns": {}},
    ],
}


def _run(retailer_decisions=None, product_decisions=None, phase2=None):
    """共通ヘルパー: convert_tool_use_result_to_phase3_output を短縮形で呼ぶ。"""
    return convert_tool_use_result_to_phase3_output(
        doc_id="test_doc_001",
        form_id="form_01",
        hatsu_month="2025-01",
        issuer={"name": "テスト発行者"},
        phase2_result=phase2 or _MINIMAL_PHASE2,
        retailer_decisions=retailer_decisions or [],
        product_decisions=product_decisions or [],
    )


# ── 1. 기본 메타데이터 보존 ────────────────────────────────────────────────────

class TestResultMetadata:
    def test_doc_id_preserved(self):
        result, _ = _run()
        assert result["doc_id"] == "test_doc_001"

    def test_form_id_preserved(self):
        result, _ = _run()
        assert result["form_id"] == "form_01"

    def test_hatsu_month_preserved(self):
        result, _ = _run()
        assert result["hatsu_month"] == "2025-01"

    def test_issuer_preserved(self):
        result, _ = _run()
        assert result["issuer"] == {"name": "テスト発行者"}

    def test_result_has_all_required_keys(self):
        """legacy phase3 result와 동일한 최상위 key를 갖는다 (Golden Contract)."""
        result, _ = _run()
        required = {
            "doc_id", "form_id", "hatsu_month", "issuer",
            "confirmed_retailers", "confirmed_products",
            "items", "cover_totals",
        }
        assert required.issubset(result.keys())

    def test_pending_is_list(self):
        _, pending = _run()
        assert isinstance(pending, list)


# ── 2. Retailer Cache Hit ─────────────────────────────────────────────────────

class TestRetailerCacheHit:
    def test_cache_hit_goes_to_confirmed(self):
        """cache basis → confirmed_retailers 엔트리 생성."""
        r = RetailerMappingDecision(
            ocr_name="テスト店舗A",
            retailer_code="R001",
            dist_code="D001",
            basis="cache",
            confidence=1.0,
        )
        result, pending = _run([r])

        assert "テスト店舗A" in result["confirmed_retailers"]
        assert len(pending) == 0

    def test_cache_hit_fields_correct(self):
        r = RetailerMappingDecision(
            ocr_name="テスト店舗A",
            retailer_code="R001",
            dist_code="D001",
            basis="cache",
            confidence=1.0,
        )
        result, _ = _run([r])
        entry = result["confirmed_retailers"]["テスト店舗A"]
        assert entry["retailer_code"] == "R001"
        assert entry["dist_code"]     == "D001"
        assert entry["basis"]         == "cache"

    def test_bracket_code_hit_goes_to_confirmed(self):
        """bracket_code basis → confirmed_retailers 엔트리 생성."""
        r = RetailerMappingDecision(
            ocr_name="テスト店舗A",
            retailer_code="R001",
            dist_code="",
            basis="bracket_code",
            confidence=1.0,
        )
        result, pending = _run([r])
        assert "テスト店舗A" in result["confirmed_retailers"]
        assert result["confirmed_retailers"]["テスト店舗A"]["basis"] == "bracket_code"

    def test_tool_use_decision_goes_to_confirmed(self):
        """tool_use basis → confirmed_retailers 엔트리 생성."""
        r = RetailerMappingDecision(
            ocr_name="テスト店舗A",
            retailer_code="R001",
            dist_code="",
            basis="tool_use",
            confidence=0.85,
        )
        result, pending = _run([r])
        assert "テスト店舗A" in result["confirmed_retailers"]
        assert result["confirmed_retailers"]["テスト店舗A"]["basis"] == "tool_use"
        assert len(pending) == 0


# ── 3. Retailer Not Found → Pending ──────────────────────────────────────────

class TestRetailerNotFound:
    def test_not_found_goes_to_pending(self):
        """not_found basis → pending 엔트리 생성."""
        r = RetailerMappingDecision(
            ocr_name="未知の店舗",
            retailer_code=None,
            dist_code="",
            basis="not_found",
            confidence=0.0,
        )
        result, pending = _run([r])
        assert "未知の店舗" not in result["confirmed_retailers"]
        assert len(pending) == 1
        assert pending[0]["mapping_type"] == "retailer"
        assert pending[0]["ocrName"]      == "未知の店舗"

    def test_error_basis_goes_to_pending(self):
        """error basis → pending 엔트리 생성."""
        r = RetailerMappingDecision(
            ocr_name="エラー店舗",
            retailer_code=None,
            dist_code="",
            basis="error",
            confidence=0.0,
            error="dispatch failed",
        )
        _, pending = _run([r])
        assert any(p["ocrName"] == "エラー店舗" for p in pending)

    def test_pending_has_candidates(self):
        """pending에 후보 목록이 포함된다."""
        candidates = [{"retailer_code": "R999", "retailer_name": "候補店", "similarity": 0.7}]
        r = RetailerMappingDecision(
            ocr_name="未知の店舗",
            retailer_code=None,
            dist_code="",
            basis="not_found",
            confidence=0.0,
            candidates=candidates,
            page_number=3,
        )
        _, pending = _run([r])
        p = pending[0]
        assert p["candidates"] == candidates
        assert p["page_number"] == 3

    def test_pending_structure_matches_legacy(self):
        """pending 항목의 key 구조가 legacy phase3와 동일하다 (Golden Contract)."""
        r = RetailerMappingDecision(
            ocr_name="未知",
            retailer_code=None,
            dist_code="",
            basis="not_found",
            confidence=0.0,
        )
        _, pending = _run([r])
        required = {"mapping_type", "ocrName", "candidates", "page_number"}
        assert required.issubset(pending[0].keys())


# ── 4. Product Mapping ────────────────────────────────────────────────────────

class TestProductMapping:
    def test_product_cache_hit_goes_to_confirmed(self):
        p = ProductMappingDecision(
            ocr_name="農心 辛ラーメン 120g",
            product_code="P001",
            product_name="辛ラーメン 120g",
            basis="cache",
            confidence=1.0,
        )
        result, pending = _run(product_decisions=[p])
        assert "農心 辛ラーメン 120g" in result["confirmed_products"]
        entry = result["confirmed_products"]["農心 辛ラーメン 120g"]
        assert entry["code"]        == "P001"
        assert entry["master_name"] == "辛ラーメン 120g"
        assert entry["basis"]       == "cache"
        assert len(pending) == 0

    def test_product_tool_use_decision_goes_to_confirmed(self):
        p = ProductMappingDecision(
            ocr_name="農心 辛ラーメン",
            product_code="P001",
            product_name="辛ラーメン",
            basis="tool_use",
            confidence=0.9,
        )
        result, _ = _run(product_decisions=[p])
        assert "農心 辛ラーメン" in result["confirmed_products"]
        assert result["confirmed_products"]["農心 辛ラーメン"]["basis"] == "tool_use"

    def test_product_not_found_goes_to_pending(self):
        p = ProductMappingDecision(
            ocr_name="未知の製品",
            product_code=None,
            product_name="",
            basis="not_found",
            confidence=0.0,
        )
        result, pending = _run(product_decisions=[p])
        assert "未知の製品" not in result["confirmed_products"]
        product_pendings = [x for x in pending if x["mapping_type"] == "product"]
        assert len(product_pendings) == 1
        assert product_pendings[0]["ocrName"] == "未知の製品"


# ── 5. Items 매핑 적용 ────────────────────────────────────────────────────────

class TestItemsMapping:
    def test_items_get_retailer_code_applied(self):
        """confirmed retailer → items에 retailer_code 적용."""
        r = RetailerMappingDecision(
            ocr_name="テスト店舗A",
            retailer_code="R001",
            dist_code="D001",
            basis="cache",
            confidence=1.0,
        )
        result, _ = _run([r])
        item = next(i for i in result["items"] if i.get("customer") == "テスト店舗A")
        assert item["retailer_code"] == "R001"
        assert item["dist_code"]     == "D001"
        assert item["unconfirmed"]   is False

    def test_unconfirmed_flag_set_for_missing_retailer(self):
        """매핑 안 된 customer → unconfirmed=True."""
        result, _ = _run([])   # no retailer decisions
        item = result["items"][0]
        assert item["unconfirmed"] is True
        assert item["retailer_code"] == ""

    def test_items_get_product_code_applied(self):
        p = ProductMappingDecision(
            ocr_name="農心 辛ラーメン 120g",
            product_code="P001",
            product_name="辛ラーメン",
            basis="cache",
            confidence=1.0,
        )
        result, _ = _run(product_decisions=[p])
        item = next(i for i in result["items"] if i.get("product") == "農心 辛ラーメン 120g")
        assert item["product_code"] == "P001"

    def test_items_count_preserved(self):
        """items 수는 phase2_result와 동일하게 유지된다."""
        result, _ = _run(phase2=_MINIMAL_PHASE2_MULTI)
        assert len(result["items"]) == 2


# ── 6. 복수 결과 처리 ─────────────────────────────────────────────────────────

class TestMultipleDecisions:
    def test_mixed_confirmed_and_pending(self):
        decisions = [
            RetailerMappingDecision("店舗A", "R001", "D001", "cache",    1.0),
            RetailerMappingDecision("店舗B", None,   "",     "not_found", 0.0),
            RetailerMappingDecision("店舗C", "R003", "",     "tool_use",  0.9),
        ]
        result, pending = _run(decisions)
        assert "店舗A" in result["confirmed_retailers"]
        assert "店舗C" in result["confirmed_retailers"]
        assert "店舗B" not in result["confirmed_retailers"]
        assert len(pending) == 1
        assert pending[0]["ocrName"] == "店舗B"

    def test_all_confirmed(self):
        decisions = [
            RetailerMappingDecision("店舗A", "R001", "", "cache", 1.0),
            RetailerMappingDecision("店舗B", "R002", "", "cache", 1.0),
        ]
        result, pending = _run(decisions)
        assert len(result["confirmed_retailers"]) == 2
        assert len(pending) == 0

    def test_all_pending(self):
        decisions = [
            RetailerMappingDecision("店舗A", None, "", "not_found", 0.0),
            RetailerMappingDecision("店舗B", None, "", "not_found", 0.0),
        ]
        result, pending = _run(decisions)
        assert len(result["confirmed_retailers"]) == 0
        assert len(pending) == 2


# ── 7. Contract 위반 → ToolUseContractError ───────────────────────────────────

class TestContractValidation:
    def test_invalid_basis_raises(self):
        """알 수 없는 basis 값 → ToolUseContractError."""
        r = RetailerMappingDecision(
            ocr_name="テスト",
            retailer_code="R001",
            dist_code="",
            basis="INVALID_BASIS",
            confidence=1.0,
        )
        with pytest.raises(ToolUseContractError, match="basis"):
            _run([r])

    def test_mapped_basis_with_none_code_raises(self):
        """cache basis인데 retailer_code=None → ToolUseContractError."""
        r = RetailerMappingDecision(
            ocr_name="テスト",
            retailer_code=None,   # 잘못됨
            dist_code="",
            basis="cache",
            confidence=1.0,
        )
        with pytest.raises(ToolUseContractError, match="retailer_code가 None"):
            _run([r])

    def test_confidence_out_of_range_raises(self):
        """confidence > 1.0 → ToolUseContractError."""
        r = RetailerMappingDecision(
            ocr_name="テスト",
            retailer_code="R001",
            dist_code="",
            basis="cache",
            confidence=1.5,   # 잘못됨
        )
        with pytest.raises(ToolUseContractError, match="confidence"):
            _run([r])

    def test_negative_confidence_raises(self):
        r = RetailerMappingDecision(
            ocr_name="テスト",
            retailer_code=None,
            dist_code="",
            basis="not_found",
            confidence=-0.1,   # 잘못됨
        )
        with pytest.raises(ToolUseContractError, match="confidence"):
            _run([r])

    def test_product_invalid_basis_raises(self):
        p = ProductMappingDecision("製品A", "P001", "商品A", "WRONG", 1.0)
        with pytest.raises(ToolUseContractError):
            _run(product_decisions=[p])

    def test_product_mapped_with_none_code_raises(self):
        p = ProductMappingDecision("製品A", None, "", "cache", 1.0)
        with pytest.raises(ToolUseContractError):
            _run(product_decisions=[p])


# ── 8. Side-effect 없음 검증 ─────────────────────────────────────────────────

class TestNoSideEffects:
    def test_confirm_mapping_never_called(self):
        """adapter 실행 중 confirm_mapping이 호출되지 않는다."""
        r = RetailerMappingDecision("テスト", "R001", "", "cache", 1.0)

        with patch("backend.tools.mapping.confirm_mapping") as mock_confirm:
            _run([r])
            mock_confirm.assert_not_called()

    def test_no_file_written(self, tmp_path):
        """adapter 실행 후 어떤 파일도 생성/수정되지 않는다."""
        files_before = set(tmp_path.iterdir())

        r = RetailerMappingDecision("テスト", "R001", "", "cache", 1.0)
        # adapter가 tmp_path에 파일을 만들 수 없음 — write() 호출 금지를 patch로 강제
        with patch("builtins.open", wraps=open) as mock_open:
            _run([r])
            # open이 쓰기 모드로 호출되지 않았어야 한다
            write_calls = [
                c for c in mock_open.call_args_list
                if len(c.args) >= 2 and "w" in str(c.args[1])
            ]
            assert len(write_calls) == 0, (
                f"adapter가 파일을 쓰기 모드로 열었음: {write_calls}"
            )

        files_after = set(tmp_path.iterdir())
        assert files_before == files_after, "adapter가 새 파일을 생성했음"

    def test_no_asyncio_in_pure_function(self):
        """convert_tool_use_result_to_phase3_output은 async가 아닌 순수 함수다."""
        import asyncio
        r = RetailerMappingDecision("テスト", "R001", "", "cache", 1.0)
        result = _run([r])
        assert not asyncio.iscoroutine(result), "함수가 coroutine을 반환함 (async 불필요)"


# ── 9. 편의 생성자 테스트 ─────────────────────────────────────────────────────

class TestDecisionFactories:
    def test_retailer_decision_from_lookup_result_cache(self):
        """LookupRetailerResult(cache) → RetailerMappingDecision(cache)."""
        from backend.tools.mapping import LookupRetailerResult
        lr = LookupRetailerResult(retailer_code="R001", basis="cache", confidence=1.0)
        d = retailer_decision_from_lookup_result("テスト", lr, dist_code="D001")
        assert d.retailer_code == "R001"
        assert d.basis         == "cache"
        assert d.dist_code     == "D001"

    def test_retailer_decision_from_lookup_result_not_found(self):
        from backend.tools.mapping import LookupRetailerResult
        lr = LookupRetailerResult(retailer_code=None, basis="not_found", confidence=0.0)
        d = retailer_decision_from_lookup_result("未知", lr)
        assert d.retailer_code is None
        assert d.basis          == "not_found"

    def test_retailer_decision_candidate_with_claude_decision(self):
        """candidate + claude_decided_code → tool_use basis."""
        from backend.tools.mapping import LookupRetailerResult, RetailerCandidate
        lr = LookupRetailerResult(
            retailer_code=None,
            basis="candidate",
            confidence=0.85,
            candidates=[RetailerCandidate(
                retailer_code="R001", retailer_name="テスト",
                source="retail_user.csv", similarity=0.85
            )],
        )
        d = retailer_decision_from_lookup_result(
            "テスト", lr, claude_decided_code="R001"
        )
        assert d.retailer_code == "R001"
        assert d.basis          == "tool_use"

    def test_product_decision_from_search_result_cache(self):
        from backend.tools.mapping import SearchProductResult
        sr = SearchProductResult(product_code="P001", basis="cache", confidence=1.0)
        d = product_decision_from_search_result("農心", sr)
        assert d.product_code == "P001"
        assert d.basis        == "cache"

    def test_product_decision_candidate_with_claude_decision(self):
        from backend.tools.mapping import SearchProductResult, ProductCandidate
        sr = SearchProductResult(
            product_code=None,
            basis="candidate",
            confidence=0.8,
            candidates=[ProductCandidate(
                product_code="P001", product_name="辛ラーメン", similarity=0.8
            )],
        )
        d = product_decision_from_search_result(
            "辛ラーメン", sr,
            claude_decided_code="P001", claude_decided_name="辛ラーメン 120g"
        )
        assert d.product_code == "P001"
        assert d.basis        == "tool_use"


# ── 10. Golden Contract: legacy phase3 result key 구조 일치 ──────────────────

class TestGoldenContract:
    """legacy run_phase3()가 반환하는 result와 동일한 key 구조를 갖는지 검증."""

    # legacy phase3의 expected result keys
    _LEGACY_RESULT_KEYS = {
        "doc_id", "form_id", "hatsu_month", "issuer",
        "confirmed_retailers", "confirmed_products",
        "items", "cover_totals",
    }

    # legacy pending item의 expected keys
    _LEGACY_PENDING_KEYS = {"mapping_type", "ocrName", "candidates", "page_number"}

    # legacy confirmed_retailers 엔트리의 expected keys
    _LEGACY_CONFIRMED_RETAILER_KEYS = {"retailer_code", "dist_code", "basis"}

    # legacy confirmed_products 엔트리의 expected keys
    _LEGACY_CONFIRMED_PRODUCT_KEYS = {"code", "master_name", "basis"}

    def test_result_top_level_keys(self):
        result, _ = _run()
        assert self._LEGACY_RESULT_KEYS.issubset(result.keys())

    def test_confirmed_retailer_entry_keys(self):
        r = RetailerMappingDecision("店舗A", "R001", "D001", "cache", 1.0)
        result, _ = _run([r])
        entry = result["confirmed_retailers"]["店舗A"]
        assert self._LEGACY_CONFIRMED_RETAILER_KEYS.issubset(entry.keys())

    def test_confirmed_product_entry_keys(self):
        p = ProductMappingDecision("商品A", "P001", "辛ラーメン", "cache", 1.0)
        result, _ = _run(product_decisions=[p])
        entry = result["confirmed_products"]["商品A"]
        assert self._LEGACY_CONFIRMED_PRODUCT_KEYS.issubset(entry.keys())

    def test_pending_entry_keys(self):
        r = RetailerMappingDecision("未知", None, "", "not_found", 0.0)
        _, pending = _run([r])
        assert self._LEGACY_PENDING_KEYS.issubset(pending[0].keys())

    def test_items_have_retailer_and_product_fields(self):
        """items의 각 엔트리가 _apply_mappings에서 추가하는 필드를 갖는다."""
        result, _ = _run()
        item = result["items"][0]
        # _apply_mappings가 추가하는 필드들
        assert "retailer_code" in item
        assert "dist_code"     in item
        assert "unconfirmed"   in item
        assert "product_code"  in item

    def test_cover_totals_is_dict(self):
        result, _ = _run()
        assert isinstance(result["cover_totals"], dict)
