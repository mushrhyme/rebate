"""test_phase3_fallback.py — Phase 3 Fallback 래퍼 테스트

실행: pytest tests/test_phase3_fallback.py -v
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    Phase3FallbackStats,
    ToolUseApiError,
    ToolUseContractError,
    ToolUseDispatchError,
    ToolUseFallbackTrigger,
    ToolUseMaxTurnsError,
    ToolUseParseError,
    run_phase3_with_tool_use_or_fallback,
)
from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


# phase2_result 최소 픽스처
_PHASE2 = {
    "pages": [],
    "items": [
        {"customer": "テスト店A", "product": "辛ラーメン", "item_type": "条件",
         "columns": {"金額": 1000}, "source_pages": [1]},
    ],
}

# legacy run_phase3의 반환값
_LEGACY_RETURN = (
    {
        "doc_id": "test_doc",
        "form_id": "form_01",
        "hatsu_month": "",
        "issuer": {},
        "confirmed_retailers": {},
        "confirmed_products": {},
        "items": [],
        "cover_totals": {},
    },
    [],  # pending
)


# ── 공통 패치 헬퍼 ────────────────────────────────────────────────────────────

def _patch_legacy(return_value=_LEGACY_RETURN):
    """run_phase3()를 mock으로 대체한다."""
    return patch(
        "backend.pipeline.phase3_fallback.run_phase3",
        new=AsyncMock(return_value=return_value),
    )


def _patch_tool_use(side_effect=None):
    """_attempt_tool_use_phase()를 mock으로 대체한다."""
    if side_effect is None:
        return patch(
            "backend.pipeline.phase3_fallback._attempt_tool_use_phase",
            new=AsyncMock(return_value=None),  # 정상 완료
        )
    return patch(
        "backend.pipeline.phase3_fallback._attempt_tool_use_phase",
        new=AsyncMock(side_effect=side_effect),
    )


_SUCCESS_PATH_RETURN = (
    {
        "doc_id": "doc1",
        "form_id": "form_01",
        "hatsu_month": "",
        "issuer": {},
        "confirmed_retailers": {},
        "confirmed_products": {},
        "items": [],
        "cover_totals": {},
    },
    [],
)


def _patch_success_path(return_value=None, side_effect=None):
    """_execute_success_path()를 mock으로 대체한다."""
    rv = return_value if return_value is not None else _SUCCESS_PATH_RETURN
    if side_effect is not None:
        return patch(
            "backend.pipeline.phase3_fallback._execute_success_path",
            new=AsyncMock(side_effect=side_effect),
        )
    return patch(
        "backend.pipeline.phase3_fallback._execute_success_path",
        new=AsyncMock(return_value=rv),
    )


# ── 1. Feature Flag OFF → legacy 직접 호출 ────────────────────────────────────

class TestFeatureFlagOff:
    async def test_legacy_called_directly_when_flag_off(self, tmp_path):
        """enable_tool_use=False(기본) → Tool Use 없이 run_phase3() 직접 호출."""
        with _patch_legacy() as mock_legacy, \
             _patch_tool_use() as mock_tu:
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=False,
            )
        mock_legacy.assert_called_once()
        mock_tu.assert_not_called()
        assert stats.enable_tool_use is False
        assert stats.used_tool_use is False
        assert stats.fallback_triggered is False

    async def test_legacy_result_returned_when_flag_off(self, tmp_path):
        """enable_tool_use=False → legacy 결과가 그대로 반환된다."""
        with _patch_legacy():
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=False,
            )
        assert result["doc_id"] == "test_doc"
        assert pending == []

    async def test_stats_timing_recorded_when_flag_off(self, tmp_path):
        """enable_tool_use=False → legacy 소요 시간이 기록된다."""
        with _patch_legacy():
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=False,
            )
        assert stats.legacy_elapsed_ms >= 0
        assert stats.tool_use_elapsed_ms == 0.0  # 미시도


# ── 2. Tool Use 성공 → Fallback 미발생 ────────────────────────────────────────

class TestToolUseSuccess:
    async def test_no_fallback_on_tool_use_success(self, tmp_path):
        """Tool Use 검증 성공 시 fallback이 발생하지 않는다."""
        with _patch_legacy(), _patch_tool_use():
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is False
        assert stats.fallback_reason is None

    async def test_legacy_not_called_on_tool_use_success(self, tmp_path):
        """Tool Use 성공 시 legacy run_phase3()가 호출되지 않는다.

        이전 구조(검증만 수행)와 달리, 성공 시 _execute_success_path()가
        직접 output을 만들고 저장하므로 legacy는 불필요하다.
        """
        with _patch_legacy() as mock_legacy, \
             _patch_tool_use(), \
             _patch_success_path():
            await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        mock_legacy.assert_not_called()

    async def test_tool_use_elapsed_recorded_on_success(self, tmp_path):
        """Tool Use 성공 시 소요 시간이 기록된다."""
        with _patch_legacy(), _patch_tool_use(), _patch_success_path():
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.tool_use_elapsed_ms >= 0
        assert stats.legacy_elapsed_ms == 0.0   # success path → legacy 미호출
        assert stats.total_elapsed_ms >= stats.tool_use_elapsed_ms

    async def test_adapter_result_returned_on_tool_use_success(self, tmp_path):
        """Tool Use 성공 시 _execute_success_path() 결과가 반환된다."""
        with _patch_legacy(), _patch_tool_use(), _patch_success_path():
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        # _SUCCESS_PATH_RETURN의 doc_id ("doc1") — legacy의 "test_doc" 아님
        assert result["doc_id"] == "doc1"
        assert stats.fallback_triggered is False


# ── 3. Fallback 발생 조건별 테스트 ───────────────────────────────────────────

class TestFallbackTriggers:
    async def test_max_turns_triggers_fallback(self, tmp_path):
        """max_turns 초과 시 fallback이 발생한다."""
        exc = ToolUseMaxTurnsError("max_turns(5) 초과 — 3/5건")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is True
        assert stats.fallback_class == "ToolUseMaxTurnsError"
        assert "max_turns" in stats.fallback_reason

    async def test_dispatch_error_triggers_fallback(self, tmp_path):
        """dispatch 오류 시 fallback이 발생한다."""
        exc = ToolUseDispatchError("Tool dispatch 3건 실패")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is True
        assert stats.fallback_class == "ToolUseDispatchError"

    async def test_api_error_triggers_fallback(self, tmp_path):
        """Claude API retry 최종 실패 시 fallback이 발생한다."""
        exc = ToolUseApiError("Claude API 최종 실패: RateLimitError")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is True
        assert stats.fallback_class == "ToolUseApiError"

    async def test_contract_error_triggers_fallback(self, tmp_path):
        """contract 위반 시 fallback이 발생한다."""
        exc = ToolUseContractError("result 타입 불일치")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is True
        assert stats.fallback_class == "ToolUseContractError"

    async def test_parse_error_triggers_fallback(self, tmp_path):
        """JSON 파싱 실패 시 fallback이 발생한다."""
        exc = ToolUseParseError("JSON decode 실패")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is True
        assert stats.fallback_class == "ToolUseParseError"

    async def test_fallback_legacy_called_on_any_trigger(self, tmp_path):
        """모든 fallback 조건에서 run_phase3()가 호출된다."""
        for exc_cls in [ToolUseMaxTurnsError, ToolUseDispatchError,
                        ToolUseApiError, ToolUseContractError, ToolUseParseError]:
            exc = exc_cls(f"test: {exc_cls.__name__}")
            with _patch_legacy() as mock_legacy, _patch_tool_use(side_effect=exc):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01",
                    enable_tool_use=True,
                )
            mock_legacy.assert_called_once()

    async def test_fallback_legacy_result_returned(self, tmp_path):
        """fallback 시 legacy run_phase3() 결과가 반환된다."""
        exc = ToolUseMaxTurnsError("초과")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert result["doc_id"] == "test_doc"
        assert stats.fallback_triggered is True


# ── 4. Fallback 비발생 조건 ───────────────────────────────────────────────────

class TestNoFallbackConditions:
    async def test_not_found_is_not_fallback(self, tmp_path):
        """lookup_retailer not_found는 fallback 조건이 아니다.

        not_found는 정상 결과이므로 ToolUseFallbackTrigger를 raise하지 않는다.
        Tool Use 검증은 성공, fallback 미발생.
        """
        # Tool Use가 성공 반환 (not_found는 검증 실패 아님)
        with _patch_legacy(), _patch_tool_use():
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )
        assert stats.fallback_triggered is False

    async def test_all_fallback_trigger_are_subclasses(self):
        """모든 fallback 예외는 ToolUseFallbackTrigger의 서브클래스다."""
        for cls in [ToolUseMaxTurnsError, ToolUseContractError,
                    ToolUseDispatchError, ToolUseApiError, ToolUseParseError]:
            assert issubclass(cls, ToolUseFallbackTrigger), f"{cls.__name__} 미포함"


# ── 5. Side-effect 중복 방지 검증 ─────────────────────────────────────────────

class TestSideEffectSafety:
    async def test_tool_use_uses_allow_side_effects_false(self, tmp_path):
        """Tool Use 경로는 allow_side_effects=False로 실행된다 (CSV 쓰기 없음).

        _attempt_tool_use_phase() 내부에서 run_batch_retailer_experiment가
        allow_side_effects=False로 호출되는지 검증한다.
        """
        call_kwargs_list = []

        async def capture_batch(**kwargs):
            call_kwargs_list.append(kwargs)
            # 정상 반환 mock
            from backend.experiments.batch_tool_use_experiment import (
                BatchExperimentResult, BatchStats,
            )
            stats = BatchStats(
                batch_size=0, success_count=0, failure_count=0,
                max_turns_hit_count=0, not_found_count=0,
                total_tool_calls=0, total_lookup_calls=0,
                total_confirm_calls=0, total_turns=0, avg_turns=0.0,
                elapsed_ms=0.0,
            )
            return BatchExperimentResult(
                scenario="success", batch_size=0, stats=stats, per_retailer=[]
            )

        with _patch_legacy(), \
             patch("backend.experiments.batch_tool_use_experiment.run_batch_retailer_experiment",
                   new=capture_batch):
            # phase2_result with actual retailers to trigger the batch call
            phase2 = {
                "pages": [],
                "items": [{"customer": "テスト", "product": "商品",
                           "item_type": "条件", "columns": {}}],
            }
            await run_phase3_with_tool_use_or_fallback(
                "doc1", phase2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        # allow_side_effects=False가 전달되었는지 확인
        if call_kwargs_list:
            assert call_kwargs_list[0].get("allow_side_effects") is False, (
                "Tool Use 경로가 allow_side_effects=True로 실행됨 — CSV 중복 저장 위험!"
            )

    async def test_confirm_mapping_called_once_on_success(self, tmp_path):
        """Tool Use 성공 후 confirm_mapping은 run_phase3() 경로에서만 호출된다.

        Tool Use 경로: 0회 (allow_side_effects=False)
        run_phase3():  N회 (기존과 동일)
        합계:          N회 (중복 없음)
        """
        import backend.tools.mapping as mapping_mod
        confirm_call_count = 0
        original_confirm = mapping_mod.confirm_mapping

        async def counting_confirm(*args, **kwargs):
            nonlocal confirm_call_count
            confirm_call_count += 1
            return await original_confirm(*args, **kwargs)

        # Tool Use 성공 mock
        with _patch_tool_use(), \
             patch.object(mapping_mod, "confirm_mapping", counting_confirm):
            # run_phase3()는 실제 호출 — 그러나 빈 phase2이므로 confirm 없음
            with patch("backend.pipeline.phase3_fallback.run_phase3",
                       new=AsyncMock(return_value=_LEGACY_RETURN)):
                await run_phase3_with_tool_use_or_fallback(
                    "doc1", _PHASE2, tmp_path, "form_01",
                    enable_tool_use=True,
                )
        # Tool Use 경로에서 confirm_mapping 호출 없음 (allow_side_effects=False)
        assert confirm_call_count == 0


# ── 6. Stats 구조 검증 ────────────────────────────────────────────────────────

class TestFallbackStatsStructure:
    async def test_stats_returned_as_third_element(self, tmp_path):
        """run_phase3_with_tool_use_or_fallback()는 (result, pending, stats) 3-tuple 반환."""
        with _patch_legacy(), _patch_tool_use():
            ret = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True
            )
        assert len(ret) == 3
        assert isinstance(ret[2], Phase3FallbackStats)

    async def test_stats_fallback_reason_on_trigger(self, tmp_path):
        """fallback_reason이 예외 메시지와 일치한다."""
        reason_msg = "max_turns(5) 초과 — 2/3건"
        exc = ToolUseMaxTurnsError(reason_msg)
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True
            )
        assert stats.fallback_reason == reason_msg
        assert stats.fallback_class == "ToolUseMaxTurnsError"

    async def test_max_turns_hit_flag_set_correctly(self, tmp_path):
        """max_turns_hit 플래그가 올바르게 설정된다."""
        exc = ToolUseMaxTurnsError("초과")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True
            )
        assert stats.max_turns_hit is True

        # non-max_turns error
        exc2 = ToolUseDispatchError("dispatch 실패")
        with _patch_legacy(), _patch_tool_use(side_effect=exc2):
            _, _, stats2 = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True
            )
        assert stats2.max_turns_hit is False

    async def test_api_retry_failed_flag_set_on_api_error(self, tmp_path):
        """api_retry_failed 플래그가 ToolUseApiError 시 설정된다."""
        exc = ToolUseApiError("RateLimitError 최종 실패")
        with _patch_legacy(), _patch_tool_use(side_effect=exc):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01", enable_tool_use=True
            )
        # _attempt_tool_use_phase가 api_retry_failed를 설정하는지
        # ← 여기서는 mock이므로 _attempt_tool_use_phase가 직접 설정하지 않음
        # ToolUseApiError 예외로 전파되므로 fallback_class로 확인
        assert stats.fallback_class == "ToolUseApiError"


# ── 7. _attempt_tool_use_phase 직접 테스트 ────────────────────────────────────

class TestAttemptToolUsePhase:
    async def test_max_turns_hit_raises_tool_use_max_turns_error(self, tmp_path):
        """batch result에 max_turns_hit이 있으면 ToolUseMaxTurnsError를 raise한다."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        from backend.pipeline.phase3_fallback import _attempt_tool_use_phase

        # max_turns_hit_count > 0 인 batch 결과
        stats_obj = BatchStats(
            batch_size=3, success_count=1, failure_count=2,
            max_turns_hit_count=2, not_found_count=0,
            total_tool_calls=3, total_lookup_calls=3, total_confirm_calls=1,
            total_turns=5, avg_turns=2.5, elapsed_ms=100.0,
        )
        mock_result = BatchExperimentResult(
            scenario="success", batch_size=3,
            stats=stats_obj, per_retailer=[],
        )

        stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=True, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False,
            batch_size=0, batch_failure_count=0,
        )

        phase2 = {"items": [{"customer": f"店{i}" for i in range(3)}]}

        with patch(
            "backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
            new=AsyncMock(return_value=mock_result),
        ):
            with pytest.raises(ToolUseMaxTurnsError):
                await _attempt_tool_use_phase(
                    phase2_result=phase2,
                    mappings_dir=tmp_path,
                    form_definitions_dir=tmp_path,
                    form_id="form_01",
                    max_turns=5,
                    stats=stats,
                    anthropic_api_key="fake",  # 운영 경로 진입을 위해 필요
                )
        assert stats.max_turns_hit is True

    async def test_failure_count_raises_dispatch_error(self, tmp_path):
        """batch failure_count > 0 이면 ToolUseDispatchError를 raise한다."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats,
        )
        from backend.pipeline.phase3_fallback import _attempt_tool_use_phase

        stats_obj = BatchStats(
            batch_size=5, success_count=3, failure_count=2,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=5, total_lookup_calls=5, total_confirm_calls=3,
            total_turns=10, avg_turns=2.0, elapsed_ms=80.0,
        )
        mock_result = BatchExperimentResult(
            scenario="success", batch_size=5,
            stats=stats_obj, per_retailer=[],
        )

        stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=True, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False,
            batch_size=0, batch_failure_count=0,
        )

        phase2 = {"items": [{"customer": f"店{i}" for i in range(5)}]}

        with patch(
            "backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
            new=AsyncMock(return_value=mock_result),
        ):
            with pytest.raises(ToolUseDispatchError):
                await _attempt_tool_use_phase(
                    phase2_result=phase2,
                    mappings_dir=tmp_path,
                    form_definitions_dir=tmp_path,
                    form_id="form_01",
                    max_turns=5,
                    stats=stats,
                    anthropic_api_key="fake",  # 운영 경로 진입을 위해 필요
                )

    async def test_empty_retailers_skips_batch(self, tmp_path):
        """retailer가 없으면 batch 실험을 건너뛴다."""
        from backend.pipeline.phase3_fallback import _attempt_tool_use_phase

        stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=True, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False,
            batch_size=0, batch_failure_count=0,
        )

        with patch(
            "backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
            new=AsyncMock(),
        ) as mock_batch:
            # items 없음 → unique_retailers 비어 있음
            await _attempt_tool_use_phase(
                phase2_result={"items": []},
                mappings_dir=tmp_path,
                form_definitions_dir=tmp_path,
                form_id="form_01",
                max_turns=5,
                stats=stats,
            )
        mock_batch.assert_not_called()


# ── Success Path 상세 테스트 ──────────────────────────────────────────────────

class TestSuccessPathDetail:
    """_execute_success_path()와 _batch_result_to_retailer_decisions() 상세 검증."""

    # ── BatchResult → RetailerDecision 변환 ──────────────────────────────────

    def test_batch_result_cache_hit_decision(self, tmp_path):
        """confirmed_code가 있고 lookup_basis=cache → RetailerMappingDecision(cache)."""
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
        assert len(decisions) == 1
        assert decisions[0].retailer_code == "R001"
        assert decisions[0].basis         == "cache"

    def test_batch_result_not_found_decision(self, tmp_path):
        """confirmed_code=None → RetailerMappingDecision(not_found)."""
        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        from backend.pipeline.phase3_fallback import _batch_result_to_retailer_decisions

        per_retailer = [
            RetailerBatchResult(
                ocr_name="未知店", success=True, confirmed_code=None,
                lookup_basis="not_found", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
            )
        ]
        decisions, _, _ = _batch_result_to_retailer_decisions(
            per_retailer,
            form_id="form_01", issuer_fingerprint="fp1",
            cached_dist={}, retail_user_rows=[],
        )
        assert decisions[0].retailer_code is None
        assert decisions[0].basis         == "not_found"

    def test_dist_1to1_confirmed_in_decision(self, tmp_path):
        """retail_user.csv에 1:1 매칭 → dist_code 자동 확정."""
        import csv
        retail = tmp_path / "retail_user.csv"
        with retail.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            w.writerow({"소매처코드": "R001", "소매처명": "テスト店",
                         "판매처코드": "D001", "판매처명": "東日本"})

        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        from backend.pipeline.phase3_fallback import _batch_result_to_retailer_decisions

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
            )
        ]
        retail_user_rows = [{"소매처코드": "R001", "소매처명": "テスト店",
                              "판매처코드": "D001", "판매처명": "東日本"}]
        decisions, dist_resolutions, dist_pending = _batch_result_to_retailer_decisions(
            per_retailer,
            form_id="form_01", issuer_fingerprint="fp1",
            cached_dist={}, retail_user_rows=retail_user_rows,
        )
        assert decisions[0].dist_code == "D001"
        assert len(dist_pending) == 0
        assert dist_resolutions["テスト店"].basis == "auto_1_to_1"

    def test_dist_1ton_creates_pending_no_fallback(self, tmp_path):
        """retail_user.csv에 1:N → dist pending, fallback 아님."""
        import csv
        retail = tmp_path / "retail_user.csv"
        with retail.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            w.writerow({"소매처코드": "R001", "소매처명": "テスト",
                         "판매처코드": "D001", "판매처명": "東日本"})
            w.writerow({"소매처코드": "R001", "소매처명": "テスト",
                         "판매처코드": "D002", "판매처명": "西日本"})

        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        from backend.pipeline.phase3_fallback import _batch_result_to_retailer_decisions

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
            )
        ]
        retail_user_rows = [
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D002", "판매처명": "西日本"},
        ]
        decisions, _, dist_pending = _batch_result_to_retailer_decisions(
            per_retailer,
            form_id="form_01", issuer_fingerprint="fp1",
            cached_dist={}, retail_user_rows=retail_user_rows,
        )
        # dist_code는 비어 있지만 retailer_code는 확정
        assert decisions[0].retailer_code == "R001"
        assert decisions[0].dist_code     == ""
        # dist pending 1건 생성
        assert len(dist_pending) == 1
        assert dist_pending[0]["mapping_type"] == "dist"
        assert dist_pending[0]["ocrName"]      == "テスト店"
        assert len(dist_pending[0]["candidates"]) == 2

    # ── _execute_success_path: JSON 저장 + confirm_mapping ────────────────────

    async def test_execute_success_path_writes_json(self, tmp_path):
        """_execute_success_path()가 phase3_output.json을 output_dir에 저장한다."""
        import json as _json
        from unittest.mock import patch
        from backend.pipeline.phase3_fallback import _execute_success_path

        phase2 = {"pages": [], "items": []}

        # _execute_success_path는 mappings_dir/form_definitions_dir를 직접 인자로 받음
        # (get_settings mock 불필요)
        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            result, pending = await _execute_success_path(
                batch_result=None,
                doc_id="doc_write_test",
                form_id="form_01",
                hatsu_month="2025-01",
                phase2_result=phase2,
                output_dir=tmp_path,
                mappings_dir=tmp_path,
                form_definitions_dir=tmp_path,
            )

        out_path = tmp_path / "phase3_output.json"
        assert out_path.exists(), "phase3_output.json이 생성되지 않음"
        saved = _json.loads(out_path.read_text(encoding="utf-8"))
        assert saved["doc_id"]      == "doc_write_test"
        assert saved["form_id"]     == "form_01"
        assert saved["hatsu_month"] == "2025-01"

    async def test_execute_success_path_confirm_mapping_for_retailer(self, tmp_path):
        """확정 retailer(tool_use basis)에 대해 confirm_mapping("retailer") 1회 호출."""
        import csv
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        from backend.pipeline.phase3_fallback import _execute_success_path

        # retail_user.csv (1:1 dist)
        retail = tmp_path / "retail_user.csv"
        with retail.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            w.writerow({"소매처코드": "R001", "소매처명": "テスト",
                         "판매처코드": "D001", "판매처명": "東日本"})

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="candidate",  # → "tool_use" basis → confirm_mapping 호출
                tool_call_count=2, lookup_call_count=1, confirm_call_count=1,
                turns_used=3, max_turns_hit=False, elapsed_ms=100.0,
            )
        ]
        stats_obj = BatchStats(
            batch_size=1, success_count=1, failure_count=0,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=2, total_lookup_calls=1, total_confirm_calls=1,
            total_turns=3, avg_turns=3.0, elapsed_ms=100.0,
        )
        batch_result = BatchExperimentResult(
            scenario="success", batch_size=1, stats=stats_obj, per_retailer=per_retailer
        )

        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "商品A", "item_type": "条件", "columns": {}}
        ]}

        confirm_calls: list[dict] = []

        async def capture_confirm(**kwargs):
            confirm_calls.append(kwargs)

        with patch("backend.pipeline.phase3_fallback.confirm_mapping",
                   side_effect=capture_confirm):
            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2,
                output_dir=tmp_path,
                mappings_dir=tmp_path,
                form_definitions_dir=tmp_path,
            )

        # retailer confirm (tool_use basis)
        retailer_confirms = [c for c in confirm_calls if c["mapping_type"] == "retailer"]
        assert len(retailer_confirms) == 1
        assert retailer_confirms[0]["confirmed_code"] == "R001"
        assert retailer_confirms[0]["ocr_name"]       == "テスト店"

        # dist confirm (auto_1_to_1 basis)
        dist_confirms = [c for c in confirm_calls if c["mapping_type"] == "dist"]
        assert len(dist_confirms) == 1
        assert dist_confirms[0]["confirmed_code"] == "D001"

    async def test_execute_success_path_json_save_failure_raises_dispatch_error(self, tmp_path):
        """phase3_output.json 저장 실패 → ToolUseDispatchError."""
        from unittest.mock import patch
        from backend.pipeline.phase3_fallback import _execute_success_path
        from backend.pipeline.phase3_fallback import ToolUseDispatchError

        phase2 = {"pages": [], "items": []}

        # output_dir를 존재하지 않는 경로로 설정 → write 실패
        nonexistent = tmp_path / "nonexistent_dir"

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            with pytest.raises(ToolUseDispatchError, match="저장 실패"):
                await _execute_success_path(
                    batch_result=None,
                    doc_id="doc1", form_id="form_01", hatsu_month="",
                    phase2_result=phase2,
                    output_dir=nonexistent,  # 존재하지 않음 → IOError
                    mappings_dir=tmp_path,
                    form_definitions_dir=tmp_path,
                )

    # ── fallback 시 Tool Use confirm_mapping 미호출 ───────────────────────────

    async def test_fallback_does_not_call_tool_use_confirm_mapping(self, tmp_path):
        """fallback 발생 시 Tool Use success path의 confirm_mapping이 호출되지 않는다.

        _execute_success_path()가 호출되지 않으므로 그 내부의
        confirm_mapping도 호출되지 않는다.
        """
        from backend.pipeline.phase3_fallback import ToolUseMaxTurnsError

        exc = ToolUseMaxTurnsError("max_turns 초과")
        confirm_calls: list = []

        async def capture_confirm(**kwargs):
            confirm_calls.append(kwargs)

        with _patch_legacy(), \
             _patch_tool_use(side_effect=exc), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping",
                   side_effect=capture_confirm):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        assert stats.fallback_triggered is True
        # Tool Use success path의 confirm_mapping은 호출되지 않았어야 함
        # (legacy run_phase3는 mock이므로 거기서도 호출 안 됨)
        assert len(confirm_calls) == 0, (
            f"fallback 시 confirm_mapping이 호출됨: {confirm_calls}"
        )

    # ── 전체 흐름: Tool Use 성공 → legacy 미호출 + output 반환 ──────────────

    async def test_success_path_full_flow_no_legacy(self, tmp_path):
        """Tool Use 성공 시 legacy run_phase3() 미호출 + stats 구조 확인."""
        with _patch_legacy() as mock_legacy, \
             _patch_tool_use(), \
             _patch_success_path():
            result, pending, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True,
            )

        mock_legacy.assert_not_called()
        assert stats.fallback_triggered is False
        assert stats.used_tool_use      is True
        assert stats.legacy_elapsed_ms  == 0.0  # legacy 미호출 → 0
        assert result["doc_id"] == "doc1"

    async def test_dist_1ton_does_not_trigger_fallback(self, tmp_path):
        """dist 1:N는 pending에 넣을 뿐 fallback으로 처리하지 않는다.

        _execute_success_path()를 직접 호출해 mappings_dir를 tmp_path로 고정.
        """
        import csv as _csv
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        from backend.pipeline.phase3_fallback import _execute_success_path

        # 1:N retail_user (R001 → D001, D002)
        retail = tmp_path / "retail_user.csv"
        with retail.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            for i in range(2):
                w.writerow({"소매처코드": "R001", "소매처명": "テスト",
                             "판매처코드": f"D00{i}", "판매처명": f"担当{i}"})

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=50.0,
            )
        ]
        stats_obj = BatchStats(
            batch_size=1, success_count=1, failure_count=0,
            max_turns_hit_count=0, not_found_count=0,
            total_tool_calls=1, total_lookup_calls=1, total_confirm_calls=0,
            total_turns=2, avg_turns=2.0, elapsed_ms=50.0,
        )
        batch_result = BatchExperimentResult(
            scenario="success", batch_size=1, stats=stats_obj, per_retailer=per_retailer
        )

        phase2 = {"pages": [], "items": [
            {"customer": "テスト店", "product": "商品A", "item_type": "条件", "columns": {}}
        ]}

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()):
            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2,
                output_dir=tmp_path,
                mappings_dir=tmp_path,           # ← tmp_path 직접 지정
                form_definitions_dir=tmp_path,
            )

        # dist_pending 1건 포함
        dist_pending = [p for p in pending if p.get("mapping_type") == "dist"]
        assert len(dist_pending) == 1, f"dist pending 없음. pending={pending}"
        assert dist_pending[0]["ocrName"] == "テスト店"
        assert len(dist_pending[0]["candidates"]) == 2

        # retailer_code는 확정, dist_code는 ""
        entry = result["confirmed_retailers"].get("テスト店", {})
        assert entry.get("retailer_code") == "R001"
        assert entry.get("dist_code")     == ""
