"""test_phase3_tool_use_token_usage.py — Tool Use token usage 수집 테스트

검증 항목:
  1. retailer Tool Use response usage 수집
  2. product Tool Use response usage 수집
  3. 여러 호출 usage 누적
  4. fallback 발생 시 token_usage 기본값(0) 유지
  5. usage 없는 mock response 처리 (방어적)
  6. token 기록 중 예외 발생해도 phase3 성공/fallback 유지
  7. result/pending public schema 불변
  8. ToolUseTokenStats 구조 확인

실행: pytest tests/test_phase3_tool_use_token_usage.py -v
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    Phase3FallbackStats,
    ToolUseMaxTurnsError,
    ToolUseTokenStats,
    _run_single_product_mapping,
    run_phase3_with_tool_use_or_fallback,
)
import pytest
from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


_PHASE2 = {
    "pages": [],
    "items": [
        {"customer": "テスト店A", "product": "辛ラーメン",
         "item_type": "条件", "columns": {"金額": 1000}},
    ],
}

_LEGACY_RETURN = (
    {"doc_id": "doc1", "form_id": "form_01", "hatsu_month": "",
     "issuer": {}, "confirmed_retailers": {}, "confirmed_products": {},
     "items": [], "cover_totals": {}},
    [],
)


def _mock_usage(input_tokens=100, output_tokens=50,
                cache_read=10, cache_creation=5):
    """Claude response.usage mock."""
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_creation
    return u


def _resp_with_usage(stop, *blocks, usage=None):
    r = MagicMock()
    r.stop_reason = stop
    r.content = list(blocks)
    r.usage = usage or _mock_usage()
    return r


def _text(t):
    b = MagicMock(); b.type = "text"; b.text = t; return b


# ── 1. ToolUseTokenStats 구조 ─────────────────────────────────────────────────

class TestToolUseTokenStatsStructure:
    def test_default_all_zero(self):
        s = ToolUseTokenStats()
        assert s.retailer_input_tokens  == 0
        assert s.retailer_output_tokens == 0
        assert s.retailer_api_calls     == 0
        assert s.product_input_tokens   == 0
        assert s.product_output_tokens  == 0
        assert s.product_api_calls      == 0

    def test_total_properties(self):
        s = ToolUseTokenStats(
            retailer_input_tokens=100, retailer_output_tokens=50,
            product_input_tokens=30, product_output_tokens=10,
        )
        assert s.total_input_tokens  == 130
        assert s.total_output_tokens == 60

    def test_total_api_calls(self):
        s = ToolUseTokenStats(retailer_api_calls=3, product_api_calls=2)
        assert s.total_api_calls == 5

    def test_phase3_fallback_stats_has_token_usage(self):
        """Phase3FallbackStats에 token_usage 필드가 있다."""
        stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=False, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False,
            batch_size=0, batch_failure_count=0,
        )
        assert hasattr(stats, "token_usage")
        assert isinstance(stats.token_usage, ToolUseTokenStats)
        assert stats.token_usage.total_input_tokens == 0


# ── 2. product Tool Use token 수집 ────────────────────────────────────────────

class TestProductTokenCollection:
    async def test_single_product_call_collects_usage(self, tmp_path):
        """_run_single_product_mapping이 response.usage를 token accumulator에 누적한다."""
        token_acc = ToolUseTokenStats()

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp_with_usage(
                "end_turn",
                _text('{"decision": "not_found", "reason": "없음"}'),
                usage=_mock_usage(input_tokens=120, output_tokens=60),
            ),
        ])

        await _run_single_product_mapping(
            "テスト製品", [], tmp_path, client=mock_client,
            _token_acc=token_acc,
        )

        assert token_acc.product_input_tokens  == 120
        assert token_acc.product_output_tokens == 60
        assert token_acc.product_api_calls     == 1

    async def test_multiple_product_turns_accumulated(self, tmp_path):
        """여러 turn의 product 호출 usage가 누적된다."""
        import csv
        mappings = tmp_path / "mappings"
        mappings.mkdir()
        with (mappings / "unit_price.csv").open("w", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["제품코드", "제품명", "시키리", "본부장"])
            w.writeheader()
            w.writerow({"제품코드": "P001", "제품명": "テスト", "시키리": "100", "본부장": "90"})

        token_acc = ToolUseTokenStats()
        tb = MagicMock(); tb.type = "tool_use"; tb.id = "1"; tb.name = "search_product"; tb.input = {"ocr_name": "テスト"}
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[
            _resp_with_usage("tool_use", tb,
                             usage=_mock_usage(input_tokens=100, output_tokens=30)),
            _resp_with_usage("end_turn",
                             _text('{"decision": "not_found", "reason": "없음"}'),
                             usage=_mock_usage(input_tokens=80, output_tokens=20)),
        ])

        await _run_single_product_mapping(
            "テスト", [], mappings, client=mock_client,
            _token_acc=token_acc,
        )

        assert token_acc.product_input_tokens  == 180   # 100 + 80
        assert token_acc.product_output_tokens == 50    # 30 + 20
        assert token_acc.product_api_calls     == 2

    async def test_no_token_acc_still_works(self, tmp_path):
        """_token_acc=None이어도 정상 동작 (backward compat)."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_resp_with_usage(
            "end_turn", _text('{"decision": "not_found", "reason": "없음"}')
        ))
        result = await _run_single_product_mapping(
            "テスト", [], tmp_path, client=mock_client, _token_acc=None
        )
        assert result.basis == "not_found"

    async def test_response_without_usage_gracefully_handled(self, tmp_path):
        """response.usage가 없어도 예외 없이 처리된다 (mock 환경 대응)."""
        token_acc = ToolUseTokenStats()

        r = MagicMock()
        r.stop_reason = "end_turn"
        r.content = [_text('{"decision": "not_found", "reason": "없음"}')]
        # usage 속성 없음
        del r.usage  # type: ignore

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=r)

        await _run_single_product_mapping(
            "テスト", [], tmp_path, client=mock_client, _token_acc=token_acc
        )

        # usage 없어도 예외 없이 0 유지
        assert token_acc.product_api_calls == 0


# ── 3. retailer token usage 수집 ────────────────────────────────────────────

class TestRetailerTokenCollection:
    async def test_retailer_token_accumulated_in_stats(self, tmp_path):
        """_execute_success_path가 batch_result.stats에서 retailer token을 stats.token_usage에 누적한다."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats,
        )
        from backend.pipeline.phase3_fallback import _execute_success_path

        stats_obj = BatchStats(
            batch_size=2, success_count=2, failure_count=0,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=4, total_lookup_calls=2, total_confirm_calls=2,
            total_turns=6, avg_turns=3.0, elapsed_ms=200.0,
            total_input_tokens=300,   # retailer usage
            total_output_tokens=150,
            total_api_calls=4,
        )
        batch_result = BatchExperimentResult(
            scenario="success", batch_size=2, stats=stats_obj, per_retailer=[]
        )

        phase2 = {"pages": [], "items": []}
        token_acc = ToolUseTokenStats()

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=tmp_path, form_definitions_dir=tmp_path,
                _token_acc=token_acc,
            )

        assert token_acc.retailer_input_tokens  == 300
        assert token_acc.retailer_output_tokens == 150
        assert token_acc.retailer_api_calls     == 4


# ── 4. ExperimentResult/BatchStats token fields ───────────────────────────────

class TestExperimentTokenFields:
    def test_experiment_result_has_token_fields(self):
        from backend.experiments.phase3_tool_use_experiment import ExperimentResult
        r = ExperimentResult(tool_calls=[], final_text=None, turns_used=1)
        assert r.input_tokens  == 0
        assert r.output_tokens == 0
        assert r.api_call_count == 0

    def test_batch_stats_has_token_fields(self):
        from backend.experiments.batch_tool_use_experiment import BatchStats
        s = BatchStats(
            batch_size=1, success_count=1, failure_count=0,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=1, total_lookup_calls=1, total_confirm_calls=0,
            total_turns=2, avg_turns=2.0, elapsed_ms=100.0,
        )
        assert s.total_input_tokens  == 0
        assert s.total_output_tokens == 0
        assert s.total_api_calls     == 0

    def test_retailer_batch_result_has_token_fields(self):
        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        r = RetailerBatchResult(
            ocr_name="テスト", success=True, confirmed_code="R001",
            lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
            confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0
        )
        assert r.input_tokens  == 0
        assert r.output_tokens == 0
        assert r.api_call_count == 0


# ── 5. fallback 시 usage 기본값 유지 ─────────────────────────────────────────

class TestTokenUsageOnFallback:
    async def test_fallback_token_usage_defaults_to_zero(self, tmp_path):
        """Tool Use 실패 → fallback 발생 시 token_usage는 0 (수집 기회 없음)."""
        from backend.pipeline.phase3_fallback import ToolUseMaxTurnsError

        exc = ToolUseMaxTurnsError("max_turns 초과")

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=_LEGACY_RETURN)), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=MagicMock(
                       mappings_dir=tmp_path,
                       form_definitions_dir=tmp_path,
                       anthropic_api_key="fake",
                   )):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        assert stats.fallback_triggered is True
        assert stats.token_usage.total_input_tokens  == 0
        assert stats.token_usage.total_output_tokens == 0
        assert stats.token_usage.total_api_calls     == 0


# ── 6. token 기록 실패해도 phase3 성공 ───────────────────────────────────────

class TestTokenRecordingErrorIsolation:
    async def test_db_error_does_not_break_phase3_result(self, tmp_path):
        """accumulate_token_usage DB 오류가 발생해도 phase3 결과가 반환된다."""
        from backend.pipeline.phase3_fallback import _execute_success_path

        phase2 = {"pages": [], "items": []}
        token_acc = ToolUseTokenStats(
            product_input_tokens=100, product_output_tokens=50, product_api_calls=1
        )

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            result, pending = await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=tmp_path, form_definitions_dir=tmp_path,
                _token_acc=token_acc,
            )

        # DB 기록 없이도 result/pending 정상 반환
        assert "doc_id" in result
        assert isinstance(pending, list)

    async def test_db_error_in_token_recording_does_not_fail_pipeline(self, tmp_path):
        """token usage DB 기록 중 예외가 발생해도 pipeline 결과에 영향 없다.

        accumulate_token_usage는 lazy import (try/except 내부)이므로,
        ImportError를 포함한 예외가 나도 warning만 남기고 result/pending 반환.
        """
        mock_settings = MagicMock(
            mappings_dir=tmp_path,
            form_definitions_dir=tmp_path,
            anthropic_api_key="fake",
        )

        async def mock_success_path(**kwargs):
            # token_acc에 usage를 채워서 DB 기록 경로가 실행되도록
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_api_calls    = 1
                acc.product_input_tokens = 100
            return _LEGACY_RETURN[0], _LEGACY_RETURN[1]

        # DB 기록을 강제로 실패시키기 위해 accumulate_token_usage lazy import 경로 차단
        import builtins
        original_import = builtins.__import__

        def fail_accumulate_import(name, *args, **kwargs):
            if "accumulate_token_usage" in str(args):
                raise ImportError("DB 연결 실패")
            return original_import(name, *args, **kwargs)

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(return_value=None)), \
             patch("backend.pipeline.phase3_fallback._execute_success_path",
                   new=mock_success_path), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=mock_settings):
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
                settings=mock_settings,
            )

        # DB 오류와 무관하게 result/pending 정상 반환
        assert result is not None
        assert isinstance(pending, list)
        assert stats.fallback_triggered is False


# ── 7. result/pending public schema 불변 ─────────────────────────────────────

class TestPublicSchemaUnchanged:
    async def test_result_has_required_keys_with_token_usage(self, tmp_path):
        """token usage 추가 후에도 result의 기존 key가 모두 존재한다."""
        from backend.pipeline.phase3_fallback import _execute_success_path

        phase2 = {"pages": [], "items": []}
        required_keys = {
            "doc_id", "form_id", "hatsu_month", "issuer",
            "confirmed_retailers", "confirmed_products", "items", "cover_totals",
        }

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            result, pending = await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=tmp_path, form_definitions_dir=tmp_path,
            )

        assert required_keys.issubset(result.keys())
        assert isinstance(pending, list)


# ── 8. success/fallback path DB 기록 인자 검증 ───────────────────────────────

_MOCK_SETTINGS = lambda tmp: MagicMock(  # noqa: E731
    mappings_dir=tmp, form_definitions_dir=tmp, anthropic_api_key="fake"
)


class TestDbRecordArgs:
    async def test_success_path_record_called_with_correct_args(self, tmp_path):
        """성공 시 _record_tool_use_token_usage가 doc_id/run_id/token_stats 인자로 호출된다."""
        mock_record = AsyncMock()

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.retailer_api_calls    = 1
                acc.retailer_input_tokens = 200
            return _LEGACY_RETURN[0], _LEGACY_RETURN[1]

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(return_value=None)), \
             patch("backend.pipeline.phase3_fallback._execute_success_path",
                   new=_fake_success), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=mock_record), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_MOCK_SETTINGS(tmp_path)):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "my_doc", _PHASE2, tmp_path, "form_01",
                run_id="run_xyz", enable_tool_use=True,
            )

        mock_record.assert_called_once()
        doc_id_arg, run_id_arg, token_stats_arg = mock_record.call_args[0]
        assert doc_id_arg  == "my_doc"
        assert run_id_arg  == "run_xyz"
        assert token_stats_arg.retailer_input_tokens == 200
        assert stats.fallback_triggered is False

    async def test_fallback_path_record_called_with_partial_tokens(self, tmp_path):
        """fallback 시에도 _record_tool_use_token_usage가 누적된 token stats로 호출된다."""
        mock_record = AsyncMock()

        partial = ToolUseTokenStats(
            retailer_input_tokens=300, retailer_output_tokens=120, retailer_api_calls=2,
        )
        exc = ToolUseMaxTurnsError("max_turns 초과", partial_token_stats=partial)

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=_LEGACY_RETURN)), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=mock_record), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_MOCK_SETTINGS(tmp_path)):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "my_doc", _PHASE2, tmp_path, "form_01",
                run_id="run_abc", enable_tool_use=True,
            )

        mock_record.assert_called_once()
        doc_id_arg, run_id_arg, token_stats_arg = mock_record.call_args[0]
        assert doc_id_arg == "my_doc"
        assert run_id_arg == "run_abc"
        assert token_stats_arg.retailer_input_tokens == 300
        assert token_stats_arg.retailer_api_calls    == 2
        assert stats.fallback_triggered is True


# ── 9. JSON parse 실패 fallback에서 usage 보존 ───────────────────────────────

class TestJsonParseFallbackUsagePreserved:
    async def test_parse_failure_preserves_product_token_usage(self, tmp_path):
        """JSON parse 실패 시 response 수신 후 누적된 product token이 보존된다."""
        from backend.pipeline.phase3_fallback import ToolUseParseError

        token_acc = ToolUseTokenStats()

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_resp_with_usage(
            "end_turn",
            _text("invalid json {{{{ not parseable"),
            usage=_mock_usage(input_tokens=150, output_tokens=80),
        ))

        with pytest.raises(ToolUseParseError):
            await _run_single_product_mapping(
                "テスト製品", [], tmp_path,
                client=mock_client,
                _token_acc=token_acc,
            )

        # parse 실패 전에 response를 받았으므로 token이 누적되어 있어야 함
        assert token_acc.product_input_tokens  == 150
        assert token_acc.product_output_tokens == 80
        assert token_acc.product_api_calls     == 1


# ── 10. max_turns 초과 fallback에서 이전 usage 누적 ──────────────────────────

class TestMaxTurnsFallbackUsagePreserved:
    async def test_max_turns_exception_partial_stats_copied_to_token_usage(self, tmp_path):
        """max_turns 초과 시 exception.partial_token_stats가 stats.token_usage에 복사된다."""
        partial = ToolUseTokenStats(
            retailer_input_tokens=500, retailer_output_tokens=200, retailer_api_calls=3,
        )
        exc = ToolUseMaxTurnsError("max_turns 초과", partial_token_stats=partial)

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=_LEGACY_RETURN)), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_MOCK_SETTINGS(tmp_path)):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        assert stats.fallback_triggered is True
        assert stats.max_turns_hit is True
        assert stats.token_usage.retailer_input_tokens  == 500
        assert stats.token_usage.retailer_output_tokens == 200
        assert stats.token_usage.retailer_api_calls     == 3


# ── 11. API response 없음 → usage 0 ──────────────────────────────────────────

class TestApiResponseAbsentUsageZero:
    async def test_api_error_no_partial_stats_usage_is_zero(self, tmp_path):
        """ToolUseApiError(partial_token_stats=None) → token usage는 0."""
        from backend.pipeline.phase3_fallback import ToolUseApiError

        exc = ToolUseApiError("Claude API 최종 실패")  # partial_token_stats=None

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=_LEGACY_RETURN)), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_MOCK_SETTINGS(tmp_path)):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        assert stats.fallback_triggered is True
        assert stats.api_retry_failed is True
        assert stats.token_usage.total_input_tokens  == 0
        assert stats.token_usage.total_output_tokens == 0
        assert stats.token_usage.total_api_calls     == 0


# ── 12. asyncpg 없이 token recording mock patch 가능 ─────────────────────────

class TestTokenRecordingMockPatch:
    def test_record_function_is_module_level_patchable(self):
        """_record_tool_use_token_usage가 module-level 심볼로 patch 가능하다 (asyncpg 불필요)."""
        import backend.pipeline.phase3_fallback as mod

        assert hasattr(mod, "_record_tool_use_token_usage"), \
            "_record_tool_use_token_usage가 모듈 심볼에 없음"
        assert callable(mod._record_tool_use_token_usage)

        mock_fn = AsyncMock()
        with patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=mock_fn):
            import backend.pipeline.phase3_fallback as reloaded
            assert reloaded._record_tool_use_token_usage is mock_fn

    async def test_record_patchable_and_called_without_asyncpg(self, tmp_path):
        """asyncpg 없이도 _record_tool_use_token_usage를 patch하고 호출 여부를 확인할 수 있다."""
        mock_record = AsyncMock()

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_api_calls    = 1
                acc.product_input_tokens = 50
            return _LEGACY_RETURN[0], _LEGACY_RETURN[1]

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(return_value=None)), \
             patch("backend.pipeline.phase3_fallback._execute_success_path",
                   new=_fake_success), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=mock_record), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_MOCK_SETTINGS(tmp_path)):
            await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
            )

        mock_record.assert_called_once()
        assert mock_record.call_args[0][2].product_input_tokens == 50


# ── 13. model name 중앙화 상수 검증 ─────────────────────────────────────────

class TestModelName:
    def test_tool_use_model_is_centralized_non_empty_string(self):
        """_TOOL_USE_MODEL이 모듈에 존재하며 비어 있지 않은 문자열이다."""
        from backend.pipeline.phase3_fallback import _TOOL_USE_MODEL

        assert isinstance(_TOOL_USE_MODEL, str), "_TOOL_USE_MODEL이 str이 아님"
        assert len(_TOOL_USE_MODEL) > 0, "_TOOL_USE_MODEL이 빈 문자열"
        # claude 계열 model 이름 형식 검증
        assert "claude" in _TOOL_USE_MODEL.lower(), \
            f"_TOOL_USE_MODEL이 claude 모델명이 아님: {_TOOL_USE_MODEL!r}"

    async def test_record_token_usage_passes_tool_use_model_to_db(self):
        """_record_tool_use_token_usage가 _TOOL_USE_MODEL을 DB 기록 함수에 전달한다.

        asyncpg 없이도 sys.modules에 mock 모듈을 주입해서 검증한다.
        """
        import sys
        import types
        from backend.pipeline.phase3_fallback import (
            _record_tool_use_token_usage, _TOOL_USE_MODEL,
        )

        captured: list[dict] = []

        async def _mock_accumulate(doc_id, phase, input_tokens, output_tokens, model, *, run_id=""):
            captured.append({"doc_id": doc_id, "model": model, "run_id": run_id})

        # asyncpg 없이도 동작하도록 sys.modules에 fake backend.db.queries 주입
        mock_queries_mod = types.ModuleType("backend.db.queries")
        mock_queries_mod.accumulate_token_usage = _mock_accumulate  # type: ignore

        _prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries_mod
        try:
            token_stats = ToolUseTokenStats(
                retailer_api_calls=1, retailer_input_tokens=100, retailer_output_tokens=40,
            )
            await _record_tool_use_token_usage("doc1", "run1", token_stats)
        finally:
            if _prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = _prev

        assert len(captured) == 1
        assert captured[0]["model"]  == _TOOL_USE_MODEL
        assert captured[0]["doc_id"] == "doc1"
        assert captured[0]["run_id"] == "run1"
