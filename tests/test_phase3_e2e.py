"""test_phase3_e2e.py — Phase3 Tool Use E2E 통합 테스트

실제 tmp_path CSV 기반으로 run_phase3_with_tool_use_or_fallback()의
성공/fallback 경로를 완전히 통과시켜 검증한다.

Mock 대상:
  - run_batch_retailer_experiment: Claude API 없이 제어된 BatchExperimentResult 반환
  - _record_tool_use_token_usage: asyncpg 없이 DB 기록 우회
  - run_phase3 (fallback 테스트 전용): legacy 경로 제어

실제 실행 대상:
  - _execute_success_path() 전체
  - _batch_result_to_retailer_decisions()
  - _build_product_decisions_with_tool_use() → search_product cache 조회
  - build_dist_resolution_from_cache() → retail_user.csv 읽기
  - convert_tool_use_result_to_phase3_output()
  - confirm_mapping() → ocr_retailer.csv, ocr_dist.csv 실제 쓰기
  - phase3_output.json 파일 쓰기

실행: pytest tests/test_phase3_e2e.py -v
"""
import csv
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.experiments.batch_tool_use_experiment import (
    BatchExperimentResult,
    BatchStats,
    RetailerBatchResult,
)
from backend.pipeline.phase3_fallback import (
    ToolUseMaxTurnsError,
    ToolUseTokenStats,
    run_phase3_with_tool_use_or_fallback,
)


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _mock_settings(mappings: Path, form_defs: Path) -> MagicMock:
    s = MagicMock()
    s.mappings_dir                = mappings
    s.form_definitions_dir        = form_defs
    # non-empty so _attempt_tool_use_phase creates a client and calls run_batch_retailer_experiment
    # (which is mocked in E2E tests — real API call never occurs)
    s.anthropic_api_key           = "fake-key-for-testing"
    s.phase3_tool_use_model       = "claude-haiku-4-5-20251001"
    s.phase3_tool_use_concurrency = 1
    return s


# ── form_01.md 최소 정의 ───────────────────────────────────────────────────────

_FORM_01_MD = """\
# form_01

## issuer 식별

```
fingerprint_fields: name
```
"""

# ── 테스트용 phase2 결과 ──────────────────────────────────────────────────────

_PHASE2 = {
    "pages": [
        {"role": "cover", "issuer": {"name": "テスト発行者"}},
    ],
    "items": [
        {
            "customer": "テスト店A",
            "product":  "辛ラーメン",
            "item_type": "条件",
            "columns":  {"金額": 1000},
        },
    ],
}

# ── Fixture ────────────────────────────────────────────────────────────────────

@pytest.fixture
def e2e_dirs(tmp_path: Path):
    """E2E 테스트용 CSV / form_definitions 디렉토리 구성."""
    mappings = tmp_path / "mappings"
    form_defs = tmp_path / "form_defs"
    output = tmp_path / "extracted" / "doc_e2e"

    mappings.mkdir()
    form_defs.mkdir()
    output.mkdir(parents=True)

    # retail_user.csv: R001 → D001 1:1 매핑 (dist auto_1_to_1)
    _write_csv(mappings / "retail_user.csv", [
        {"소매처코드": "R001", "소매처명": "テスト小売",
         "판매처코드": "D001", "판매처명": "東日本販社"},
    ])

    # ocr_product.csv: 辛ラーメン → P001 캐시 히트
    _write_csv(mappings / "ocr_product.csv", [
        {"ocr_name": "辛ラーメン", "product_code": "P001", "product_name": "신라면"},
    ])

    # form_01.md: fingerprint_fields=name
    (form_defs / "form_01.md").write_text(_FORM_01_MD, encoding="utf-8")

    return mappings, form_defs, output


# ── 성공 BatchExperimentResult 팩토리 ─────────────────────────────────────────

def _confirmed_batch_result(ocr_name: str, retailer_code: str) -> BatchExperimentResult:
    """단일 retailer가 Tool Use로 확정된 BatchExperimentResult."""
    return BatchExperimentResult(
        scenario="success",
        batch_size=1,
        stats=BatchStats(
            batch_size=1, success_count=1, failure_count=0,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=2, total_lookup_calls=1, total_confirm_calls=1,
            total_turns=3, avg_turns=3.0, elapsed_ms=100.0,
            total_input_tokens=300, total_output_tokens=120, total_api_calls=3,
        ),
        per_retailer=[
            RetailerBatchResult(
                ocr_name=ocr_name, success=True, confirmed_code=retailer_code,
                lookup_basis="candidate",
                tool_call_count=2, lookup_call_count=1, confirm_call_count=1,
                turns_used=3, max_turns_hit=False, elapsed_ms=80.0,
                input_tokens=300, output_tokens=120, api_call_count=3,
            )
        ],
    )


# ── 1. Tool Use 성공 경로 E2E ─────────────────────────────────────────────────

class TestToolUseSuccessE2E:
    """실제 CSV 파일 기반 Tool Use 성공 경로 전체 통과 검증.

    run_batch_retailer_experiment만 mock — 나머지는 실제 실행.
    """

    async def test_phase3_output_json_created(self, e2e_dirs):
        """Tool Use 성공 시 phase3_output.json이 output_dir에 생성된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert (output / "phase3_output.json").exists(), "phase3_output.json 미생성"

    async def test_confirmed_retailers_has_r001(self, e2e_dirs):
        """confirmed_retailers에 テスト店A → R001 / D001 / tool_use basis가 기록된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        cr = result["confirmed_retailers"]
        assert "テスト店A" in cr, f"confirmed_retailers에 テスト店A 없음: {cr}"
        assert cr["テスト店A"]["retailer_code"] == "R001"
        assert cr["テスト店A"]["dist_code"]     == "D001"   # retail_user.csv 1:1
        assert cr["テスト店A"]["basis"]         == "tool_use"

    async def test_confirmed_products_from_cache_hit(self, e2e_dirs):
        """ocr_product.csv 캐시 히트로 confirmed_products에 P001이 기록된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        cp = result["confirmed_products"]
        assert "辛ラーメン" in cp, f"confirmed_products에 辛ラーメン 없음: {cp}"
        assert cp["辛ラーメン"]["code"]  == "P001"
        assert cp["辛ラーメン"]["basis"] == "cache"

    async def test_items_have_retailer_and_product_codes_applied(self, e2e_dirs):
        """items[].retailer_code == R001, items[].product_code == P001."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["retailer_code"] == "R001"
        assert item["product_code"]  == "P001"

    async def test_no_pending_when_all_confirmed(self, e2e_dirs):
        """retailer R001 / product P001 모두 확정 + dist 1:1 → pending 없음."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            _, pending, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert pending == [], f"예상치 못한 pending: {pending}"

    async def test_stats_not_fallback_and_token_usage_set(self, e2e_dirs):
        """성공 시 fallback_triggered=False, token_usage에 retailer 수치 누적."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert stats.fallback_triggered is False
        assert stats.used_tool_use is True
        # batch_result.stats에서 누적
        assert stats.token_usage.retailer_input_tokens  == 300
        assert stats.token_usage.retailer_output_tokens == 120
        assert stats.token_usage.retailer_api_calls     == 3

    async def test_confirm_mapping_writes_retailer_csv(self, e2e_dirs):
        """_execute_success_path의 confirm_mapping이 ocr_retailer.csv에 기록한다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        retailer_csv = mappings / "ocr_retailer.csv"
        assert retailer_csv.exists(), "ocr_retailer.csv 미생성"
        rows = _read_csv_rows(retailer_csv)
        matched = [r for r in rows if r.get("ocr_name") == "テスト店A"]
        assert len(matched) == 1, f"テスト店A 미기록: {rows}"
        assert matched[0]["retailer_code"] == "R001"

    async def test_confirm_mapping_writes_dist_csv(self, e2e_dirs):
        """1:1 dist 자동 확정으로 ocr_dist.csv에 R001 → D001이 기록된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        dist_csv = mappings / "ocr_dist.csv"
        assert dist_csv.exists(), "ocr_dist.csv 미생성"
        rows = _read_csv_rows(dist_csv)
        matched = [r for r in rows
                   if r.get("retailer_code") == "R001" and r.get("dist_code") == "D001"]
        assert len(matched) == 1, f"R001→D001 미기록: {rows}"

    async def test_saved_json_matches_returned_result(self, e2e_dirs):
        """phase3_output.json 파일 내용이 반환된 result dict와 동일하다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        saved = json.loads((output / "phase3_output.json").read_text(encoding="utf-8"))
        assert saved == result

    async def test_result_schema_has_required_keys(self, e2e_dirs):
        """result에 phase4가 기대하는 최소 키 구조가 갖춰진다."""
        mappings, form_defs, output = e2e_dirs
        required_keys = {
            "doc_id", "form_id", "hatsu_month", "issuer",
            "confirmed_retailers", "confirmed_products", "items", "cover_totals",
        }

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        missing = required_keys - set(result.keys())
        assert not missing, f"필수 키 누락: {missing}"


# ── 2. Tool Use fallback 경로 E2E ─────────────────────────────────────────────

class TestToolUseFallbackE2E:
    """Tool Use 실패 → legacy fallback 경로 검증."""

    _LEGACY_RESULT = {
        "doc_id": "doc_e2e", "form_id": "form_01", "hatsu_month": "2025-01",
        "issuer": {}, "confirmed_retailers": {}, "confirmed_products": {},
        "items": [], "cover_totals": {},
    }

    @staticmethod
    def _max_turns_batch_result(
        input_tokens: int = 0, output_tokens: int = 0, api_calls: int = 0,
    ) -> BatchExperimentResult:
        """max_turns_hit_count=1인 BatchExperimentResult → _attempt_tool_use_phase가
        ToolUseMaxTurnsError(partial_token_stats)를 raise한다."""
        return BatchExperimentResult(
            scenario="max_turns",
            batch_size=1,
            stats=BatchStats(
                batch_size=1, success_count=0, failure_count=0,
                max_turns_hit_count=1, not_found_count=0,
                total_tool_calls=0, total_lookup_calls=0, total_confirm_calls=0,
                total_turns=0, avg_turns=0.0, elapsed_ms=0.0,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                total_api_calls=api_calls,
            ),
            per_retailer=[
                RetailerBatchResult(
                    ocr_name="テスト店A", success=False, confirmed_code=None,
                    lookup_basis=None,
                    tool_call_count=0, lookup_call_count=0, confirm_call_count=0,
                    turns_used=5, max_turns_hit=True, elapsed_ms=50.0,
                )
            ],
        )

    async def test_fallback_triggered_on_max_turns(self, e2e_dirs):
        """max_turns_hit_count=1 BatchResult → ToolUseMaxTurnsError → fallback."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=self._max_turns_batch_result())), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=(self._LEGACY_RESULT, []))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert stats.fallback_triggered is True
        assert stats.max_turns_hit is True
        assert stats.fallback_class == "ToolUseMaxTurnsError"

    async def test_fallback_returns_legacy_result(self, e2e_dirs):
        """fallback 시 legacy run_phase3의 결과가 반환된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=self._max_turns_batch_result())), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=(self._LEGACY_RESULT, []))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, pending, _ = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert result == self._LEGACY_RESULT
        assert pending == []

    async def test_fallback_partial_token_stats_preserved(self, e2e_dirs):
        """batch stats token이 partial_token_stats → stats.token_usage에 복사된다."""
        mappings, form_defs, output = e2e_dirs

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=self._max_turns_batch_result(
                       input_tokens=250, output_tokens=100, api_calls=3,
                   ))), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=(self._LEGACY_RESULT, []))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert stats.token_usage.retailer_input_tokens  == 250
        assert stats.token_usage.retailer_output_tokens == 100
        assert stats.token_usage.retailer_api_calls     == 3

    async def test_fallback_token_record_called_with_partial_stats(self, e2e_dirs):
        """fallback 시에도 _record_tool_use_token_usage가 partial 값으로 호출된다."""
        mappings, form_defs, output = e2e_dirs
        mock_record = AsyncMock()

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=self._max_turns_batch_result(
                       input_tokens=100, api_calls=1,
                   ))), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=(self._LEGACY_RESULT, []))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=mock_record):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        mock_record.assert_called_once()
        _, _, token_arg = mock_record.call_args[0]
        assert token_arg.retailer_input_tokens == 100

    async def test_fallback_does_not_write_tool_use_json(self, e2e_dirs):
        """_attempt_tool_use_phase 실패 → phase3_output.json을 Tool Use가 쓰지 않는다."""
        mappings, form_defs, output = e2e_dirs
        exc = ToolUseMaxTurnsError("max_turns 초과")

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=(self._LEGACY_RESULT, []))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        # _attempt_tool_use_phase 실패 → _execute_success_path 미실행
        # → phase3_output.json 없음 (mock legacy도 파일을 쓰지 않음)
        assert not (output / "phase3_output.json").exists()

    async def test_fallback_legacy_json_overwrites_any_tool_use_json(self, e2e_dirs):
        """legacy run_phase3가 파일을 쓰면 그 내용이 최종 phase3_output.json이 된다."""
        mappings, form_defs, output = e2e_dirs

        legacy_result = dict(self._LEGACY_RESULT)
        legacy_result["confirmed_retailers"] = {
            "legacy_only": {"retailer_code": "L001", "dist_code": "", "basis": "cache"}
        }

        exc = ToolUseMaxTurnsError("max_turns 초과")

        async def _fake_legacy(*args, **kwargs):
            (output / "phase3_output.json").write_text(
                json.dumps(legacy_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return legacy_result, []

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=_fake_legacy), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        assert stats.fallback_triggered is True

        saved = json.loads((output / "phase3_output.json").read_text(encoding="utf-8"))
        assert "legacy_only" in saved["confirmed_retailers"]
        assert saved["confirmed_retailers"]["legacy_only"]["retailer_code"] == "L001"


# ── 3. Feature flag OFF 경로 E2E ─────────────────────────────────────────────

class TestFeatureFlagOffE2E:
    """enable_tool_use=False 시 Tool Use를 전혀 거치지 않고 legacy 직행."""

    async def test_flag_off_skips_tool_use_entirely(self, e2e_dirs):
        """enable_tool_use=False → stats.used_tool_use=False, fallback 아님."""
        mappings, form_defs, output = e2e_dirs

        mock_legacy = AsyncMock(return_value=(
            {"doc_id": "doc_e2e", "form_id": "form_01", "hatsu_month": "",
             "issuer": {}, "confirmed_retailers": {}, "confirmed_products": {},
             "items": [], "cover_totals": {}},
            [],
        ))
        mock_batch = AsyncMock()

        with patch("backend.pipeline.phase3_fallback.run_phase3", new=mock_legacy), \
             patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=mock_batch):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="",
                enable_tool_use=False,
                settings=_mock_settings(mappings, form_defs),
            )

        mock_legacy.assert_called_once()
        mock_batch.assert_not_called()
        assert stats.used_tool_use is False
        assert stats.fallback_triggered is False
        assert stats.enable_tool_use is False


# ── 4. dist 1:N pending 경로 ─────────────────────────────────────────────────

class TestDist1toNPendingE2E:
    """retail_user.csv에 1:N이면 dist pending이 생성된다."""

    async def test_dist_1ton_creates_pending_entry(self, e2e_dirs):
        """R001에 두 판매처가 있으면 dist pending 1건이 반환된다."""
        mappings, form_defs, output = e2e_dirs

        # retail_user.csv 덮어쓰기 (1:N)
        _write_csv(mappings / "retail_user.csv", [
            {"소매처코드": "R001", "소매처명": "テスト小売",
             "판매처코드": "D001", "판매처명": "東日本販社"},
            {"소매처코드": "R001", "소매처명": "テスト小売",
             "판매처코드": "D002", "판매처명": "西日本販社"},
        ])

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=_confirmed_batch_result("テスト店A", "R001"))), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01", run_id="run_test",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        # dist 1:N → pending 1건, fallback 아님
        dist_pending = [p for p in pending if p["mapping_type"] == "dist"]
        assert len(dist_pending) == 1, f"dist pending 1건 기대: {pending}"
        assert dist_pending[0]["ocrName"] == "テスト店A"
        assert len(dist_pending[0]["candidates"]) == 2

        # retailer_code는 확정됨 (dist_code는 빈 문자열)
        assert result["confirmed_retailers"]["テスト店A"]["retailer_code"] == "R001"
        assert result["confirmed_retailers"]["テスト店A"]["dist_code"] == ""

        # fallback 아님
        assert stats.fallback_triggered is False


# ── 5. Production path 검증 ──────────────────────────────────────────────────

class TestProductionPathNotMockClient:
    """운영 경로가 Mock/Scenario client를 사용하지 않음을 검증한다."""

    async def test_no_api_key_returns_pending_not_fallback(self, e2e_dirs):
        """ANTHROPIC_API_KEY 없으면 retailer가 pending으로 처리되고 fallback이 아니다."""
        mappings, form_defs, output = e2e_dirs

        s = MagicMock()
        s.mappings_dir                = mappings
        s.form_definitions_dir        = form_defs
        s.anthropic_api_key           = ""  # 의도적으로 비움
        s.phase3_tool_use_model       = "claude-haiku-4-5-20251001"
        s.phase3_tool_use_concurrency = 1

        with patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01",
                enable_tool_use=True,
                settings=s,
            )

        # API key 없음 → retailer Tool Use 미실행 (batch_result=None) → fallback 아님
        assert stats.fallback_triggered is False
        assert stats.used_tool_use is True
        # confirmed_retailers 비어 있음 (batch_result=None → retailer_decisions=[])
        assert result["confirmed_retailers"] == {}, \
            f"API key 없을 때 confirmed_retailers가 비어 있어야 함: {result['confirmed_retailers']}"
        # items의 retailer_code가 비어 있음 (unconfirmed=True)
        for item in result["items"]:
            assert item.get("retailer_code") == "" or not item.get("retailer_code"), \
                f"API key 없을 때 retailer_code가 설정됨: {item.get('retailer_code')}"

    async def test_run_batch_experiment_receives_real_client(self, e2e_dirs):
        """_attempt_tool_use_phase가 run_batch_retailer_experiment에 non-None client를 전달한다."""
        mappings, form_defs, output = e2e_dirs

        captured_client = []

        async def _capture_client(*args, **kwargs):
            captured_client.append(kwargs.get("client"))
            # minimal successful result
            from backend.experiments.batch_tool_use_experiment import (
                BatchExperimentResult, BatchStats,
            )
            return BatchExperimentResult(
                scenario="success", batch_size=1,
                stats=BatchStats(
                    batch_size=1, success_count=1, failure_count=0,
                    max_turns_hit_count=0, not_found_count=0,
                    total_tool_calls=2, total_lookup_calls=1, total_confirm_calls=1,
                    total_turns=3, avg_turns=3.0, elapsed_ms=100.0,
                    total_input_tokens=0, total_output_tokens=0, total_api_calls=0,
                ),
                per_retailer=[],
            )

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   side_effect=_capture_client), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        # API key 있는 경우 → client가 non-None으로 전달되어야 함
        assert len(captured_client) == 1
        assert captured_client[0] is not None, \
            "run_batch_retailer_experiment에 client=None이 전달됨 — Mock 경로가 사용된 것"

    async def test_confirmed_retailers_zero_while_items_exist_is_unexpected(self, e2e_dirs):
        """confirmed_retailers=0이지만 items가 있는 경우 = retailer 미확정 상태.

        실제 실행에서 이 상태가 발생하면 pending을 통한 사용자 확인이 필요하다.
        이 테스트는 해당 상태를 감지하는 로직이 올바른지 검증한다.
        """
        mappings, form_defs, output = e2e_dirs
        # all retailers go to pending (no confirmed_code from batch)
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        batch_no_confirm = BatchExperimentResult(
            scenario="success", batch_size=1,
            stats=BatchStats(
                batch_size=1, success_count=1, failure_count=0,
                max_turns_hit_count=0, not_found_count=0,
                total_tool_calls=1, total_lookup_calls=1, total_confirm_calls=0,
                total_turns=2, avg_turns=2.0, elapsed_ms=50.0,
                total_input_tokens=100, total_output_tokens=40, total_api_calls=1,
            ),
            per_retailer=[
                RetailerBatchResult(
                    ocr_name="テスト店A", success=True,
                    confirmed_code=None,  # ← no decision (not_found)
                    lookup_basis="not_found",
                    tool_call_count=1, lookup_call_count=1, confirm_call_count=0,
                    turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
                )
            ],
        )

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   new=AsyncMock(return_value=batch_no_confirm)), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc_e2e", _PHASE2, output,
                form_id="form_01", hatsu_month="2025-01",
                enable_tool_use=True,
                settings=_mock_settings(mappings, form_defs),
            )

        # not_found → pending, not fallback
        assert stats.fallback_triggered is False
        assert result["confirmed_retailers"] == {}
        retailer_pending = [p for p in pending if p.get("mapping_type") == "retailer"]
        assert len(retailer_pending) == 1
        assert retailer_pending[0]["ocrName"] == "テスト店A"
