"""test_tools_metrics.py — Tool Metrics Layer 단위 테스트

실행: pytest tests/test_tools_metrics.py -v
"""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.tools.metrics import ToolMetrics, get_metrics, reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    """각 테스트 전후로 메트릭을 초기화해 테스트 간 간섭을 방지한다."""
    reset_metrics()
    yield
    reset_metrics()


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def dirs(tmp_path: Path):
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_definitions"
    mappings.mkdir()
    form_defs.mkdir()
    return mappings, form_defs


# ── 1. get_metrics / reset_metrics API ───────────────────────────────────────

class TestMetricsAPI:
    def test_get_metrics_all_returns_dict(self):
        result = get_metrics()
        assert isinstance(result, dict)

    def test_get_metrics_all_has_three_tools(self):
        result = get_metrics()
        assert set(result.keys()) == {"lookup_retailer", "search_product", "confirm_mapping"}

    def test_get_metrics_all_values_are_toolmetrics(self):
        for v in get_metrics().values():
            assert isinstance(v, ToolMetrics)

    def test_get_metrics_specific_tool(self):
        m = get_metrics("lookup_retailer")
        assert isinstance(m, ToolMetrics)

    def test_get_metrics_specific_unknown_raises_keyerror(self):
        with pytest.raises(KeyError, match="알 수 없는 Tool"):
            get_metrics("nonexistent")

    def test_get_metrics_returns_snapshot_not_live(self):
        """get_metrics()는 스냅샷을 반환하므로 이후 변경이 반영되지 않는다."""
        snap = get_metrics("lookup_retailer")
        # 내부 state를 직접 바꾸는 방법 없이 함수 호출로 변경
        from backend.tools.metrics import _METRICS
        _METRICS["lookup_retailer"].calls += 99
        assert snap.calls == 0  # 스냅샷은 불변

    def test_reset_metrics_all(self):
        from backend.tools.metrics import _METRICS
        _METRICS["lookup_retailer"].calls = 5
        _METRICS["search_product"].calls = 3
        reset_metrics()
        assert get_metrics("lookup_retailer").calls == 0
        assert get_metrics("search_product").calls == 0

    def test_reset_metrics_specific_tool(self):
        from backend.tools.metrics import _METRICS
        _METRICS["lookup_retailer"].calls = 10
        _METRICS["search_product"].calls = 7
        reset_metrics("lookup_retailer")
        assert get_metrics("lookup_retailer").calls == 0
        assert get_metrics("search_product").calls == 7  # 변경 없음

    def test_reset_metrics_unknown_raises_keyerror(self):
        with pytest.raises(KeyError):
            reset_metrics("nonexistent")

    def test_initial_metrics_all_zero(self):
        m = get_metrics("lookup_retailer")
        assert m.calls == 0
        assert m.success == 0
        assert m.failures == 0
        assert m.cache_hits == 0
        assert m.not_found == 0


# ── 2. lookup_retailer 메트릭 ─────────────────────────────────────────────────

class TestLookupRetailerMetrics:
    async def test_cache_hit_increments_cache_hits_and_success(self, dirs):
        """캐시 히트 시 cache_hits와 success가 증가한다."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "テスト店", "retailer_code": "R001", "retailer_name": "テスト"},
        ])
        from backend.tools.mapping import lookup_retailer
        await lookup_retailer("テスト店", "form_01", mappings, form_defs)

        m = get_metrics("lookup_retailer")
        assert m.calls == 1
        assert m.cache_hits == 1
        assert m.success == 1
        assert m.not_found == 0
        assert m.failures == 0

    async def test_bracket_code_hit_increments_cache_hits(self, dirs):
        """괄호코드 직접 매칭도 cache_hits로 집계된다 (결정적 조회)."""
        mappings, form_defs = dirs
        (form_defs / "form_01.md").write_text(
            "## データソース\nbracket_code_csv: domae_retail_1.csv\n- domae_retail_1.csv\n",
            encoding="utf-8",
        )
        write_csv(mappings / "domae_retail_1.csv", [
            {"도매소매처코드": "32423", "소매처코드": "6003851"},
        ])
        from backend.tools.mapping import lookup_retailer
        await lookup_retailer("テスト店 (32423)", "form_01", mappings, form_defs)

        m = get_metrics("lookup_retailer")
        assert m.calls == 1
        assert m.cache_hits == 1
        assert m.success == 1

    async def test_candidate_increments_success_not_cache_hits(self, dirs):
        """candidate 결과는 success만 증가, cache_hits는 0이다."""
        mappings, form_defs = dirs
        (form_defs / "form_02.md").write_text(
            "## データソース\n- retail_user.csv\n", encoding="utf-8"
        )
        write_csv(mappings / "retail_user.csv", [
            {"소매처명": "ファミリーマート", "소매처코드": "R001", "판매처코드": "D1", "판매처명": "t"},
        ])
        from backend.tools.mapping import lookup_retailer
        result = await lookup_retailer("ファミリーマート", "form_02", mappings, form_defs)
        assert result.basis == "candidate"

        m = get_metrics("lookup_retailer")
        assert m.calls == 1
        assert m.success == 1
        assert m.cache_hits == 0
        assert m.not_found == 0

    async def test_not_found_increments_not_found(self, dirs):
        """조회 실패 시 not_found가 증가하고 success는 0이다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer
        await lookup_retailer("ZZZZZZ존재하지않음", "form_01", mappings, form_defs)

        m = get_metrics("lookup_retailer")
        assert m.calls == 1
        assert m.not_found == 1
        assert m.success == 0
        assert m.cache_hits == 0

    async def test_calls_accumulates_across_multiple_calls(self, dirs):
        """여러 번 호출 시 calls가 누적 증가한다."""
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer
        for _ in range(3):
            await lookup_retailer("없는가게", "form_01", mappings, form_defs)

        assert get_metrics("lookup_retailer").calls == 3

    async def test_calls_equals_success_plus_not_found(self, dirs):
        """calls == success + not_found (정상 흐름에서 failures=0)."""
        mappings, form_defs = dirs
        write_csv(mappings / "ocr_retailer.csv", [
            {"ocr_name": "テスト店", "retailer_code": "R001", "retailer_name": "テスト"},
        ])
        from backend.tools.mapping import lookup_retailer
        await lookup_retailer("テスト店", "form_01", mappings, form_defs)   # cache hit
        await lookup_retailer("없는가게", "form_01", mappings, form_defs)   # not_found

        m = get_metrics("lookup_retailer")
        assert m.calls == 2
        assert m.calls == m.success + m.not_found + m.failures


# ── 3. search_product 메트릭 ──────────────────────────────────────────────────

class TestSearchProductMetrics:
    async def test_cache_hit_increments_cache_hits(self, dirs):
        mappings, _ = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "農心 辛ラーメン", "product_code": "P001", "product_name": "辛ラーメン"},
        ])
        from backend.tools.mapping import search_product
        await search_product("農心 辛ラーメン", mappings)

        m = get_metrics("search_product")
        assert m.calls == 1
        assert m.cache_hits == 1
        assert m.success == 1

    async def test_candidate_increments_success_not_cache_hits(self, dirs):
        mappings, _ = dirs
        write_csv(mappings / "unit_price.csv", [
            {"제품코드": "P001", "제품명": "農心 辛ラーメン 120g", "시키리": "100", "본부장": "90"},
        ])
        from backend.tools.mapping import search_product
        result = await search_product("農心 辛ラーメン 120g", mappings)
        assert result.basis == "candidate"

        m = get_metrics("search_product")
        assert m.calls == 1
        assert m.success == 1
        assert m.cache_hits == 0

    async def test_not_found_increments_not_found(self, dirs):
        mappings, _ = dirs
        from backend.tools.mapping import search_product
        await search_product("存在しない製品ZZZZZ", mappings)

        m = get_metrics("search_product")
        assert m.calls == 1
        assert m.not_found == 1
        assert m.success == 0

    async def test_calls_equals_success_plus_not_found(self, dirs):
        mappings, _ = dirs
        write_csv(mappings / "ocr_product.csv", [
            {"ocr_name": "テスト製品", "product_code": "P001", "product_name": "テスト"},
        ])
        from backend.tools.mapping import search_product
        await search_product("テスト製品", mappings)   # cache hit
        await search_product("ZZZZZZ", mappings)       # not_found

        m = get_metrics("search_product")
        assert m.calls == 2
        assert m.calls == m.success + m.not_found + m.failures


# ── 4. confirm_mapping 메트릭 ─────────────────────────────────────────────────

class TestConfirmMappingMetrics:
    async def test_success_increments_success(self, dirs):
        """정상 저장 시 success가 증가한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        await confirm_mapping(
            mapping_type="retailer",
            ocr_name="テスト",
            confirmed_code="R001",
            context={"retailer_name": "テスト店"},
            mappings_dir=mappings,
        )

        m = get_metrics("confirm_mapping")
        assert m.calls == 1
        assert m.success == 1
        assert m.failures == 0

    async def test_invalid_mapping_type_increments_failure(self, dirs):
        """알 수 없는 mapping_type → ValueError → failures가 증가한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        with pytest.raises(ValueError):
            await confirm_mapping(
                mapping_type="unknown",  # type: ignore
                ocr_name="テスト",
                confirmed_code="X001",
                context={},
                mappings_dir=mappings,
            )

        m = get_metrics("confirm_mapping")
        assert m.calls == 1
        assert m.failures == 1
        assert m.success == 0

    async def test_dist_missing_context_key_increments_failure(self, dirs):
        """dist의 필수 context 키 누락 → ValueError → failures가 증가한다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        with pytest.raises(ValueError):
            await confirm_mapping(
                mapping_type="dist",
                ocr_name="テスト",
                confirmed_code="D001",
                context={"form_id": "form_04"},   # issuer_fingerprint, retailer_code 누락
                mappings_dir=mappings,
            )

        m = get_metrics("confirm_mapping")
        assert m.calls == 1
        assert m.failures == 1
        assert m.success == 0

    async def test_multiple_successes_accumulate(self, dirs):
        """여러 번 성공 호출 시 success와 calls가 누적된다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        for i in range(3):
            await confirm_mapping(
                mapping_type="retailer",
                ocr_name=f"店舗{i}",
                confirmed_code=f"R00{i}",
                context={},
                mappings_dir=mappings,
            )

        m = get_metrics("confirm_mapping")
        assert m.calls == 3
        assert m.success == 3
        assert m.failures == 0

    async def test_calls_equals_success_plus_failures(self, dirs):
        """confirm_mapping: calls == success + failures."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        await confirm_mapping("product", "製品A", "P001", {}, mappings)
        with pytest.raises(ValueError):
            await confirm_mapping("invalid", "テスト", "X001", {}, mappings)  # type: ignore

        m = get_metrics("confirm_mapping")
        assert m.calls == 2
        assert m.calls == m.success + m.failures

    async def test_cache_hits_always_zero_for_confirm(self, dirs):
        """confirm_mapping은 쓰기 전용 — cache_hits는 항상 0이다."""
        mappings, _ = dirs
        from backend.tools.mapping import confirm_mapping
        await confirm_mapping("retailer", "テスト", "R001", {}, mappings)

        m = get_metrics("confirm_mapping")
        assert m.cache_hits == 0
        assert m.not_found == 0


# ── 5. 독립성: Tool별 메트릭이 서로 간섭하지 않는다 ─────────────────────────

class TestMetricsIsolation:
    async def test_lookup_does_not_affect_search_metrics(self, dirs):
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer
        await lookup_retailer("テスト", "form_01", mappings, form_defs)

        assert get_metrics("search_product").calls == 0
        assert get_metrics("confirm_mapping").calls == 0

    async def test_search_does_not_affect_lookup_metrics(self, dirs):
        mappings, _ = dirs
        from backend.tools.mapping import search_product
        await search_product("テスト製品", mappings)

        assert get_metrics("lookup_retailer").calls == 0
        assert get_metrics("confirm_mapping").calls == 0

    async def test_reset_one_tool_preserves_others(self, dirs):
        mappings, form_defs = dirs
        from backend.tools.mapping import lookup_retailer, search_product
        await lookup_retailer("テスト", "form_01", mappings, form_defs)
        await search_product("テスト製品", mappings)

        reset_metrics("lookup_retailer")

        assert get_metrics("lookup_retailer").calls == 0
        assert get_metrics("search_product").calls == 1  # 유지
