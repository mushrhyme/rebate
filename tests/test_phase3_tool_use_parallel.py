"""test_phase3_tool_use_parallel.py — Tool Use 병렬화 테스트

검증 항목:
  1. settings: phase3_tool_use_concurrency 필드/env/default
  2. retailer batch: concurrency limit 실제 준수
  3. retailer batch: 결과 순서 유지
  4. product: 결과 순서 유지
  5. product: cache hit는 Claude 호출 제외
  6. product: parse error 시 fallback 정책 유지
  7. retailer: API error 시 fallback 정책 유지
  8. product 병렬 token 합산 정확성
  9. fallback 시 partial usage 보존
  10. confirm_mapping 저장은 순차/1회

실행: pytest tests/test_phase3_tool_use_parallel.py -v
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    ToolUseApiError,
    ToolUseDispatchError,
    ToolUseMaxTurnsError,
    ToolUseParseError,
    ToolUseTokenStats,
    _build_product_decisions_with_tool_use,
    _run_single_product_mapping,
    run_phase3_with_tool_use_or_fallback,
)
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
        {"customer": "テスト店A", "product": "商品A", "item_type": "条件", "columns": {}},
        {"customer": "テスト店B", "product": "商品B", "item_type": "条件", "columns": {}},
    ],
}

_LEGACY = (
    {"doc_id": "doc1", "form_id": "form_01", "hatsu_month": "",
     "issuer": {}, "confirmed_retailers": {}, "confirmed_products": {},
     "items": [], "cover_totals": {}},
    [],
)


def _mock_settings(tmp: Path, *, model: str = "claude-haiku-4-5-20251001",
                   concurrency: int = 1) -> MagicMock:
    s = MagicMock()
    s.mappings_dir           = tmp
    s.form_definitions_dir   = tmp
    s.anthropic_api_key      = "fake"
    s.phase3_tool_use_model  = model
    s.phase3_tool_use_concurrency = concurrency
    return s


def _sp_cache(product_code="P001"):
    sp = MagicMock()
    sp.basis = "cache"
    sp.product_code = product_code
    sp.candidates = []
    return sp


def _sp_candidate(candidates=None):
    sp = MagicMock()
    sp.basis = "candidate"
    sp.product_code = None
    sp.candidates = candidates or []
    return sp


def _sp_not_found():
    sp = MagicMock()
    sp.basis = "not_found"
    sp.product_code = None
    sp.candidates = []
    return sp


def _resp(stop, *blocks, usage=None):
    r = MagicMock()
    r.stop_reason = stop
    r.content = list(blocks)
    u = MagicMock()
    u.input_tokens = 50
    u.output_tokens = 20
    u.cache_read_input_tokens = 0
    u.cache_creation_input_tokens = 0
    r.usage = usage or u
    return r


def _text(t):
    b = MagicMock(); b.type = "text"; b.text = t; return b


# ── 1. settings 필드/default 테스트 ──────────────────────────────────────────

class TestConcurrencySettings:
    def test_settings_has_concurrency_field(self):
        """Settings에 phase3_tool_use_concurrency 필드가 있다."""
        from backend.core.config import Settings
        assert "phase3_tool_use_concurrency" in Settings.model_fields

    def test_settings_default_concurrency_is_one(self):
        """phase3_tool_use_concurrency 기본값은 1(순차)이다."""
        from backend.core.config import Settings
        default = Settings.model_fields["phase3_tool_use_concurrency"].default
        assert default == 1

    async def test_concurrency_zero_clamped_to_one(self, tmp_path):
        """settings.phase3_tool_use_concurrency=0이면 _concurrency는 1로 보정된다."""
        s = _mock_settings(tmp_path, concurrency=0)
        captured = []

        async def _fake_attempt(**kwargs):
            captured.append(kwargs.get("concurrency"))
            return None

        async def _fake_success(**kwargs):
            return _LEGACY[0], _LEGACY[1]

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=_fake_attempt), \
             patch("backend.pipeline.phase3_fallback._execute_success_path",
                   new=_fake_success), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True, settings=s,
            )

        assert captured[0] == 1, f"concurrency=0 → expected 1, got {captured[0]}"

    async def test_concurrency_negative_clamped_to_one(self, tmp_path):
        """settings.phase3_tool_use_concurrency 음수 → 1로 보정."""
        s = _mock_settings(tmp_path, concurrency=-5)
        captured = []

        async def _fake_attempt(**kwargs):
            captured.append(kwargs.get("concurrency"))
            return None

        async def _fake_success(**kwargs):
            return _LEGACY[0], _LEGACY[1]

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   new=_fake_attempt), \
             patch("backend.pipeline.phase3_fallback._execute_success_path",
                   new=_fake_success), \
             patch("backend.pipeline.phase3_fallback._record_tool_use_token_usage",
                   new=AsyncMock()):
            await run_phase3_with_tool_use_or_fallback(
                "doc1", _PHASE2, tmp_path, "form_01",
                enable_tool_use=True, settings=s,
            )

        assert captured[0] == 1


# ── 2. retailer batch concurrency limit 준수 테스트 ──────────────────────────

class TestRetailerBatchConcurrencyLimit:
    async def test_semaphore_limits_simultaneous_calls(self, tmp_path):
        """concurrency=1이면 retailer 동시 실행 수가 1로 제한된다."""
        from backend.experiments.batch_tool_use_experiment import (
            run_batch_retailer_experiment,
            SCENARIO_SUCCESS,
        )

        max_concurrent = 0
        current = 0

        async def _slow_run_one(**kwargs):
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0)  # yield to event loop
            current -= 1
            return MagicMock(
                ocr_name=kwargs["ocr_name"], success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=1, lookup_call_count=1,
                confirm_call_count=0, turns_used=2, max_turns_hit=False, elapsed_ms=10.0,
                input_tokens=0, output_tokens=0, api_call_count=0, error=None,
            )

        with patch("backend.experiments.batch_tool_use_experiment._run_one",
                   side_effect=_slow_run_one):
            await run_batch_retailer_experiment(
                ocr_names=["A", "B", "C", "D"],
                mappings_dir=tmp_path,
                scenario=SCENARIO_SUCCESS,
                concurrency=1,
            )

        assert max_concurrent == 1, f"concurrency=1이지만 동시 실행={max_concurrent}"

    async def test_concurrency_2_allows_two_simultaneous(self, tmp_path):
        """concurrency=2이면 동시 실행 수가 최대 2까지 허용된다."""
        from backend.experiments.batch_tool_use_experiment import (
            run_batch_retailer_experiment, SCENARIO_SUCCESS,
        )

        max_concurrent = 0
        current = 0

        async def _slow_run_one(**kwargs):
            nonlocal max_concurrent, current
            current += 1
            max_concurrent = max(max_concurrent, current)
            await asyncio.sleep(0)
            current -= 1
            return MagicMock(
                ocr_name=kwargs["ocr_name"], success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=0, lookup_call_count=0,
                confirm_call_count=0, turns_used=1, max_turns_hit=False, elapsed_ms=5.0,
                input_tokens=0, output_tokens=0, api_call_count=0, error=None,
            )

        with patch("backend.experiments.batch_tool_use_experiment._run_one",
                   side_effect=_slow_run_one):
            await run_batch_retailer_experiment(
                ocr_names=["A", "B", "C", "D"],
                mappings_dir=tmp_path,
                scenario=SCENARIO_SUCCESS,
                concurrency=2,
            )

        assert max_concurrent <= 2, f"concurrency=2이지만 동시 실행={max_concurrent}"
        assert max_concurrent >= 1


# ── 3. retailer batch 결과 순서 유지 테스트 ──────────────────────────────────

class TestRetailerBatchOrderPreserved:
    async def test_per_retailer_order_matches_input(self, tmp_path):
        """병렬 실행 후 per_retailer 순서가 입력 순서와 동일하다."""
        from backend.experiments.batch_tool_use_experiment import (
            run_batch_retailer_experiment, SCENARIO_SUCCESS,
        )
        import asyncio as _asyncio

        names = ["店A", "店B", "店C", "店D", "店E"]
        # 역순 완료 시뮬레이션: 마지막 이름이 가장 빨리 완료
        delays = {n: (len(names) - i) * 0.001 for i, n in enumerate(names)}

        async def _delayed_run_one(**kwargs):
            n = kwargs["ocr_name"]
            await _asyncio.sleep(delays[n])
            return MagicMock(
                ocr_name=n, success=True, confirmed_code="R001",
                lookup_basis="cache", tool_call_count=0, lookup_call_count=0,
                confirm_call_count=0, turns_used=1, max_turns_hit=False, elapsed_ms=1.0,
                input_tokens=0, output_tokens=0, api_call_count=0, error=None,
            )

        with patch("backend.experiments.batch_tool_use_experiment._run_one",
                   side_effect=_delayed_run_one):
            result = await run_batch_retailer_experiment(
                ocr_names=names,
                mappings_dir=tmp_path,
                scenario=SCENARIO_SUCCESS,
                concurrency=len(names),  # 모두 동시 실행
            )

        returned_names = [r.ocr_name for r in result.per_retailer]
        assert returned_names == names, f"순서 불일치: {returned_names}"


# ── 4. product 결과 순서 유지 테스트 ─────────────────────────────────────────

class TestProductOrderPreserved:
    async def test_product_decisions_order_matches_input(self, tmp_path):
        """병렬 product 실행 후 decisions 순서가 입력 순서와 동일하다."""
        import asyncio as _asyncio

        products = ["商品A", "商品B", "商品C", "商品D"]
        delays   = {p: (len(products) - i) * 0.001 for i, p in enumerate(products)}

        async def _slow_search_product(**kwargs):
            await _asyncio.sleep(delays[kwargs["ocr_name"]])
            return _sp_not_found()

        with patch("backend.pipeline.phase3_fallback.search_product",
                   side_effect=_slow_search_product):
            decisions = await _build_product_decisions_with_tool_use(
                products, tmp_path,
                product_client=None,
                concurrency=len(products),
            )

        returned = [d.ocr_name for d in decisions]
        assert returned == products, f"순서 불일치: {returned}"


# ── 5. cache hit는 Claude 호출 제외 테스트 ───────────────────────────────────

class TestCacheHitSkipsClaude:
    async def test_cache_hit_does_not_call_single_mapping(self, tmp_path):
        """cache hit product에 대해 _run_single_product_mapping을 호출하지 않는다."""
        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=_sp_cache("P_CACHED"))), \
             patch("backend.pipeline.phase3_fallback._run_single_product_mapping",
                   new=AsyncMock()) as mock_single:
            decisions = await _build_product_decisions_with_tool_use(
                ["商品A"], tmp_path,
                product_client=MagicMock(),
                concurrency=2,
            )

        mock_single.assert_not_called()
        assert decisions[0].basis == "cache"
        assert decisions[0].product_code == "P_CACHED"


# ── 6. product parse error → fallback 정책 테스트 ────────────────────────────

class TestProductParseErrorFallbackPolicy:
    async def test_parse_error_in_one_product_raises_fallback_trigger(self, tmp_path):
        """병렬 product 중 하나가 ToolUseParseError → ToolUseFallbackTrigger 전파."""
        sp_candidate = _sp_candidate()

        async def _failing_single_mapping(ocr_name, candidates, mappings_dir, **kwargs):
            raise ToolUseParseError("JSON 파싱 실패")

        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=sp_candidate)), \
             patch("backend.pipeline.phase3_fallback._run_single_product_mapping",
                   side_effect=_failing_single_mapping):
            with pytest.raises(ToolUseParseError):
                await _build_product_decisions_with_tool_use(
                    ["商品A", "商品B"], tmp_path,
                    product_client=MagicMock(),
                    concurrency=2,
                )

    async def test_parse_error_preserves_completed_tokens_before_failure(self, tmp_path):
        """parse error 발생 전 완료된 호출의 token이 _token_acc에 누적된다."""
        sp_candidate = _sp_candidate()
        token_acc = ToolUseTokenStats()

        call_count = [0]

        async def _flaky_mapping(ocr_name, *args, **kwargs):
            call_count[0] += 1
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_input_tokens  += 100
                acc.product_api_calls     += 1
            if call_count[0] == 2:
                raise ToolUseParseError("2번째 파싱 실패")
            from backend.pipeline.phase3_tool_result_adapter import ProductMappingDecision
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code=None,
                product_name="", basis="not_found", confidence=0.0,
            )

        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=sp_candidate)), \
             patch("backend.pipeline.phase3_fallback._run_single_product_mapping",
                   side_effect=_flaky_mapping):
            with pytest.raises(ToolUseParseError):
                await _build_product_decisions_with_tool_use(
                    ["商品A", "商品B"], tmp_path,
                    product_client=MagicMock(),
                    concurrency=2,
                    _token_acc=token_acc,
                )

        # 실패한 호출도 포함해 모든 완료된 token이 보존됨
        assert token_acc.product_api_calls >= 1, "완료된 호출의 token이 없음"
        assert token_acc.product_input_tokens >= 100


# ── 7. retailer API error → fallback 정책 테스트 ─────────────────────────────

class TestRetailerApiErrorFallbackPolicy:
    async def test_api_error_in_retailer_batch_triggers_fallback(self, tmp_path):
        """retailer batch 중 API error → ToolUseApiError(fallback trigger) 전파."""
        from backend.pipeline.phase3_fallback import _attempt_tool_use_phase, Phase3FallbackStats

        import anthropic as _anthropic

        async def _api_error_batch(**kwargs):
            raise _anthropic.APIConnectionError(request=MagicMock())

        stats = Phase3FallbackStats(
            enable_tool_use=True, used_tool_use=True, fallback_triggered=False,
            fallback_reason=None, fallback_class=None,
            tool_use_elapsed_ms=0, legacy_elapsed_ms=0, total_elapsed_ms=0,
            max_turns_hit=False, api_retry_failed=False, batch_size=0, batch_failure_count=0,
        )

        with patch("backend.pipeline.phase3_fallback.run_batch_retailer_experiment",
                   side_effect=_api_error_batch):
            with pytest.raises(ToolUseApiError):
                await _attempt_tool_use_phase(
                    phase2_result=_PHASE2,
                    mappings_dir=tmp_path,
                    form_definitions_dir=tmp_path,
                    form_id="form_01",
                    max_turns=5,
                    stats=stats,
                    anthropic_api_key="fake",  # 운영 경로 진입을 위해 필요
                )


# ── 8. 병렬 product token 합산 테스트 ────────────────────────────────────────

class TestParallelProductTokenSum:
    async def test_parallel_product_tokens_summed_correctly(self, tmp_path):
        """n개 병렬 product 호출의 token이 double count 없이 합산된다."""
        n = 4
        sp_candidate = _sp_candidate()
        token_acc = ToolUseTokenStats()

        async def _mock_mapping(ocr_name, *args, **kwargs):
            acc = kwargs.get("_token_acc")
            if acc is not None:
                acc.product_input_tokens  += 100
                acc.product_output_tokens += 40
                acc.product_api_calls     += 1
            from backend.pipeline.phase3_tool_result_adapter import ProductMappingDecision
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code=None,
                product_name="", basis="not_found", confidence=0.0,
            )

        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=sp_candidate)), \
             patch("backend.pipeline.phase3_fallback._run_single_product_mapping",
                   side_effect=_mock_mapping):
            decisions = await _build_product_decisions_with_tool_use(
                [f"商品{i}" for i in range(n)], tmp_path,
                product_client=MagicMock(),
                concurrency=n,
                _token_acc=token_acc,
            )

        assert len(decisions) == n
        assert token_acc.product_input_tokens  == 100 * n, f"expected {100*n}, got {token_acc.product_input_tokens}"
        assert token_acc.product_output_tokens ==  40 * n
        assert token_acc.product_api_calls     ==       n

    async def test_cache_hit_does_not_add_to_token_acc(self, tmp_path):
        """cache hit product는 token_acc에 값을 더하지 않는다."""
        token_acc = ToolUseTokenStats()

        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=_sp_cache("P001"))):
            await _build_product_decisions_with_tool_use(
                ["商品A", "商品B"], tmp_path,
                product_client=MagicMock(),
                concurrency=2,
                _token_acc=token_acc,
            )

        assert token_acc.product_api_calls    == 0
        assert token_acc.product_input_tokens == 0


# ── 9. fallback 시 partial usage 보존 테스트 ─────────────────────────────────

class TestFallbackPartialUsagePreserved:
    async def test_partial_token_preserved_after_product_parse_error(self, tmp_path):
        """product parse error → fallback 시 완료된 product token이 stats에 보존된다."""
        token_acc = ToolUseTokenStats()
        call_order = []

        async def _mixed_mapping(ocr_name, *args, **kwargs):
            acc = kwargs.get("_token_acc")
            call_order.append(ocr_name)
            if acc is not None:
                acc.product_input_tokens += 80
                acc.product_api_calls    += 1
            if ocr_name == "商品B":
                raise ToolUseParseError("B 파싱 실패")
            from backend.pipeline.phase3_tool_result_adapter import ProductMappingDecision
            return ProductMappingDecision(
                ocr_name=ocr_name, product_code=None,
                product_name="", basis="not_found", confidence=0.0,
            )

        with patch("backend.pipeline.phase3_fallback.search_product",
                   new=AsyncMock(return_value=_sp_candidate())), \
             patch("backend.pipeline.phase3_fallback._run_single_product_mapping",
                   side_effect=_mixed_mapping):
            with pytest.raises(ToolUseParseError):
                await _build_product_decisions_with_tool_use(
                    ["商品A", "商品B"], tmp_path,
                    product_client=MagicMock(),
                    concurrency=1,
                    _token_acc=token_acc,
                )

        # 성공/실패 모두 token 누적됨
        assert token_acc.product_api_calls    >= 1
        assert token_acc.product_input_tokens >= 80


# ── 10. confirm_mapping 저장 순차/1회 테스트 ─────────────────────────────────

class TestConfirmMappingStillSequential:
    async def test_confirm_mapping_called_once_per_retailer_in_success_path(self, tmp_path):
        """_execute_success_path에서 retailer confirm_mapping이 product와 무관하게 1회 호출된다."""
        import csv
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        from backend.pipeline.phase3_fallback import _execute_success_path

        retail = tmp_path / "retail_user.csv"
        with retail.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            w.writerow({"소매처코드": "R001", "소매처명": "テスト", "판매처코드": "D001", "판매처명": "東日本"})

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="candidate",
                tool_call_count=2, lookup_call_count=1, confirm_call_count=1,
                turns_used=3, max_turns_hit=False, elapsed_ms=100.0,
            )
        ]
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

        confirm_calls: list[dict] = []

        async def _capture(**kwargs):
            confirm_calls.append({"type": kwargs.get("mapping_type"), "code": kwargs.get("confirmed_code")})

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", side_effect=_capture), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result={"pages": [], "items": []},
                output_dir=tmp_path,
                mappings_dir=tmp_path,
                form_definitions_dir=tmp_path,
                concurrency=4,  # 높은 concurrency에서도 confirm_mapping은 순차
            )

        retailer_confirms = [c for c in confirm_calls if c["type"] == "retailer"]
        assert len(retailer_confirms) == 1, f"retailer confirm은 1회여야 함: {retailer_confirms}"

    async def test_concurrency_setting_passed_to_product_build(self, tmp_path):
        """_execute_success_path에서 concurrency가 product build 함수에 전달된다."""
        captured_concurrency = []

        async def _mock_build(*args, **kwargs):
            captured_concurrency.append(kwargs.get("concurrency"))
            return []

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   side_effect=_mock_build):
            from backend.pipeline.phase3_fallback import _execute_success_path
            await _execute_success_path(
                batch_result=None,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result={"pages": [], "items": []},
                output_dir=tmp_path,
                mappings_dir=tmp_path,
                form_definitions_dir=tmp_path,
                concurrency=3,
            )

        assert captured_concurrency[0] == 3, f"expected concurrency=3, got {captured_concurrency}"
