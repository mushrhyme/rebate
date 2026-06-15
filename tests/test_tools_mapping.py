"""test_tools_mapping.py — lookup_retailer() 단위 테스트 + phase3 회귀

테스트 목표:
  1. cache hit        — ocr_retailer.csv 히트 (정확 매칭 + 정규화 매칭)
  2. bracket_code     — 괄호 코드 → domae_retail CSV 직접 매칭
  3. candidate        — 유사도 검색 후보 반환
  4. not_found        — 캐시·괄호코드·유사도 모두 실패
  5. phase3 회귀      — 임포트·유틸리티 함수 동작 확인

전략: 실제 CSV/MD 파일을 tmp_path fixture로 생성 (mocking 없음).
"""
import csv
from pathlib import Path

import pytest


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def write_csv(path: Path, rows: list[dict]) -> None:
    """UTF-8-BOM CSV fixture 생성."""
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def dirs(tmp_path: Path):
    """mappings/ 와 form_definitions/ 디렉토리."""
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()
    return mappings, form_defs


# ── 1. 캐시 히트 ──────────────────────────────────────────────────────────────

class TestCacheHit:
    """ocr_retailer.csv에서 캐시 히트하는 시나리오."""

    async def test_exact_match(self, dirs):
        """저장된 OCR명과 쿼리가 완전히 같으면 cache 히트."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "ダイレックス(株) (32423)", "retailer_code": "6003851", "retailer_name": "ダイレックス"},
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ダイレックス(株) (32423)", "form_01", mappings, form_defs)

        assert result.basis == "cache"
        assert result.retailer_code == "6003851"
        assert result.confidence == 1.0
        assert result.candidates == []

    async def test_normalized_match(self, dirs):
        """全角 法人格으로 캐시 저장 → 半角 쿼리로 정규화 히트.

        normalize("ダイレックス株式会社") == normalize("(株)ダイレックス") == "ダイレックス"
        """
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "ダイレックス株式会社", "retailer_code": "6003851", "retailer_name": "ダイレックス"},
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("(株)ダイレックス", "form_01", mappings, form_defs)

        assert result.basis == "cache"
        assert result.retailer_code == "6003851"

    async def test_cache_takes_priority_over_bracket(self, dirs):
        """캐시 히트 시 괄호코드 조회를 거치지 않는다."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "ダイレックス(株) (32423)", "retailer_code": "CACHE_CODE", "retailer_name": "テスト"},
        ])
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "BRACKET_CODE"},
        ])
        (form_defs / "form_01.md").write_text(
            "## データソース\nbracket_code_csv: domae_retail_1.csv\n- domae_retail_1.csv\n",
            encoding="utf-8",
        )

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ダイレックス(株) (32423)", "form_01", mappings, form_defs)

        assert result.basis == "cache"
        assert result.retailer_code == "CACHE_CODE"  # 캐시 우선, bracket_code 아님


# ── 2. 괄호 코드 매칭 ────────────────────────────────────────────────────────

class TestBracketCode:
    """bracket_code_csv 설정이 있는 양식에서 괄호 코드 직접 매칭."""

    def _write_form_md(self, form_defs: Path, form_id: str) -> None:
        (form_defs / f"{form_id}.md").write_text(
            "## データソース\nbracket_code_csv: domae_retail_1.csv\n- domae_retail_1.csv\n- retail_user.csv\n",
            encoding="utf-8",
        )

    async def test_bracket_match(self, dirs):
        """OCR명의 괄호 숫자가 domae_retail_1.csv와 매칭되면 bracket_code 확정."""
        mappings, form_defs = dirs
        self._write_form_md(form_defs, "form_01")
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "6003851"},
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ダイレックス(株) (32423)", "form_01", mappings, form_defs)

        assert result.basis == "bracket_code"
        assert result.retailer_code == "6003851"
        assert result.confidence == 1.0
        assert result.candidates == []

    async def test_bracket_code_not_in_csv(self, dirs):
        """괄호 코드가 CSV에 없으면 bracket 매칭 실패 → not_found로 낙하."""
        mappings, form_defs = dirs
        self._write_form_md(form_defs, "form_01")
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "99999", "소매처코드": "OTHER"},  # 32423 없음
        ])
        # retail_user.csv 없음 → candidate 검색도 실패

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ダイレックス(株) (32423)", "form_01", mappings, form_defs)

        assert result.basis == "not_found"
        assert result.retailer_code is None

    async def test_no_bracket_in_ocr_name(self, dirs):
        """OCR명에 숫자 괄호가 없으면 bracket 매칭 미시도."""
        mappings, form_defs = dirs
        self._write_form_md(form_defs, "form_01")
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "6003851"},
        ])

        from backend.tools.mapping import lookup_retailer
        # "株式会社" 형태 — 숫자 괄호 없음
        result = await lookup_retailer("ダイレックス株式会社", "form_01", mappings, form_defs)

        assert result.basis != "bracket_code"
        assert result.retailer_code is None

    async def test_no_bracket_code_csv_in_form(self, dirs):
        """form_XX.md에 bracket_code_csv 지시어가 없으면 괄호코드 조회 스킵."""
        mappings, form_defs = dirs
        # bracket_code_csv 없는 form_04
        (form_defs / "form_04.md").write_text(
            "## データソース\n- retail_user.csv\n",
            encoding="utf-8",
        )
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "6003851"},
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("何かの店 (32423)", "form_04", mappings, form_defs)

        assert result.basis != "bracket_code"


# ── 3. 후보 반환 ─────────────────────────────────────────────────────────────

class TestCandidates:
    """캐시·괄호코드 미스 후 유사도 검색으로 후보 반환."""

    async def test_legal_marker_normalized_to_exact_match(self, dirs):
        """法人格記号(株)を正規化後に完全一致 → exact_match で自動確定."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "ファミリーマート", "소매처코드": "6001234", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처명": "セブンイレブン",   "소매처코드": "6002345", "판매처코드": "D002", "판매처명": "関東"},
        ])

        from backend.tools.mapping import lookup_retailer
        # 全角括弧 → normalize → "ファミリーマート" → similarity 1.0 → exact_match
        result = await lookup_retailer("（株）ファミリーマート", "form_02", mappings, form_defs)

        assert result.basis == "exact_match"
        assert result.retailer_code == "6001234"
        assert result.confidence == 1.0

    async def test_candidates_sorted_by_similarity(self, dirs):
        """후보가 복수일 때 유사도 내림차순 정렬."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "イオン",         "소매처코드": "A001", "판매처코드": "D001", "판매처명": "test"},
            {"소매처명": "イオンモール",   "소매처코드": "A002", "판매처코드": "D001", "판매처명": "test"},
            {"소매처명": "イオンリテール", "소매처코드": "A003", "판매처코드": "D001", "판매처명": "test"},
        ])

        from backend.tools.mapping import lookup_retailer
        # "イオングループ"はCSV에 없어서 exact_match에 해당しない → candidate
        result = await lookup_retailer("イオングループ", "form_02", mappings, form_defs)

        assert result.basis == "candidate"
        sims = [c["similarity"] for c in result.candidates]
        assert sims == sorted(sims, reverse=True), "유사도 내림차순 정렬 실패"

    async def test_top_k_limit(self, dirs):
        """top_k 파라미터가 후보 수 상한을 제한한다."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": f"テスト店舗{i:02d}", "소매처코드": f"C{i:03d}", "판매처코드": "D001", "판매처명": "test"}
            for i in range(10)
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("テスト店舗", "form_02", mappings, form_defs, top_k=3)

        assert result.basis == "candidate"
        assert len(result.candidates) <= 3

    async def test_dedup_same_retailer_code(self, dirs):
        """동일 소매처코드가 여러 행에 있으면 후보에서 1건만 반환한다.

        retail_user.csv는 1:N 구조 (같은 소매처코드 + 다른 판매처코드).
        소매처 후보는 코드 단위로 dedup 해야 한다.
        exact_match 이외의 쿼리로 candidate 경로를 통과시켜서 검증한다.
        """
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "ファミリーマート", "소매처코드": "6001234", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처명": "ファミリーマート", "소매처코드": "6001234", "판매처코드": "D002", "판매처명": "西日本"},
        ])

        from backend.tools.mapping import lookup_retailer
        # "ファミリーマート系" → similarity ≈ 0.94 (not exact_match) → candidate 경로
        result = await lookup_retailer("ファミリーマート系", "form_02", mappings, form_defs)

        assert result.basis == "candidate"
        codes = [c["retailer_code"] for c in result.candidates]
        assert codes.count("6001234") == 1, "동일 소매처코드 중복 제거 실패"

    async def test_domae_retail_1_skipped_in_candidate_search(self, dirs):
        """domae_retail_1.csv(코드→코드 매핑)는 후보 검색 대상에서 제외된다."""
        mappings, form_defs = dirs
        (form_defs / "form_01.md").write_text(
            "## データソース\nbracket_code_csv: domae_retail_1.csv\n- domae_retail_1.csv\n",
            encoding="utf-8",
        )
        # domae_retail_1.csv: 첫 열이 숫자 코드 → 유사도 검색 스킵
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "6003851"},
            {"도매소매처코드": "11111", "소매처코드": "6004444"},
        ])
        # OCR명에 숫자 괄호 없음 → bracket miss → candidate 검색
        # domae_retail_1.csv가 스킵되어야 하므로 candidates 없음 → not_found

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("適当な店名", "form_01", mappings, form_defs)

        assert result.basis == "not_found"


# ── 4. not_found ─────────────────────────────────────────────────────────────

class TestNotFound:
    """캐시·괄호코드·유사도 검색 모두 실패하는 시나리오."""

    async def test_no_csv_files(self, dirs):
        """아무 CSV 파일도 없으면 not_found."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("どこかの店舗", "form_02", mappings, form_defs)

        assert result.basis == "not_found"
        assert result.retailer_code is None
        assert result.confidence == 0.0
        assert result.candidates == []

    async def test_below_similarity_threshold(self, dirs):
        """유사도 0.3 미만은 후보로 취급하지 않는다."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        # 쿼리 "ZZZZZZZZZZ" vs CSV "あいうえお" — 유사도 ≈ 0
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "あいうえお", "소매처코드": "9999", "판매처코드": "D001", "판매처명": "test"},
        ])

        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ZZZZZZZZZZ", "form_02", mappings, form_defs)

        assert result.basis == "not_found"

    async def test_no_form_md_falls_back_to_retail_user(self, dirs):
        """form_XX.md가 없으면 retail_user.csv를 기본값으로 검색한다."""
        mappings, form_defs = dirs
        # form_99.md 없음
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "テスト", "소매처코드": "1111", "판매처코드": "D001", "판매처명": "test"},
        ])

        from backend.tools.mapping import lookup_retailer
        # form_99.md 없어도 retail_user.csv 기본 검색 → exact_match 확정
        result = await lookup_retailer("テスト", "form_99", mappings, form_defs)

        assert result.basis == "exact_match"
        assert result.retailer_code == "1111"


# ── 4-B. 제품 용량 우선 매칭 ──────────────────────────────────────────────────

class TestProductVolumePriority:
    """search_product 후보 검색의 용량 우선 정렬·정확 일치 판정.

    배경: 과거 ±5% 비율 톨러런스(0.95)가 103↔105처럼 인접하지만 다른 용량을
    '같은 용량'으로 오판해, 이름이 더 비슷한 틀린 용량 제품에 가산까지 줬다.
    → OCR에 용량이 있으면 용량 일치 후보를 점수와 무관하게 위로 올린다.
    """

    def _master(self, mappings: Path) -> None:
        # 이름은 거의 같고 용량만 다른 실제 패턴 (103 vs 105)
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P103", "제품명": "辛ラーメン焼きそばカップ24入",  "제품용량": "103.0", "규격": "12×2", "시키리": "100", "본부장": "110", "JANコード": ""},
            {"제품코드": "P105", "제품명": "N辛ラーメン焼きそばカップ24入", "제품용량": "105.0", "규격": "12×2", "시키리": "100", "본부장": "110", "JANコード": ""},
        ])

    async def test_exact_volume_outranks_more_similar_name(self, dirs):
        """이름은 105 제품과 똑같지만 OCR 용량이 103이면 103 제품이 1순위."""
        mappings, _ = dirs
        self._master(mappings)
        from backend.tools.mapping import search_product
        # 이름은 N제품(105)과 동일, 용량만 103
        res = await search_product("N辛ラーメン焼きそばカップ24入 103g", mappings, top_k=15)
        assert res.basis == "candidate"
        assert res.candidates[0]["code"] == "P103", (
            f"용량 103 후보가 1순위여야 함, 실제 1순위={res.candidates[0]['code']}"
        )

    async def test_close_but_distinct_volume_not_treated_as_match(self, dirs):
        """103과 105는 ±5% 이내(0.981)지만 다른 제품 — 105는 용량 불일치로 하위."""
        mappings, _ = dirs
        self._master(mappings)
        from backend.tools.mapping import search_product
        res = await search_product("辛ラーメン焼きそばカップ24入 103g", mappings, top_k=15)
        codes = [c["code"] for c in res.candidates]
        # 105 제품은 후보에 남되(재고 여지), 103보다 뒤
        assert codes.index("P103") < codes.index("P105")

    async def test_volume_match_candidate_survives_cutoff(self, dirs):
        """이름이 거의 안 맞아도 용량 일치 후보는 컷오프(0.3) 면제로 후보에 포함."""
        mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "PX", "제품명": "全く違う商品名アイウエオ", "제품용량": "103.0", "규격": "", "시키리": "1", "본부장": "1", "JANコード": ""},
        ])
        from backend.tools.mapping import search_product
        res = await search_product("辛ラーメン焼きそば 103g", mappings, top_k=15)
        assert res.basis == "candidate"
        assert any(c["code"] == "PX" for c in res.candidates), "용량 일치 후보가 컷오프로 누락됨"

    async def test_no_ocr_volume_keeps_name_only_ranking(self, dirs):
        """OCR에 용량이 없으면 기존대로 명칭 유사도 순 (용량 정렬 미적용)."""
        mappings, _ = dirs
        self._master(mappings)
        from backend.tools.mapping import search_product
        res = await search_product("辛ラーメン焼きそばカップ24入", mappings, top_k=15)  # 용량 토큰 없음
        # 이름 더 가까운 P103(접두 N 없음)이 자연스럽게 위 — 단 용량 보정 없이 순수 명칭
        assert res.candidates[0]["code"] == "P103"


class TestVolumeExtraction:
    """_extract_volume_g — g 뒤 CJK/가나 경계 처리.

    배경: 정규식 `g\\b`는 袋·カップ 같은 유니코드 단어 문자에서 경계가 사라져
    "120g袋"·"120gカップ"의 추출이 실패했다(봉지·컵 주력 SKU 16% 누락).
    """

    def test_g_followed_by_cjk_or_kana(self):
        from backend.tools.mapping import _extract_volume_g
        assert _extract_volume_g("辛ラーメン 120g袋") == 120.0
        assert _extract_volume_g("辛ラーメン 120gカップ") == 120.0
        assert _extract_volume_g("カムジャ麺 100g袋") == 100.0
        assert _extract_volume_g("135g") == 135.0          # 끝
        assert _extract_volume_g("68g 12×2") == 68.0       # 공백
        assert _extract_volume_g("辛ラーメン 137.5g袋") == 137.5  # 소수

    def test_no_false_positive_on_word_g(self):
        from backend.tools.mapping import _extract_volume_g
        # 'g'가 영어 단어 중간이면 용량 아님
        assert _extract_volume_g("100gram pack") is None
        assert _extract_volume_g("big size") is None
        # 단위 없는 끝 숫자는 용량으로 보지 않음 (입수·수량 오인 방지)
        assert _extract_volume_g("辛ラーメン焼きそばカップ炒めキムチ味122") is None

    def test_pack_count_not_volume(self):
        from backend.tools.mapping import _extract_volume_g
        # 'P'·'袋'만 붙은 개수 표기는 g 용량 아님
        assert _extract_volume_g("辛ラーメン キムチ 3P袋") is None
        assert _extract_volume_g("本場韓国コムタンラーメン 3袋") is None


# ── 5. phase3.py 회귀 테스트 ─────────────────────────────────────────────────

class TestPhase3Regression:
    """phase3.py 수정 후 기존 동작이 유지되는지 검증."""

    def test_phase3_can_import(self):
        """phase3.py가 새 import 구조(tools.mapping 의존)에서 정상 로드된다."""
        from backend.pipeline import phase3
        assert callable(phase3.run_phase3)

    def test_normalize_ocr_name_accessible_from_phase3(self):
        """phase3이 tools.mapping에서 normalize_ocr_name을 올바르게 임포트한다."""
        from backend.pipeline.phase3 import normalize_ocr_name as p3_norm
        from backend.tools.mapping import normalize_ocr_name as tool_norm

        cases = [
            "（株）ファミリーマート",
            "ダイレックス株式会社",
            "(株) テスト (32423)",
        ]
        for name in cases:
            assert p3_norm(name) == tool_norm(name), f"결과 불일치: {name!r}"

    def test_parse_retailer_csv_sources_default(self):
        """form_MD가 없거나 비어있으면 retail_user.csv 기본값을 반환한다."""
        from backend.tools.mapping import parse_retailer_csv_sources

        assert parse_retailer_csv_sources("") == ["retail_user.csv"]
        assert parse_retailer_csv_sources("# 서문만 있는 MD") == ["retail_user.csv"]

    def test_parse_retailer_csv_sources_from_form04(self):
        """form_04.md 형식의 データソース 섹션을 올바르게 파싱한다."""
        from backend.tools.mapping import parse_retailer_csv_sources

        form_md = (
            "## データソース\n"
            "(form_04는 이름 기반 검색 방식)\n\n"
            "- retail_user.csv\n"
        )
        result = parse_retailer_csv_sources(form_md)
        assert result == ["retail_user.csv"]

    def test_build_retailer_csv_context_uses_parse_sources(self, dirs):
        """_build_retailer_csv_context가 parse_retailer_csv_sources로 CSV를 로드한다."""
        mappings, form_defs = dirs
        (mappings / "retail_user.csv").write_text(
            "소매처명,소매처코드\nテスト店,1234\n", encoding="utf-8-sig"
        )
        form_md = "## データソース\n- retail_user.csv\n"

        from backend.pipeline.phase3 import _build_retailer_csv_context
        context = _build_retailer_csv_context(form_md, mappings)

        assert "retail_user.csv" in context
        assert "テスト店" in context

    def test_phase3_no_longer_has_parse_retailer_csvs(self):
        """_parse_retailer_csvs는 phase3에서 제거됐다 (tools.mapping으로 이동)."""
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_parse_retailer_csvs"), (
            "_parse_retailer_csvs가 아직 phase3.py에 남아 있음 — 제거 필요"
        )

    def test_phase3_no_longer_has_load_retailer_cache(self):
        """_load_retailer_cache는 phase3에서 제거됐다 (lookup_retailer 내부로 이동)."""
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_load_retailer_cache"), (
            "_load_retailer_cache가 아직 phase3.py에 남아 있음 — 제거 필요"
        )

    def test_phase3_no_longer_has_load_product_cache(self):
        """_load_product_cache는 phase3에서 제거됐다 (search_product 내부로 이동)."""
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_load_product_cache"), (
            "_load_product_cache가 아직 phase3.py에 남아 있음 — 제거 필요"
        )

    def test_phase3_imports_search_product(self):
        """phase3이 tools.mapping에서 search_product를 올바르게 임포트한다."""
        from backend.pipeline import phase3
        assert callable(phase3.search_product)


# ── 6. search_product 단위 테스트 ────────────────────────────────────────────

class TestSearchProduct:
    """search_product() — OCR 제품명 → 제품코드 조회."""

    # ── 6-A. 캐시 히트 ───────────────────────────────────────────────────────

    async def test_cache_exact_match(self, dirs):
        """ocr_product.csv에 OCR명이 그대로 있으면 cache 히트."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心 辛ラーメン 3P", "product_code": "101000491", "product_name": "辛ラーメン"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("農心 辛ラーメン 3P", mappings)

        assert result.basis == "cache"
        assert result.product_code == "101000491"
        assert result.confidence == 1.0
        assert result.candidates == []

    async def test_cache_normalized_match(self, dirs):
        """全角 법인격 포함 캐시 → 半角 쿼리로 정규화 히트.

        normalize("農心辛ラーメン株式会社") == normalize("農心辛ラーメン(株)") == "農心辛ラーメン"
        """
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心辛ラーメン株式会社", "product_code": "P001", "product_name": "辛ラーメン"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("農心辛ラーメン(株)", mappings)

        assert result.basis == "cache"
        assert result.product_code == "P001"

    async def test_cache_takes_priority_over_similarity(self, dirs):
        """캐시 히트 시 unit_price.csv 유사도 검색을 거치지 않는다."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心 辛ラーメン 3P", "product_code": "CACHE_CODE", "product_name": "test"},
        ])
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "UNIT_CODE", "제품명": "農心 辛ラーメン 3P", "시키리": "100", "본부장": "90"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("農心 辛ラーメン 3P", mappings)

        assert result.basis == "cache"
        assert result.product_code == "CACHE_CODE"

    # ── 6-B. 후보 반환 ───────────────────────────────────────────────────────

    async def test_candidate_from_unit_price(self, dirs):
        """unit_price.csv에서 유사 제품명 발견 → candidate 반환."""
        mappings, form_defs = dirs
        # 쿼리와 완전히 동일한 이름을 1순위 데이터로 사용해 순위를 명확하게 고정
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "101000491", "제품명": "農心 辛ラーメン 120g",          "시키리": "100", "본부장": "90"},
            {"제품코드": "101003042", "제품명": "農心 辛ラーメンミニカップ 49G", "시키리": "80",  "본부장": "70"},
        ])

        from backend.tools.mapping import search_product
        # 쿼리 "農心 辛ラーメン 120g" — 동일 이름 → similarity=1.0 → 1순위 확정
        result = await search_product("農心 辛ラーメン 120g", mappings)

        assert result.basis == "candidate"
        assert result.product_code is None
        assert len(result.candidates) >= 1
        assert result.candidates[0]["code"] == "101000491"
        # 계약: basis="candidate" → confidence = candidates[0]["score"] (> 0.3)
        assert result.confidence == result.candidates[0]["score"] > 0.3

    async def test_candidates_sorted_by_similarity(self, dirs):
        """후보가 복수일 때 유사도 내림차순 정렬."""
        mappings, form_defs = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "テスト商品A",     "시키리": "100", "본부장": "90"},
            {"제품코드": "P002", "제품명": "テスト商品AB",    "시키리": "100", "본부장": "90"},
            {"제품코드": "P003", "제품명": "テスト商品ABC",   "시키리": "100", "본부장": "90"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("テスト商品AB", mappings)

        assert result.basis == "candidate"
        sims = [c["score"] for c in result.candidates]
        assert sims == sorted(sims, reverse=True), "유사도 내림차순 정렬 실패"

    async def test_top_k_limit(self, dirs):
        """top_k 파라미터가 후보 수 상한을 제한한다."""
        mappings, form_defs = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": f"P{i:03d}", "제품명": f"テスト商品{i:02d}", "시키리": "100", "본부장": "90"}
            for i in range(10)
        ])

        from backend.tools.mapping import search_product
        result = await search_product("テスト商品", mappings, top_k=3)

        assert result.basis == "candidate"
        assert len(result.candidates) <= 3

    async def test_dedup_same_product_code(self, dirs):
        """동일 product_code가 여러 행에 있으면 최고 점수 1건만 반환한다."""
        mappings, form_defs = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "辛ラーメン 袋",   "시키리": "100", "본부장": "90"},
            {"제품코드": "P001", "제품명": "辛ラーメン 袋型", "시키리": "100", "본부장": "90"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("辛ラーメン 袋", mappings)

        codes = [c["code"] for c in result.candidates]
        assert codes.count("P001") == 1, "동일 product_code 중복 제거 실패"

    # ── 6-C. not_found ──────────────────────────────────────────────────────

    async def test_not_found_no_csv(self, dirs):
        """unit_price.csv가 없으면 not_found."""
        mappings, form_defs = dirs
        # 어떤 CSV도 없음

        from backend.tools.mapping import search_product
        result = await search_product("農心 辛ラーメン", mappings)

        assert result.basis == "not_found"
        assert result.product_code is None
        assert result.confidence == 0.0
        assert result.candidates == []

    async def test_not_found_below_threshold(self, dirs):
        """유사도 0.3 미만은 후보로 취급하지 않는다."""
        mappings, form_defs = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P999", "제품명": "AAAA", "시키리": "100", "본부장": "90"},
        ])

        from backend.tools.mapping import search_product
        result = await search_product("全然違う製品名ZZZZZ", mappings)

        assert result.basis == "not_found"


# ── 7. confirm_mapping 단위 테스트 ───────────────────────────────────────────

class TestConfirmMapping:
    """confirm_mapping() — 매핑 확정 결과를 캐시 CSV에 기록."""

    def _read_csv(self, path: Path) -> list[dict]:
        import csv as _csv
        with path.open(encoding="utf-8-sig") as f:
            return list(_csv.DictReader(f))

    # ── 7-A. retailer 저장 ──────────────────────────────────────────────────

    async def test_retailer_creates_csv_if_absent(self, dirs):
        """ocr_retailer.csv가 없어도 새로 생성된다."""
        mappings, _ = dirs

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="retailer",
            ocr_name="ダイレックス(株) (32423)",
            confirmed_code="6003851",
            context={"retailer_name": "ダイレックス"},
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_retailer.csv")
        assert len(rows) == 1
        assert rows[0]["ocr_name"] == "ダイレックス(株) (32423)"
        assert rows[0]["retailer_code"] == "6003851"
        assert rows[0]["retailer_name"] == "ダイレックス"

    async def test_retailer_upsert_existing_row(self, dirs):
        """동일 ocr_name 재저장 시 행이 추가되지 않고 갱신된다."""
        mappings, _ = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "ダイレックス(株) (32423)", "retailer_code": "OLD_CODE", "retailer_name": "旧名"},
        ])

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="retailer",
            ocr_name="ダイレックス(株) (32423)",
            confirmed_code="6003851",
            context={"retailer_name": "ダイレックス"},
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_retailer.csv")
        assert len(rows) == 1, "중복 행이 생성됐음"
        assert rows[0]["retailer_code"] == "6003851"

    async def test_retailer_appends_new_row(self, dirs):
        """다른 ocr_name이면 행이 추가된다."""
        mappings, _ = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "既存店A", "retailer_code": "R001", "retailer_name": "既存A"},
        ])

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="retailer",
            ocr_name="新規店B",
            confirmed_code="R002",
            context={"retailer_name": "新規B"},
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_retailer.csv")
        assert len(rows) == 2
        codes = {r["retailer_code"] for r in rows}
        assert codes == {"R001", "R002"}

    # ── 7-B. product 저장 ───────────────────────────────────────────────────

    async def test_product_creates_csv_if_absent(self, dirs):
        """ocr_product.csv가 없어도 새로 생성된다."""
        mappings, _ = dirs

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="product",
            ocr_name="農心 辛ラーメン 3P",
            confirmed_code="101000491",
            context={"product_name": "農心 辛ラーメン 袋 120g×3"},
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_product.csv")
        assert len(rows) == 1
        assert rows[0]["ocr_name"] == "農心 辛ラーメン 3P"
        assert rows[0]["product_code"] == "101000491"
        assert rows[0]["product_name"] == "農心 辛ラーメン 袋 120g×3"

    async def test_product_upsert_existing_row(self, dirs):
        """동일 ocr_name 재저장 시 행이 갱신된다."""
        mappings, _ = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心 辛ラーメン 3P", "product_code": "OLD", "product_name": "旧名"},
        ])

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="product",
            ocr_name="農心 辛ラーメン 3P",
            confirmed_code="101000491",
            context={"product_name": "辛ラーメン"},
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_product.csv")
        assert len(rows) == 1
        assert rows[0]["product_code"] == "101000491"

    # ── 7-C. dist 저장 ──────────────────────────────────────────────────────

    async def test_dist_creates_csv_if_absent(self, dirs):
        """ocr_dist.csv가 없어도 새로 생성된다."""
        mappings, _ = dirs

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="dist",
            ocr_name="ファミリーマート",
            confirmed_code="D001",
            context={
                "form_id": "form_04",
                "issuer_fingerprint": "日本アクセス|03-1234-5678",
                "retailer_code": "6001234",
                "dist_name": "東日本",
            },
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_dist.csv")
        assert len(rows) == 1
        assert rows[0]["form_id"] == "form_04"
        assert rows[0]["retailer_code"] == "6001234"
        assert rows[0]["dist_code"] == "D001"
        assert rows[0]["dist_name"] == "東日本"

    async def test_dist_upsert_same_composite_key(self, dirs):
        """(form_id, issuer_fingerprint, retailer_code) 복합키 중복 시 갱신된다."""
        mappings, _ = dirs
        write_csv(mappings / "ocr_dist.csv", [
            {
                "form_id": "form_04", "issuer_fingerprint": "fp1",
                "retailer_code": "R001", "dist_code": "OLD_DIST", "dist_name": "旧担当",
            },
        ])

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="dist",
            ocr_name="ファミリーマート",
            confirmed_code="NEW_DIST",
            context={
                "form_id": "form_04",
                "issuer_fingerprint": "fp1",
                "retailer_code": "R001",
                "dist_name": "新担当",
            },
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_dist.csv")
        assert len(rows) == 1, "중복 행이 생성됐음"
        assert rows[0]["dist_code"] == "NEW_DIST"
        assert rows[0]["dist_name"] == "新担当"

    async def test_dist_different_fingerprint_adds_row(self, dirs):
        """fingerprint가 다르면 별개 행으로 추가된다."""
        mappings, _ = dirs
        write_csv(mappings / "ocr_dist.csv", [
            {
                "form_id": "form_04", "issuer_fingerprint": "fp_A",
                "retailer_code": "R001", "dist_code": "D_A", "dist_name": "担当A",
            },
        ])

        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="dist",
            ocr_name="ファミリーマート",
            confirmed_code="D_B",
            context={
                "form_id": "form_04",
                "issuer_fingerprint": "fp_B",   # 다른 fingerprint
                "retailer_code": "R001",
                "dist_name": "担当B",
            },
            mappings_dir=mappings,
        )

        rows = self._read_csv(mappings / "ocr_dist.csv")
        assert len(rows) == 2

    # ── 7-D. 예외 처리 ──────────────────────────────────────────────────────

    async def test_invalid_mapping_type_raises(self, dirs):
        """알 수 없는 mapping_type은 ValueError를 발생시킨다."""
        mappings, _ = dirs

        from backend.tools.mapping import confirm_mapping
        with pytest.raises(ValueError, match="알 수 없는 mapping_type"):
            await confirm_mapping(
                mapping_type="unknown",   # type: ignore[arg-type]
                ocr_name="テスト",
                confirmed_code="X001",
                context={},
                mappings_dir=mappings,
            )


# ── 8. confirm_mapping phase3 회귀 ───────────────────────────────────────────

class TestConfirmMappingPhase3Regression:
    """phase3.py에서 _append_* helper들이 제거되었는지 확인."""

    def test_append_retailer_cache_removed(self):
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_append_retailer_cache"), (
            "_append_retailer_cache가 아직 phase3.py에 남아 있음"
        )

    def test_append_product_cache_removed(self):
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_append_product_cache"), (
            "_append_product_cache가 아직 phase3.py에 남아 있음"
        )

    def test_append_dist_cache_removed(self):
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_append_dist_cache"), (
            "_append_dist_cache가 아직 phase3.py에 남아 있음"
        )

    def test_upsert_cache_row_importable_from_tools_mapping(self):
        """_upsert_cache_row는 tools.mapping에서 직접 임포트 가능하다."""
        from backend.tools.mapping import _upsert_cache_row
        assert callable(_upsert_cache_row)

    def test_upsert_dist_cache_row_importable_from_tools_mapping(self):
        """_upsert_dist_cache_row는 tools.mapping에서 직접 임포트 가능하다."""
        from backend.tools.mapping import _upsert_dist_cache_row
        assert callable(_upsert_dist_cache_row)

    def test_upsert_helpers_not_exposed_by_phase3(self):
        """phase3.py는 _upsert_* helper를 노출하지 않는다 (임시 re-export 완전 제거)."""
        import backend.pipeline.phase3 as p3
        assert not hasattr(p3, "_upsert_cache_row"), (
            "_upsert_cache_row가 phase3 네임스페이스에 남아 있음"
        )
        assert not hasattr(p3, "_upsert_dist_cache_row"), (
            "_upsert_dist_cache_row가 phase3 네임스페이스에 남아 있음"
        )

    def test_orchestrator_does_not_import_helpers_from_phase3(self):
        """orchestrator.py가 phase3 내부 helper(_upsert_*)에 의존하지 않는다."""
        import ast, pathlib
        src = pathlib.Path("backend/pipeline/orchestrator.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module and "phase3" in node.module:
                    names = [a.name for a in node.names]
                    assert "_upsert_cache_row" not in names, (
                        "orchestrator.py가 phase3에서 _upsert_cache_row를 import함"
                    )
                    assert "_upsert_dist_cache_row" not in names, (
                        "orchestrator.py가 phase3에서 _upsert_dist_cache_row를 import함"
                    )

    def test_phase3_imports_confirm_mapping(self):
        from backend.pipeline import phase3
        assert callable(phase3.confirm_mapping)


# ── 9. Contract 보장사항 검증 ─────────────────────────────────────────────────

class TestContractGuarantees:
    """공통 Contract 보장사항을 직접 검증한다.

    - confidence ∈ [0.0, 1.0]
    - candidates는 similarity 내림차순 정렬
    - basis 값이 정의된 Literal 범위 안
    - CSV 없음 → 예외 없이 not_found / 빈 후보
    - confirm_mapping → None 반환
    - dist 필수 context 키 누락 → ValueError (KeyError 아님)
    - 캐시 컬럼 누락 행 → 캐시 미스 처리 (KeyError 아님)
    """

    # ── confidence 범위 ──────────────────────────────────────────────────────

    async def test_confidence_is_1_on_cache_hit(self, dirs):
        """캐시 히트 시 confidence는 정확히 1.0이다."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "テスト店", "retailer_code": "R001", "retailer_name": "テスト"},
        ])
        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("テスト店", "form_01", mappings, form_defs)
        assert result.confidence == 1.0
        assert 0.0 <= result.confidence <= 1.0

    async def test_confidence_is_0_on_not_found(self, dirs):
        """not_found 시 confidence는 정확히 0.0이다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer, search_product
        r1 = await lookup_retailer("ZZZZZZ", "form_01", mappings, form_defs)
        r2 = await search_product("ZZZZZZ", mappings)
        assert r1.confidence == 0.0
        assert r2.confidence == 0.0

    async def test_confidence_is_1_on_exact_match(self, dirs):
        """exact_match 반환 시 confidence == 1.0."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "ファミリーマート", "소매처코드": "R001", "판매처코드": "D001", "판매처명": "test"},
        ])
        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("（株）ファミリーマート", "form_02", mappings, form_defs)
        assert result.basis == "exact_match"
        assert result.retailer_code == "R001"
        assert result.confidence == 1.0

    # ── basis 값 검증 ────────────────────────────────────────────────────────

    async def test_lookup_retailer_basis_always_valid(self, dirs):
        """lookup_retailer의 basis는 항상 정의된 Literal 범위 안이다."""
        from backend.tools.mapping import lookup_retailer
        mappings, form_defs = dirs
        valid_bases = {"cache", "bracket_code", "exact_match", "candidate", "not_found"}
        result = await lookup_retailer("テスト", "form_01", mappings, form_defs)
        assert result.basis in valid_bases

    async def test_search_product_basis_always_valid(self, dirs):
        """search_product의 basis는 항상 정의된 Literal 범위 안이다."""
        from backend.tools.mapping import search_product
        mappings, _ = dirs
        valid_bases = {"cache", "candidate", "not_found"}
        result = await search_product("テスト製品", mappings)
        assert result.basis in valid_bases

    # ── candidates 정렬 보장 ─────────────────────────────────────────────────

    async def test_lookup_retailer_candidates_sorted_descending(self, dirs):
        """lookup_retailer의 candidates는 similarity 내림차순 정렬이 보장된다."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "イオン",         "소매처코드": "A001", "판매처코드": "D1", "판매처명": "t"},
            {"소매처명": "イオンモール",   "소매처코드": "A002", "판매처코드": "D1", "판매처명": "t"},
            {"소매처명": "イオンリテール", "소매처코드": "A003", "판매처코드": "D1", "판매처명": "t"},
        ])
        from backend.tools.mapping import lookup_retailer
        # "イオングループ"はCSVに存在しない → exact_match 없음 → candidate
        result = await lookup_retailer("イオングループ", "form_02", mappings, form_defs)
        assert result.basis == "candidate"
        sims = [c["similarity"] for c in result.candidates]
        assert sims == sorted(sims, reverse=True)

    async def test_search_product_candidates_sorted_descending(self, dirs):
        """search_product의 candidates는 similarity 내림차순 정렬이 보장된다."""
        mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P1", "제품명": "テスト商品A",   "시키리": "100", "본부장": "90"},
            {"제품코드": "P2", "제품명": "テスト商品AB",  "시키리": "100", "본부장": "90"},
            {"제품코드": "P3", "제품명": "テスト商品ABC", "시키리": "100", "본부장": "90"},
        ])
        from backend.tools.mapping import search_product
        result = await search_product("テスト商品AB", mappings)
        assert result.basis == "candidate"
        sims = [c["score"] for c in result.candidates]
        assert sims == sorted(sims, reverse=True)

    # ── CSV 없음 → 예외 없음 ─────────────────────────────────────────────────

    async def test_lookup_retailer_no_csv_no_exception(self, dirs):
        """캐시·CSV 파일이 전혀 없어도 lookup_retailer는 예외 없이 not_found를 반환한다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("テスト", "form_99", mappings, form_defs)
        assert result.basis == "not_found"
        assert result.confidence == 0.0
        assert result.candidates == []

    async def test_search_product_no_csv_no_exception(self, dirs):
        """unit_price.csv가 없어도 search_product는 예외 없이 not_found를 반환한다."""
        mappings, _ = dirs
        from backend.tools.mapping import search_product
        result = await search_product("テスト製品", mappings)
        assert result.basis == "not_found"

    # ── 캐시 컬럼 누락 → 캐시 미스 처리 ────────────────────────────────────

    async def test_lookup_retailer_cache_missing_code_column_is_miss(self, dirs):
        """ocr_retailer.csv에 retailer_code 컬럼이 없는 행은 캐시 미스로 처리한다.

        (KeyError가 발생하지 않음을 검증)
        """
        mappings, form_defs = dirs
        # retailer_code 컬럼 없는 CSV
        (mappings / "ocr_retailer.csv").write_text(
            "ocr_name,something_else\nテスト,WRONG\n", encoding="utf-8-sig"
        )
        from backend.tools.mapping import lookup_retailer
        # KeyError가 발생하지 않고 not_found로 진행해야 한다
        result = await lookup_retailer("テスト", "form_01", mappings, form_defs)
        assert result.basis in ("candidate", "not_found")  # 캐시 미스 후 후보 검색 진행

    async def test_search_product_cache_missing_code_column_is_miss(self, dirs):
        """ocr_product.csv에 product_code 컬럼이 없는 행은 캐시 미스로 처리한다."""
        mappings, _ = dirs
        (mappings / "ocr_product.csv").write_text(
            "ocr_name,wrong_col\n農心 辛ラーメン,BAD\n", encoding="utf-8-sig"
        )
        from backend.tools.mapping import search_product
        result = await search_product("農心 辛ラーメン", mappings)
        # KeyError 없이 not_found 또는 candidate로 진행
        assert result.basis in ("candidate", "not_found")

    # ── confirm_mapping Contract ─────────────────────────────────────────────

    async def test_confirm_mapping_always_returns_none(self, dirs):
        """confirm_mapping은 성공 시 항상 None을 반환한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        ret = await confirm_mapping(
            mapping_type="retailer",
            ocr_name="テスト",
            confirmed_code="R001",
            context={},
            mappings_dir=mappings,
        )
        assert ret is None

    async def test_confirm_mapping_dist_missing_form_id_raises_valueerror(self, dirs):
        """dist mapping_type에서 form_id가 없으면 KeyError가 아닌 ValueError가 발생한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        with pytest.raises(ValueError, match="context 키"):
            await confirm_mapping(
                mapping_type="dist",
                ocr_name="テスト",
                confirmed_code="D001",
                context={
                    # form_id, issuer_fingerprint, retailer_code 전부 누락
                },
                mappings_dir=mappings,
            )

    async def test_confirm_mapping_dist_partial_missing_key_raises_valueerror(self, dirs):
        """dist mapping_type에서 일부 필수 키가 없어도 ValueError가 발생한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        with pytest.raises(ValueError, match="context 키"):
            await confirm_mapping(
                mapping_type="dist",
                ocr_name="テスト",
                confirmed_code="D001",
                context={
                    "form_id": "form_04",
                    # issuer_fingerprint, retailer_code 누락
                },
                mappings_dir=mappings,
            )


# ── 10. CSV Lock 안전성 ───────────────────────────────────────────────────────

class TestCsvLockSafety:
    """asyncio.Lock 기반 CSV write 직렬화 동시성 안전성 검증.

    목표:
      - 같은 CSV 파일에 대한 asyncio.gather 동시 호출에서 row 유실 없음
      - 같은 key에 대한 동시 upsert 시 중복 row 없음
      - 다른 파일 → 다른 lock 객체 (병렬 가능)
      - 같은 파일 → 같은 lock 객체 (직렬화)
      - lock key = path.resolve() (canonical path 기준)
    """

    # ── Lock Registry 단위 테스트 ─────────────────────────────────────────────

    def test_same_file_same_lock_instance(self, dirs):
        """같은 파일 경로는 항상 동일한 Lock 객체를 반환한다."""
        from backend.tools.mapping import _get_csv_lock
        mappings, _ = dirs
        path = mappings / "ocr_retailer.csv"
        assert _get_csv_lock(path) is _get_csv_lock(path)

    def test_different_files_different_lock_instances(self, dirs):
        """서로 다른 파일은 독립된 Lock 객체를 갖는다 (병렬 실행 가능)."""
        from backend.tools.mapping import _get_csv_lock
        mappings, _ = dirs
        lock_retailer = _get_csv_lock(mappings / "ocr_retailer.csv")
        lock_product  = _get_csv_lock(mappings / "ocr_product.csv")
        lock_dist     = _get_csv_lock(mappings / "ocr_dist.csv")
        assert lock_retailer is not lock_product
        assert lock_retailer is not lock_dist
        assert lock_product  is not lock_dist

    def test_lock_key_uses_resolved_path(self, tmp_path):
        """path.resolve()로 정규화된 경로를 lock key로 사용한다.

        서로 다른 path 표현이 같은 파일을 가리키면 동일 Lock을 반환해야 한다.
        """
        from backend.tools.mapping import _get_csv_lock
        subdir = tmp_path / "sub"
        subdir.mkdir()
        # 직접 경로와 '..' 경유 경로가 같은 파일을 가리킴
        path_direct    = tmp_path / "test.csv"
        path_traversal = subdir / ".." / "test.csv"
        assert _get_csv_lock(path_direct) is _get_csv_lock(path_traversal)

    # ── 동시성 테스트 — 다른 key, 같은 CSV ───────────────────────────────────

    async def test_concurrent_writes_no_data_loss(self, dirs):
        """25개 서로 다른 key를 동시에 confirm_mapping → row 유실 없음.

        asyncio.gather로 동시 실행 시 lock이 write를 직렬화해
        read-modify-write 경쟁이 발생하지 않는다.
        """
        import asyncio as _asyncio
        import csv as _csv
        from backend.tools.mapping import confirm_mapping
        mappings, _ = dirs

        n = 25
        tasks = [
            confirm_mapping(
                mapping_type="retailer",
                ocr_name=f"テスト店舗{i:04d}",
                confirmed_code=f"R{i:04d}",
                context={},
                mappings_dir=mappings,
            )
            for i in range(n)
        ]
        await _asyncio.gather(*tasks)

        rows = list(_csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        assert len(rows) == n, f"행 유실 발생: 기대 {n}, 실제 {len(rows)}"
        codes = {r["retailer_code"] for r in rows}
        assert codes == {f"R{i:04d}" for i in range(n)}, "코드 유실 또는 중복"

    async def test_concurrent_same_key_no_duplicate_row(self, dirs):
        """같은 ocr_name으로 동시에 10회 upsert → row 중복 없음 (upsert 의미 유지).

        마지막으로 쓴 writer의 코드가 남는다 (last-writer-wins).
        """
        import asyncio as _asyncio
        import csv as _csv
        from backend.tools.mapping import confirm_mapping
        mappings, _ = dirs

        ocr_name = "重複テスト店舗"
        tasks = [
            confirm_mapping(
                mapping_type="retailer",
                ocr_name=ocr_name,
                confirmed_code=f"R{i:04d}",
                context={},
                mappings_dir=mappings,
            )
            for i in range(10)
        ]
        await _asyncio.gather(*tasks)

        rows = list(_csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        names = [r["ocr_name"] for r in rows]
        assert names.count(ocr_name) == 1, f"중복 row 발생: {names.count(ocr_name)}건"
        assert len(rows) == 1

    # ── 동시성 테스트 — 다른 CSV 파일 병렬 실행 ──────────────────────────────

    async def test_concurrent_different_csv_files_no_loss(self, dirs):
        """retailer·product CSV에 동시 쓰기 (20건+20건) → 각각 유실 없음.

        서로 다른 파일 = 서로 다른 lock → 실제 병렬 실행.
        """
        import asyncio as _asyncio
        import csv as _csv
        from backend.tools.mapping import confirm_mapping
        mappings, _ = dirs

        n = 20
        retailer_tasks = [
            confirm_mapping("retailer", f"店舗{i:03d}", f"R{i:03d}", {}, mappings)
            for i in range(n)
        ]
        product_tasks = [
            confirm_mapping("product", f"商品{i:03d}", f"P{i:03d}", {}, mappings)
            for i in range(n)
        ]
        await _asyncio.gather(*retailer_tasks, *product_tasks)

        retailer_rows = list(_csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        product_rows = list(_csv.DictReader(
            (mappings / "ocr_product.csv").open(encoding="utf-8-sig")
        ))
        assert len(retailer_rows) == n, f"retailer 행 유실: {len(retailer_rows)}/{n}"
        assert len(product_rows) == n, f"product 행 유실: {len(product_rows)}/{n}"

    # ── 이벤트 루프 블로킹 비차단 검증 ───────────────────────────────────────

    async def test_concurrent_writes_event_loop_not_blocked(self, dirs):
        """asyncio.to_thread 사용으로 이벤트 루프가 차단되지 않는다.

        기준: 50건 동시 쓰기 중에도 asyncio.sleep(0)이 즉시 실행되는지 검증.
        진정한 이벤트 루프 블로킹이 있으면 sleep 해결에 시간이 걸린다.
        """
        import asyncio as _asyncio
        import csv as _csv
        from backend.tools.mapping import confirm_mapping
        mappings, _ = dirs

        yielded = []

        async def yield_task():
            await _asyncio.sleep(0)
            yielded.append(True)

        n = 50
        write_tasks = [
            confirm_mapping("retailer", f"店舗{i:03d}", f"R{i:03d}", {}, mappings)
            for i in range(n)
        ]
        await _asyncio.gather(*write_tasks, yield_task())

        # yield_task가 반드시 실행됐어야 한다
        assert len(yielded) == 1, "이벤트 루프가 블로킹됨 — asyncio.sleep(0)이 실행 안 됨"

        rows = list(_csv.DictReader(
            (mappings / "ocr_retailer.csv").open(encoding="utf-8-sig")
        ))
        assert len(rows) == n
