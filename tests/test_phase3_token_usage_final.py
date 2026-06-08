"""test_phase3_token_usage_final.py — Token Usage 최종 검증 테스트

검증 항목:
  1. success path: retailer usage가 DB 기록 인자에 포함됨
  2. success path: product usage가 DB 기록 인자에 포함됨
  3. success path: retailer + product 합산 input/output이 DB에 기록됨
  4. fallback: JSON parse 실패 시 product usage가 DB에 기록됨
  5. fallback: max_turns 초과 시 partial retailer usage가 DB에 기록됨
  6. fallback: contract violation 시 response usage가 보존됨
  7. settings model: PHASE3_TOOL_USE_MODEL env 값이 DB 기록에 사용됨
  8. settings model: DB 기록 model이 settings.phase3_tool_use_model과 일치함

실행: pytest tests/test_phase3_token_usage_final.py -v
"""
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    ToolUseContractError,
    ToolUseMaxTurnsError,
    ToolUseParseError,
    ToolUseTokenStats,
    _TOOL_USE_MODEL,
    _record_tool_use_token_usage,
    _run_single_product_mapping,
    run_phase3_with_tool_use_or_fallback,
)
from backend.tools.metrics import reset_metrics


# ── Fixtures / 공통 헬퍼 ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


_LEGACY_RESULT = (
    {"doc_id": "doc1", "form_id": "form_01", "hatsu_month": "",
     "issuer": {}, "confirmed_retailers": {}, "confirmed_products": {},
     "items": [], "cover_totals": {}},
    [],
)

_PHASE2 = {
    "pages": [],
    "items": [
        {"customer": "テスト店A", "product": "辛ラーメン",
         "item_type": "条件", "columns": {"金額": 1000}},
    ],
}


def _mock_settings(tmp: Path, *, model: str = _TOOL_USE_MODEL) -> MagicMock:
    return MagicMock(
        mappings_dir=tmp,
        form_definitions_dir=tmp,
        anthropic_api_key="fake",
        phase3_tool_use_model=model,
    )


def _mock_usage(input_tokens=100, output_tokens=50,
                cache_read=0, cache_creation=0) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_creation
    return u


def _resp(stop, *blocks, usage=None):
    r = MagicMock()
    r.stop_reason = stop
    r.content = list(blocks)
    r.usage = usage or _mock_usage()
    return r


def _text(t):
    b = MagicMock(); b.type = "text"; b.text = t; return b


def _inject_mock_db_queries(mock_accumulate):
    """sys.modules에 fake backend.db.queries를 주입한다 (asyncpg 불필요)."""
    mod = types.ModuleType("backend.db.queries")
    mod.accumulate_token_usage = mock_accumulate  # type: ignore
    return mod


# ── 1. success: retailer usage가 DB 기록 인자에 포함됨 ────────────────────────

class TestSuccessRetailerUsageInDb:
    async def test_retailer_input_tokens_recorded(self, tmp_path):
        """성공 시 retailer input_tokens가 DB 기록 token_stats에 포함된다."""
        import sys
        captured: list[dict] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured.append({"inp": inp, "out": out, "model": model})

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.retailer_input_tokens  = 400
                acc.retailer_output_tokens = 180
                acc.retailer_api_calls     = 3
            return _LEGACY_RESULT[0], _LEGACY_RESULT[1]

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(return_value=None)), \
                 patch("backend.pipeline.phase3_fallback._execute_success_path",
                       new=_fake_success), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path)):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        assert len(captured) == 1
        assert captured[0]["inp"] == 400
        assert captured[0]["out"] == 180


# ── 2. success: product usage가 DB 기록 인자에 포함됨 ────────────────────────

class TestSuccessProductUsageInDb:
    async def test_product_input_tokens_recorded(self, tmp_path):
        """성공 시 product input_tokens가 DB 기록 token_stats에 포함된다."""
        import sys
        captured: list[dict] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured.append({"inp": inp, "out": out})

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_input_tokens  = 250
                acc.product_output_tokens = 90
                acc.product_api_calls     = 2
            return _LEGACY_RESULT[0], _LEGACY_RESULT[1]

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(return_value=None)), \
                 patch("backend.pipeline.phase3_fallback._execute_success_path",
                       new=_fake_success), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path)):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        assert len(captured) == 1
        assert captured[0]["inp"] == 250
        assert captured[0]["out"] == 90


# ── 3. success: retailer + product 합산이 DB에 기록됨 ─────────────────────────

class TestSuccessCombinedUsageInDb:
    async def test_combined_retailer_and_product_tokens_recorded(self, tmp_path):
        """성공 시 retailer + product 합산 token이 DB에 기록된다."""
        import sys
        captured: list[dict] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured.append({"inp": inp, "out": out, "model": model})

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                # retailer: 300 in / 120 out
                acc.retailer_input_tokens  = 300
                acc.retailer_output_tokens = 120
                acc.retailer_api_calls     = 2
                # product: 200 in / 80 out
                acc.product_input_tokens   = 200
                acc.product_output_tokens  = 80
                acc.product_api_calls      = 1
            return _LEGACY_RESULT[0], _LEGACY_RESULT[1]

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(return_value=None)), \
                 patch("backend.pipeline.phase3_fallback._execute_success_path",
                       new=_fake_success), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path)):
                _, _, stats = await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        # DB 기록값 검증
        assert len(captured) == 1
        assert captured[0]["inp"] == 500   # 300 + 200
        assert captured[0]["out"] == 200   # 120 + 80

        # stats에도 동일하게 반영
        assert stats.token_usage.total_input_tokens  == 500
        assert stats.token_usage.total_output_tokens == 200
        assert stats.token_usage.total_api_calls     == 3


# ── 4. fallback: JSON parse 실패 시 product usage가 DB에 기록됨 ───────────────

class TestFallbackJsonParseProductUsageInDb:
    async def test_product_usage_recorded_after_parse_failure_fallback(self, tmp_path):
        """JSON parse 실패 → fallback 시 이미 누적된 product usage가 DB에 기록된다."""
        import sys
        captured: list[dict] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured.append({"inp": inp, "out": out})

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        # _execute_success_path가 product token을 누적한 뒤 parse error를 일으키는 상황 시뮬레이션
        # (실제로는 product token이 _token_acc에 직접 누적된 뒤 exception 발생)
        from backend.pipeline.phase3_fallback import ToolUseParseError

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_input_tokens  = 150
                acc.product_output_tokens = 60
                acc.product_api_calls     = 1
            raise ToolUseParseError("product JSON parse 실패")

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(return_value=None)), \
                 patch("backend.pipeline.phase3_fallback._execute_success_path",
                       new=_fake_success), \
                 patch("backend.pipeline.phase3_fallback.run_phase3",
                       new=AsyncMock(return_value=_LEGACY_RESULT)), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path)):
                _, _, stats = await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        # fallback 발생했지만 DB에 이미 누적된 product token이 기록됨
        assert stats.fallback_triggered is True
        assert len(captured) == 1
        assert captured[0]["inp"] == 150
        assert captured[0]["out"] == 60


# ── 5. fallback: max_turns 초과 시 partial retailer usage가 DB에 기록됨 ───────

class TestFallbackMaxTurnsRetailerUsageInDb:
    async def test_partial_retailer_usage_recorded_on_max_turns_fallback(self, tmp_path):
        """max_turns 초과 → fallback 시 exception.partial_token_stats가 DB에 기록된다."""
        import sys
        captured: list[dict] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured.append({"inp": inp, "out": out, "model": model})

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        partial = ToolUseTokenStats(
            retailer_input_tokens=350, retailer_output_tokens=140, retailer_api_calls=3,
        )
        exc = ToolUseMaxTurnsError("max_turns 초과", partial_token_stats=partial)

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(side_effect=exc)), \
                 patch("backend.pipeline.phase3_fallback.run_phase3",
                       new=AsyncMock(return_value=_LEGACY_RESULT)), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path)):
                _, _, stats = await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        assert stats.fallback_triggered is True
        assert stats.max_turns_hit is True
        assert len(captured) == 1
        assert captured[0]["inp"] == 350
        assert captured[0]["out"] == 140
        # stats에도 반영
        assert stats.token_usage.retailer_input_tokens  == 350
        assert stats.token_usage.retailer_output_tokens == 140


# ── 6. fallback: contract violation 시 response usage가 보존됨 ────────────────

class TestFallbackContractViolationUsagePreserved:
    async def test_contract_violation_preserves_partial_token_stats(self, tmp_path):
        """contract violation → exception.partial_token_stats가 stats.token_usage에 복사된다."""
        partial = ToolUseTokenStats(
            retailer_input_tokens=200, retailer_output_tokens=80, retailer_api_calls=2,
        )
        exc = ToolUseContractError(
            "'テスト店': success=False",
            partial_token_stats=partial,
        )

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=AsyncMock(side_effect=exc)), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=_LEGACY_RESULT)), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   return_value=_mock_settings(tmp_path)):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
            )

        assert stats.fallback_triggered is True
        assert stats.token_usage.retailer_input_tokens  == 200
        assert stats.token_usage.retailer_output_tokens == 80
        assert stats.token_usage.retailer_api_calls     == 2


# ── 7. settings model: PHASE3_TOOL_USE_MODEL env 값이 DB 기록에 사용됨 ─────────

class TestSettingsModelUsedInDb:
    async def test_custom_model_passed_to_db_accumulate(self, tmp_path):
        """settings.phase3_tool_use_model 값이 DB 기록 model 인자로 사용된다."""
        import sys
        captured_model: list[str] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured_model.append(model)

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        custom_model = "claude-sonnet-4-6"

        async def _fake_success(**kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.retailer_api_calls    = 1
                acc.retailer_input_tokens = 100
            return _LEGACY_RESULT[0], _LEGACY_RESULT[1]

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(return_value=None)), \
                 patch("backend.pipeline.phase3_fallback._execute_success_path",
                       new=_fake_success), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path, model=custom_model)):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        assert len(captured_model) == 1
        assert captured_model[0] == custom_model

    def test_settings_has_phase3_tool_use_model_field(self):
        """Settings에 phase3_tool_use_model 필드가 존재하고 기본값이 올바르다."""
        from backend.core.config import Settings
        # 기본값 검증 (env 파일 읽기 없이 직접 검사)
        field_info = Settings.model_fields.get("phase3_tool_use_model")
        assert field_info is not None, "phase3_tool_use_model 필드가 없음"
        default = field_info.default
        assert isinstance(default, str) and len(default) > 0
        assert "haiku" in default or "sonnet" in default or "claude" in default.lower()

    def test_phase3_tool_use_model_default_matches_tool_use_model_constant(self):
        """Settings의 기본 model이 _TOOL_USE_MODEL 상수와 동일하다."""
        from backend.core.config import Settings
        default = Settings.model_fields["phase3_tool_use_model"].default
        assert default == _TOOL_USE_MODEL, (
            f"Settings default '{default}' != _TOOL_USE_MODEL '{_TOOL_USE_MODEL}'"
        )


# ── 8. DB 기록 model 인자가 settings 값과 일치함 ──────────────────────────────

class TestDbRecordModelMatchesSettings:
    async def test_fallback_db_record_uses_settings_model(self, tmp_path):
        """fallback 시에도 DB 기록 model이 settings.phase3_tool_use_model과 일치한다."""
        import sys
        captured_model: list[str] = []

        async def mock_accumulate(doc_id, phase, inp, out, model, *, run_id=""):
            captured_model.append(model)

        mock_queries = _inject_mock_db_queries(mock_accumulate)

        custom_model = "claude-haiku-4-5-20251001-custom"
        partial = ToolUseTokenStats(retailer_input_tokens=100, retailer_api_calls=1)
        exc = ToolUseMaxTurnsError("max_turns", partial_token_stats=partial)

        prev = sys.modules.get("backend.db.queries")
        sys.modules["backend.db.queries"] = mock_queries
        try:
            with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                       new=AsyncMock(side_effect=exc)), \
                 patch("backend.pipeline.phase3_fallback.run_phase3",
                       new=AsyncMock(return_value=_LEGACY_RESULT)), \
                 patch("backend.pipeline.phase3_fallback.get_settings",
                       return_value=_mock_settings(tmp_path, model=custom_model)):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True,
                )
        finally:
            if prev is None:
                sys.modules.pop("backend.db.queries", None)
            else:
                sys.modules["backend.db.queries"] = prev

        assert len(captured_model) == 1
        assert captured_model[0] == custom_model

    async def test_product_call_uses_settings_model(self, tmp_path):
        """product Tool Use Claude 호출에 settings.phase3_tool_use_model이 사용된다."""
        custom_model = "claude-sonnet-override"
        called_models: list[str] = []

        class _FakeClient:
            class messages:
                @staticmethod
                async def create(**kwargs):
                    called_models.append(kwargs.get("model", ""))
                    r = MagicMock()
                    r.stop_reason = "end_turn"
                    r.content = [_text('{"decision": "not_found", "reason": "없음"}')]
                    r.usage = _mock_usage(input_tokens=50, output_tokens=20)
                    return r

        token_acc = ToolUseTokenStats()
        await _run_single_product_mapping(
            "テスト製品", [], tmp_path,
            client=_FakeClient(),
            model=custom_model,
            _token_acc=token_acc,
        )

        assert len(called_models) == 1
        assert called_models[0] == custom_model
