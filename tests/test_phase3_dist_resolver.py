"""test_phase3_dist_resolver.py — dist_code 조회 resolver 단위 테스트

검증 항목:
  1. retailer_code가 1:1 → auto_1_to_1 자동 확정
  2. retailer_code가 1:N → needs_confirmation + 후보 반환
  3. ocr_dist.csv 캐시 히트 → cache 확정
  4. retail_user.csv에 없음 → not_found
  5. 빈 retailer_code → not_found
  6. CSV 파일 없음 → not_found
  7. 파일 쓰기 없음 검증
  8. adapter와 연결 시 dist_code 채워짐
  9. legacy phase3와 동일한 결과 비교
  10. build_dist_resolution_from_cache (파일 I/O 없음 버전)

실행: pytest tests/test_phase3_dist_resolver.py -v
"""
import csv
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_dist_resolver import (
    DistResolution,
    build_dist_resolution_from_cache,
    resolve_dist_code_for_retailer,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def dirs(tmp_path: Path):
    mappings = tmp_path / "mappings"
    mappings.mkdir()
    return mappings


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_retail_user(mappings: Path, rows: list[dict]) -> None:
    write_csv(mappings / "retail_user.csv", rows)


def write_ocr_dist(mappings: Path, rows: list[dict]) -> None:
    write_csv(mappings / "ocr_dist.csv", rows)


# ── 1. 1:1 자동 확정 ──────────────────────────────────────────────────────────

class TestAutoOneToOne:
    def test_single_candidate_is_auto_confirmed(self, dirs):
        """retail_user.csv에 1건만 있으면 자동 확정 (basis="auto_1_to_1")."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.basis         == "auto_1_to_1"
        assert result.dist_code     == "D001"
        assert result.needs_confirmation is False
        assert result.dist_code is not None

    def test_single_candidate_dist_name_preserved_in_candidates(self, dirs):
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert len(result.candidates) == 1
        assert result.candidates[0]["dist_name"] == "東日本"

    def test_single_candidate_from_multiple_retailer_rows(self, dirs):
        """retail_user.csv에 같은 소매처코드가 1행만 있으면 자동 확정."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처코드": "R002", "소매처명": "別の店",  "판매처코드": "D002", "판매처명": "西日本"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.basis     == "auto_1_to_1"
        assert result.dist_code == "D001"


# ── 2. 1:N → needs_confirmation ───────────────────────────────────────────────

class TestMultipleCandidates:
    def test_multiple_candidates_returns_needs_confirmation(self, dirs):
        """retail_user.csv에 N건이면 needs_confirmation (legacy와 동일)."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D002", "판매처명": "西日本"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.basis              == "needs_confirmation"
        assert result.dist_code          is None
        assert result.needs_confirmation is True
        assert len(result.candidates)    == 2

    def test_candidates_contain_all_options(self, dirs):
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D002", "판매처명": "西日本"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D003", "판매처명": "中日本"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        codes = {c["dist_code"] for c in result.candidates}
        assert codes == {"D001", "D002", "D003"}

    def test_reason_mentions_candidate_count(self, dirs):
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "A"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D002", "판매처명": "B"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.reason is not None
        assert "2" in result.reason


# ── 3. ocr_dist.csv 캐시 히트 ────────────────────────────────────────────────

class TestDistCache:
    def test_cache_hit_returns_dist_code(self, dirs):
        """ocr_dist.csv에 (form_id, issuer_fp, retailer_code) 매칭 → cache 확정."""
        write_ocr_dist(dirs, [
            {"form_id": "form_01", "issuer_fingerprint": "国分|03-1234", "retailer_code": "R001",
             "dist_code": "D999", "dist_name": "キャッシュ担当"},
        ])
        result = resolve_dist_code_for_retailer(
            "R001", mappings_dir=dirs,
            form_id="form_01", issuer_fingerprint="国分|03-1234"
        )
        assert result.basis     == "cache"
        assert result.dist_code == "D999"
        assert result.needs_confirmation is False

    def test_cache_takes_priority_over_retail_user(self, dirs):
        """캐시가 있으면 retail_user.csv 조회보다 우선한다."""
        write_ocr_dist(dirs, [
            {"form_id": "form_01", "issuer_fingerprint": "fp1", "retailer_code": "R001",
             "dist_code": "D_CACHE", "dist_name": "캐시담당"},
        ])
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D_RETAIL", "판매처명": "리테일담당"},
        ])
        result = resolve_dist_code_for_retailer(
            "R001", mappings_dir=dirs,
            form_id="form_01", issuer_fingerprint="fp1"
        )
        assert result.basis     == "cache"
        assert result.dist_code == "D_CACHE"

    def test_cache_miss_falls_through_to_retail(self, dirs):
        """form_id나 issuer_fp가 다르면 캐시 미스 → retail_user.csv 조회."""
        write_ocr_dist(dirs, [
            {"form_id": "OTHER_FORM", "issuer_fingerprint": "fp1", "retailer_code": "R001",
             "dist_code": "D999", "dist_name": ""},
        ])
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "担当"},
        ])
        result = resolve_dist_code_for_retailer(
            "R001", mappings_dir=dirs,
            form_id="form_01", issuer_fingerprint="fp1"
        )
        assert result.basis     == "auto_1_to_1"
        assert result.dist_code == "D001"


# ── 4. Not Found ──────────────────────────────────────────────────────────────

class TestNotFound:
    def test_missing_retailer_code_returns_not_found(self, dirs):
        """retail_user.csv에 소매처코드가 없으면 not_found."""
        write_retail_user(dirs, [
            {"소매처코드": "R999", "소매처명": "別店", "판매처코드": "D999", "판매처명": "別担当"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.basis     == "not_found"
        assert result.dist_code is None
        assert result.needs_confirmation is False
        assert len(result.candidates) == 0

    def test_empty_retailer_code_returns_not_found(self, dirs):
        """빈 retailer_code → not_found."""
        result = resolve_dist_code_for_retailer("", mappings_dir=dirs)
        assert result.basis == "not_found"

    def test_no_retail_user_csv_returns_not_found(self, dirs):
        """retail_user.csv가 없으면 not_found."""
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.basis     == "not_found"
        assert result.dist_code is None


# ── 5. 파일 쓰기 없음 검증 ───────────────────────────────────────────────────

class TestNoFileWriting:
    def test_resolver_does_not_write_files(self, dirs):
        """resolver는 파일을 쓰지 않는다 (읽기 전용)."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"},
        ])
        files_before = set(dirs.iterdir())

        resolve_dist_code_for_retailer("R001", mappings_dir=dirs)

        files_after = set(dirs.iterdir())
        new_files = files_after - files_before
        assert not new_files, f"resolver가 새 파일을 생성함: {new_files}"

    def test_confirm_mapping_not_called(self, dirs):
        """resolver는 confirm_mapping을 호출하지 않는다."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"},
        ])
        with patch("backend.tools.mapping.confirm_mapping") as mock_confirm:
            resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
            mock_confirm.assert_not_called()


# ── 6. build_dist_resolution_from_cache (I/O 없음) ───────────────────────────

class TestBuildFromCache:
    def test_cache_hit(self):
        """pre-loaded 캐시 딕셔너리에서 캐시 히트. 키는 (form_id, fp, retailer_code, jisho) 4튜플."""
        cached = {("form_01", "fp1", "R001", ""): "D999"}
        result = build_dist_resolution_from_cache(
            "R001", cached, [],
            form_id="form_01", issuer_fingerprint="fp1"
        )
        assert result.basis     == "cache"
        assert result.dist_code == "D999"

    def test_cache_hit_jisho_specific(self):
        """같은 소매처라도 jisho가 다르면 다른 판매처를 반환한다."""
        cached = {
            ("form_04", "fp1", "R001", "CVS営業部"): "D100",
            ("form_04", "fp1", "R001", "R営業東北"): "D200",
        }
        r1 = build_dist_resolution_from_cache(
            "R001", cached, [], form_id="form_04", issuer_fingerprint="fp1", jisho="CVS営業部")
        r2 = build_dist_resolution_from_cache(
            "R001", cached, [], form_id="form_04", issuer_fingerprint="fp1", jisho="R営業東北")
        assert r1.dist_code == "D100"
        assert r2.dist_code == "D200"

    def test_1to1_from_rows(self):
        """pre-loaded retail_user rows에서 1:1 자동 확정."""
        rows = [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"},
        ]
        result = build_dist_resolution_from_cache("R001", {}, rows)
        assert result.basis     == "auto_1_to_1"
        assert result.dist_code == "D001"

    def test_1ton_returns_candidates(self):
        """pre-loaded rows에서 1:N → needs_confirmation."""
        rows = [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "A"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D002", "판매처명": "B"},
        ]
        result = build_dist_resolution_from_cache("R001", {}, rows)
        assert result.basis              == "needs_confirmation"
        assert result.needs_confirmation is True
        assert len(result.candidates)    == 2

    def test_not_found(self):
        """rows에 해당 소매처코드 없음 → not_found."""
        rows = [{"소매처코드": "R999", "소매처명": "別", "판매처코드": "D999", "판매처명": "別"}]
        result = build_dist_resolution_from_cache("R001", {}, rows)
        assert result.basis == "not_found"

    def test_no_file_io(self):
        """build_dist_resolution_from_cache는 파일을 열지 않는다."""
        with patch("builtins.open", side_effect=RuntimeError("파일 I/O 금지")) as mock_open:
            result = build_dist_resolution_from_cache("R001", {}, [])
            mock_open.assert_not_called()
        assert result.basis == "not_found"


# ── 7. adapter와의 연결 ───────────────────────────────────────────────────────

class TestAdapterIntegration:
    def test_dist_resolution_1to1_populates_dist_code_in_decision(self, dirs):
        """DistResolution(auto_1_to_1) → RetailerMappingDecision.dist_code 채워짐."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
        ])
        from backend.tools.mapping import LookupRetailerResult
        from backend.pipeline.phase3_tool_result_adapter import retailer_decision_from_lookup_result

        lr = LookupRetailerResult(retailer_code="R001", basis="cache", confidence=1.0)
        dist = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)

        decision = retailer_decision_from_lookup_result(
            "テスト店舗",
            lr,
            dist_resolution=dist,  # ← resolver 결과 주입
        )
        assert decision.dist_code == "D001"
        assert decision.retailer_code == "R001"

    def test_dist_resolution_not_found_leaves_dist_code_empty(self, dirs):
        """DistResolution(not_found) → dist_code는 ""로 유지."""
        from backend.tools.mapping import LookupRetailerResult
        from backend.pipeline.phase3_tool_result_adapter import retailer_decision_from_lookup_result

        lr = LookupRetailerResult(retailer_code="R001", basis="cache", confidence=1.0)
        dist = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)  # CSV 없음 → not_found

        decision = retailer_decision_from_lookup_result(
            "テスト店舗",
            lr,
            dist_resolution=dist,
        )
        assert decision.dist_code     == ""
        assert decision.retailer_code == "R001"

    def test_adapter_remains_pure_with_prefetched_dist(self, dirs):
        """adapter에 dist_code가 채워진 decision을 넘기면 파일 I/O 없이 변환된다."""
        from backend.tools.mapping import LookupRetailerResult
        from backend.pipeline.phase3_tool_result_adapter import (
            RetailerMappingDecision,
            convert_tool_use_result_to_phase3_output,
        )

        # dist_code 미리 채운 decision
        decision = RetailerMappingDecision(
            ocr_name="テスト店舗",
            retailer_code="R001",
            dist_code="D001",     # 미리 채워진 값
            basis="cache",
            confidence=1.0,
        )

        phase2 = {"pages": [], "items": [
            {"customer": "テスト店舗", "product": "商品A",
             "item_type": "条件", "columns": {}},
        ]}

        with patch("builtins.open", side_effect=RuntimeError("파일 I/O 금지")):
            result, pending = convert_tool_use_result_to_phase3_output(
                doc_id="doc1", form_id="form_01", hatsu_month="",
                issuer={}, phase2_result=phase2,
                retailer_decisions=[decision], product_decisions=[],
            )

        assert result["confirmed_retailers"]["テスト店舗"]["dist_code"] == "D001"


# ── 8. Legacy 동작 비교 ───────────────────────────────────────────────────────

class TestLegacyParity:
    """resolve_dist_code_for_retailer 결과가 legacy phase3 로직과 일치하는지 검증."""

    def test_1to1_parity_with_legacy(self, dirs):
        """1:1 케이스: legacy는 자동 확정. resolver도 동일."""
        retail_rows = [
            {"소매처코드": "R001", "소매처명": "テスト店", "판매처코드": "D001", "판매처명": "東日本"},
        ]
        write_retail_user(dirs, retail_rows)

        # Legacy 시뮬레이션: retail_user_rows에서 1:1 필터
        legacy_candidates = [
            {"dist_code": r["판매처코드"], "dist_name": r["판매처명"]}
            for r in retail_rows
            if r.get("소매처코드") == "R001"
        ]
        legacy_dist = legacy_candidates[0]["dist_code"] if len(legacy_candidates) == 1 else ""

        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        assert result.dist_code == legacy_dist

    def test_not_found_parity_with_legacy(self, dirs):
        """0건 케이스: legacy는 pending에 추가. resolver는 not_found 반환."""
        write_retail_user(dirs, [
            {"소매처코드": "OTHER", "소매처명": "別店", "판매처코드": "D999", "판매처명": "別"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        # legacy: pending.append({"mapping_type": "dist", ...})
        # resolver: basis="not_found", dist_code=None
        assert result.basis     == "not_found"
        assert result.dist_code is None

    def test_1ton_needs_claude_parity(self, dirs):
        """N건 케이스: legacy는 Claude에 위임. resolver는 needs_confirmation=True."""
        write_retail_user(dirs, [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "A"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D002", "판매처명": "B"},
        ])
        result = resolve_dist_code_for_retailer("R001", mappings_dir=dirs)
        # legacy: cached_retailers_needing_dist에 추가 → Claude 판단
        # resolver: needs_confirmation=True, candidates 반환
        assert result.needs_confirmation is True
        assert len(result.candidates) >= 2
