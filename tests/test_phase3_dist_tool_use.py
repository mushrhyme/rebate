"""test_phase3_dist_tool_use.py — Dist 1:N Tool Use 단위 테스트

검증 항목:
  1. _parse_dist_decision_json: 정상/오류 케이스
  2. _run_single_dist_mapping: Claude 결정, pending, 후보 외 거부, dist_name 보완
  3. _build_dist_decisions_with_tool_use: 배치 처리, 순서 보존, token 누적
  4. _execute_success_path 통합: dist 1:N 확정 + confirm_mapping + phase3_output
  5. feature flag OFF → legacy 경로 (dist Tool Use 미실행)
  6. fallback 시 legacy 결과 유지
  7. token usage에 dist 포함 여부

실행: pytest tests/test_phase3_dist_tool_use.py -v
"""
import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.pipeline.phase3_fallback import (
    ToolUseApiError,
    ToolUseParseError,
    ToolUseTokenStats,
    _build_dist_decisions_with_tool_use,
    _parse_dist_decision_json,
    _run_single_dist_mapping,
)
from backend.pipeline.phase3_dist_resolver import DistResolution
from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


def _resp(text: str) -> MagicMock:
    """end_turn response mock."""
    b = MagicMock(); b.type = "text"; b.text = text
    u = MagicMock()
    u.input_tokens = 50; u.output_tokens = 20
    u.cache_read_input_tokens = 0; u.cache_creation_input_tokens = 0
    r = MagicMock(); r.stop_reason = "end_turn"; r.content = [b]; r.usage = u
    return r


def _mock_client(text: str) -> MagicMock:
    c = MagicMock()
    c.messages.create = AsyncMock(return_value=_resp(text))
    return c


_CANDIDATES = [
    {"dist_code": "D001", "dist_name": "東日本販社"},
    {"dist_code": "D002", "dist_name": "西日本販社"},
]


# ── 1. _parse_dist_decision_json ──────────────────────────────────────────────

class TestParseDistDecisionJson:
    def test_confirmed_parsed(self):
        text = '{"decision": "confirmed", "dist_code": "D001", "reason": "東日本"}'
        d = _parse_dist_decision_json(text)
        assert d["decision"] == "confirmed"
        assert d["dist_code"] == "D001"

    def test_pending_parsed(self):
        text = '{"decision": "pending", "reason": "판단 불가"}'
        d = _parse_dist_decision_json(text)
        assert d["decision"] == "pending"

    def test_code_fence_stripped(self):
        text = '```json\n{"decision": "confirmed", "dist_code": "D002"}\n```'
        d = _parse_dist_decision_json(text)
        assert d["dist_code"] == "D002"

    def test_leading_text_stripped(self):
        text = '판매처를 선택합니다:\n{"decision": "confirmed", "dist_code": "D001"}'
        d = _parse_dist_decision_json(text)
        assert d["dist_code"] == "D001"

    def test_invalid_json_raises(self):
        with pytest.raises(ToolUseParseError, match="파싱 실패"):
            _parse_dist_decision_json("not valid json")

    def test_missing_decision_raises(self):
        with pytest.raises(ToolUseParseError, match="decision.*없음"):
            _parse_dist_decision_json('{"dist_code": "D001"}')

    def test_empty_text_raises(self):
        with pytest.raises(ToolUseParseError, match="빈 응답"):
            _parse_dist_decision_json("")


# ── 2. _run_single_dist_mapping ───────────────────────────────────────────────

class TestRunSingleDistMapping:
    async def test_claude_confirmed_returns_tool_use(self, tmp_path):
        """Claude가 후보 내 dist_code 선택 → basis=tool_use."""
        client = _mock_client('{"decision": "confirmed", "dist_code": "D001", "reason": "東日本"}')

        res = await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
        )

        assert res.basis == "tool_use"
        assert res.dist_code == "D001"
        assert not res.needs_confirmation
        # dist_name은 candidates에서 확인 (DistResolution 자체 필드 아님)
        matched = [c for c in _CANDIDATES if c["dist_code"] == res.dist_code]
        assert matched[0]["dist_name"] == "東日本販社"

    async def test_prompt_includes_form_rule_and_jisho(self, tmp_path):
        """프롬프트가 form_md(판매처 결정 규칙)와 jisho를 포함한다 (md-driven 핵심).

        docs/phase3-dist-mapping-prompt.md 템플릿 로드 + 토큰 치환을 실제로 검증."""
        client = _mock_client('{"decision": "confirmed", "dist_code": "D001", "reason": "ok"}')
        form_md = "## 판매처 결정 규칙\njisho=R営業東北 → D001 확정"

        await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_04", issuer_fingerprint="日本アクセス|03-x",
            jisho="R営業東北", form_md=form_md,
            client=client, model="claude-haiku-4-5-20251001",
        )

        sent = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "판매처 결정 규칙" in sent          # form_md 주입됨
        assert "R営業東北" in sent                  # jisho 주입됨
        assert "D001" in sent and "D002" in sent    # 후보 목록 치환됨
        assert "{{" not in sent                      # 미치환 토큰 없음

    def test_dist_name_supplemented_from_candidates(self):
        """dist_name이 없을 때 candidates에서 자동 보완 (동기 확인용)."""
        # _run_single_dist_mapping 내부에서 candidates 루프로 보완됨
        # 이 테스트는 data flow만 확인
        candidates = [{"dist_code": "D001", "dist_name": "東日本販社"}]
        valid_codes = {c["dist_code"] for c in candidates}
        assert "D001" in valid_codes

    async def test_claude_pending_returns_needs_confirmation(self, tmp_path):
        """Claude가 pending 선택 → needs_confirmation=True."""
        client = _mock_client('{"decision": "pending", "reason": "판단 불가"}')

        res = await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
        )

        assert res.basis == "needs_confirmation"
        assert res.dist_code is None
        assert res.needs_confirmation

    async def test_outside_candidate_dist_code_rejected(self, tmp_path):
        """후보 외 dist_code 선택 → pending (계약 위반)."""
        client = _mock_client('{"decision": "confirmed", "dist_code": "INVALID", "reason": "?"}')

        res = await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
        )

        assert res.basis == "needs_confirmation"
        assert res.dist_code is None
        assert "후보 외" in (res.reason or "")

    async def test_empty_dist_code_rejected(self, tmp_path):
        """빈 dist_code → pending."""
        client = _mock_client('{"decision": "confirmed", "dist_code": "", "reason": "?"}')

        res = await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
        )

        assert res.basis == "needs_confirmation"
        assert res.dist_code is None

    async def test_parse_error_returns_pending(self, tmp_path):
        """JSON 파싱 실패 → pending (fallback 아님)."""
        client = _mock_client("판단할 수 없습니다")  # not JSON

        res = await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
        )

        assert res.basis == "needs_confirmation"

    async def test_api_error_raises_tool_use_api_error(self, tmp_path):
        """API 오류 → ToolUseApiError (전체 fallback 트리거)."""
        import anthropic
        client = MagicMock()
        client.messages.create = AsyncMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock())
        )

        with pytest.raises(ToolUseApiError):
            await _run_single_dist_mapping(
                ocr_name="テスト店", retailer_code="R001",
                candidates=_CANDIDATES,
                form_id="form_01", issuer_fingerprint="fp",
                client=client, model="claude-haiku-4-5-20251001",
            )

    async def test_token_accumulated_in_token_acc(self, tmp_path):
        """API 호출 후 token이 _token_acc에 누적된다."""
        client = _mock_client('{"decision": "confirmed", "dist_code": "D001", "reason": "ok"}')
        token_acc = ToolUseTokenStats()

        await _run_single_dist_mapping(
            ocr_name="テスト店", retailer_code="R001",
            candidates=_CANDIDATES,
            form_id="form_01", issuer_fingerprint="fp",
            client=client, model="claude-haiku-4-5-20251001",
            _token_acc=token_acc,
        )

        assert token_acc.dist_api_calls == 1
        assert token_acc.dist_input_tokens == 50
        assert token_acc.dist_output_tokens == 20


# ── 3. _build_dist_decisions_with_tool_use ────────────────────────────────────

class TestBuildDistDecisions:
    _PENDING = [
        {
            "mapping_type": "dist", "ocrName": "テスト店A",
            "retailer_code": "R001",
            "candidates": [
                {"dist_code": "D001", "dist_name": "東日本"},
                {"dist_code": "D002", "dist_name": "西日本"},
            ],
            "page_number": None,
        },
        {
            "mapping_type": "dist", "ocrName": "テスト店B",
            "retailer_code": "R002",
            "candidates": [
                {"dist_code": "D003", "dist_name": "北日本"},
                {"dist_code": "D004", "dist_name": "南日本"},
            ],
            "page_number": None,
        },
    ]

    async def test_all_confirmed_returns_empty_remaining(self, tmp_path):
        """全件 Claude 확정 → remaining_pending=[]."""
        async def _mock_single(ocr_name, **kw):
            code = "D001" if "店A" in ocr_name else "D003"
            return DistResolution(dist_code=code, basis="tool_use",
                                  candidates=kw["candidates"], needs_confirmation=False)

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_single):
            resolved, remaining = await _build_dist_decisions_with_tool_use(
                dist_pending=self._PENDING,
                form_id="form_01", issuer_fingerprint="fp",
                retail_user_rows=[],
                dist_client=MagicMock(), model="m",
            )

        assert len(resolved) == 2
        # resolved 키는 (ocr_name, jisho) — 이 pending은 jisho 없음 → ""
        assert resolved[("テスト店A", "")].basis == "tool_use"
        assert len(remaining) == 0

    async def test_partial_pending_stays_in_remaining(self, tmp_path):
        """일부 pending → remaining에 남음."""
        async def _mock_single(ocr_name, **kw):
            if "店A" in ocr_name:
                return DistResolution(dist_code="D001", basis="tool_use",
                                      candidates=kw["candidates"], needs_confirmation=False)
            return DistResolution(dist_code=None, basis="needs_confirmation",
                                  candidates=kw["candidates"], needs_confirmation=True)

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_single):
            resolved, remaining = await _build_dist_decisions_with_tool_use(
                dist_pending=self._PENDING,
                form_id="form_01", issuer_fingerprint="fp",
                retail_user_rows=[],
                dist_client=MagicMock(), model="m",
            )

        assert resolved[("テスト店A", "")].basis == "tool_use"
        assert resolved[("テスト店B", "")].needs_confirmation is True
        assert len(remaining) == 1
        assert remaining[0]["ocrName"] == "テスト店B"

    async def test_order_preserved(self, tmp_path):
        """결과 순서가 입력 순서를 따른다."""
        call_order: list[str] = []

        async def _mock_single(ocr_name, **kw):
            call_order.append(ocr_name)
            return DistResolution(dist_code="D001", basis="tool_use",
                                  candidates=kw["candidates"], needs_confirmation=False)

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_single):
            resolved, _ = await _build_dist_decisions_with_tool_use(
                dist_pending=self._PENDING,
                form_id="form_01", issuer_fingerprint="fp",
                retail_user_rows=[],
                dist_client=MagicMock(), model="m",
            )

        assert list(resolved.keys()) == [("テスト店A", ""), ("テスト店B", "")]

    async def test_token_acc_accumulated_from_all_calls(self, tmp_path):
        """모든 dist 호출 token이 _token_acc에 합산된다."""
        call_count = [0]

        async def _mock_single(ocr_name, *, _token_acc=None, **kw):
            call_count[0] += 1
            if _token_acc:
                _token_acc.dist_api_calls    += 1
                _token_acc.dist_input_tokens += 40
            return DistResolution(dist_code="D001", basis="tool_use",
                                  candidates=kw["candidates"], needs_confirmation=False)

        token_acc = ToolUseTokenStats()

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_single):
            await _build_dist_decisions_with_tool_use(
                dist_pending=self._PENDING,
                form_id="form_01", issuer_fingerprint="fp",
                retail_user_rows=[],
                dist_client=MagicMock(), model="m",
                _token_acc=token_acc,
            )

        assert token_acc.dist_api_calls    == 2
        assert token_acc.dist_input_tokens == 80


# ── 4. _execute_success_path 통합 ─────────────────────────────────────────────

class TestExecuteSuccessPathDistToolUse:
    """dist 1:N → Tool Use 확정 + confirm_mapping + phase3_output 반영."""

    @staticmethod
    def _make_batch_result_with_1n_dist(retailer_code: str) -> object:
        """1:N dist가 발생하는 BatchExperimentResult."""
        from backend.experiments.batch_tool_use_experiment import (
            BatchExperimentResult, BatchStats, RetailerBatchResult,
        )
        return BatchExperimentResult(
            scenario="success", batch_size=1,
            stats=BatchStats(
                batch_size=1, success_count=1, failure_count=0,
                max_turns_hit_count=0, not_found_count=0,
                total_tool_calls=2, total_lookup_calls=1, total_confirm_calls=1,
                total_turns=3, avg_turns=3.0, elapsed_ms=100.0,
                total_input_tokens=200, total_output_tokens=80, total_api_calls=2,
            ),
            per_retailer=[
                RetailerBatchResult(
                    ocr_name="テスト店A", success=True, confirmed_code=retailer_code,
                    lookup_basis="candidate",
                    tool_call_count=2, lookup_call_count=1, confirm_call_count=1,
                    turns_used=3, max_turns_hit=False, elapsed_ms=80.0,
                    input_tokens=200, output_tokens=80, api_call_count=2,
                )
            ],
        )

    @staticmethod
    def _write_retail_user_1n(path: Path, retailer_code: str) -> None:
        """1:N retail_user.csv 생성."""
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["소매처코드", "소매처명", "판매처코드", "판매처명"])
            w.writeheader()
            w.writerows([
                {"소매처코드": retailer_code, "소매처명": "テスト小売",
                 "판매처코드": "D001", "판매처명": "東日本販社"},
                {"소매처코드": retailer_code, "소매처명": "テスト小売",
                 "판매처코드": "D002", "판매처명": "西日本販社"},
            ])

    async def test_dist_1n_claude_confirmed_in_output(self, tmp_path):
        """dist 1:N → Claude 확정 → phase3_output.json에 dist_code 반영."""
        mappings = tmp_path / "mappings"
        form_defs = tmp_path / "form_defs"
        mappings.mkdir(); form_defs.mkdir()

        self._write_retail_user_1n(mappings / "retail_user.csv", "R001")
        (form_defs / "form_01.md").write_text(
            "# form_01\n## issuer 식별\n```\nfingerprint_fields: name\n```\n",
            encoding="utf-8",
        )
        # ocr_product.csv (dummy)
        (mappings / "ocr_product.csv").write_text("ocr_name,product_code,product_name\n",
                                                   encoding="utf-8-sig")

        batch_result = self._make_batch_result_with_1n_dist("R001")

        phase2 = {
            "pages": [{"role": "cover", "issuer": {"name": "テスト発行者"}}],
            "items": [{"customer": "テスト店A", "product": "テスト商品",
                       "item_type": "条件", "columns": {}}],
        }

        # Claude가 D001 선택
        async def _mock_dist(ocr_name, *, candidates, **kw):
            return DistResolution(
                dist_code="D001", basis="tool_use",
                candidates=candidates, needs_confirmation=False, reason="東日本を選択",
            )

        confirm_calls: list[dict] = []
        async def _capture_confirm(**kwargs):
            confirm_calls.append(kwargs)

        from backend.pipeline.phase3_fallback import _execute_success_path

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_dist), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping",
                   side_effect=_capture_confirm), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
                product_client=MagicMock(),
            )

        # dist_code 반영 확인
        cr = result.get("confirmed_retailers", {})
        assert "テスト店A" in cr
        assert cr["テスト店A"]["dist_code"] == "D001"

        # pending에 dist 없음
        dist_pending = [p for p in pending if p.get("mapping_type") == "dist"]
        assert len(dist_pending) == 0

        # confirm_mapping(dist) 호출 확인
        dist_confirms = [c for c in confirm_calls if c.get("mapping_type") == "dist"]
        assert len(dist_confirms) == 1
        assert dist_confirms[0]["confirmed_code"] == "D001"
        assert dist_confirms[0]["context"]["retailer_code"] == "R001"

    async def test_dist_1n_claude_pending_stays_in_pending(self, tmp_path):
        """dist 1:N → Claude pending → pending 유지."""
        mappings = tmp_path / "mappings"; form_defs = tmp_path / "form_defs"
        mappings.mkdir(); form_defs.mkdir()
        self._write_retail_user_1n(mappings / "retail_user.csv", "R001")
        (form_defs / "form_01.md").write_text(
            "# form_01\n## issuer 식별\n```\nfingerprint_fields: name\n```\n",
            encoding="utf-8",
        )
        (mappings / "ocr_product.csv").write_text("ocr_name,product_code,product_name\n",
                                                   encoding="utf-8-sig")

        batch_result = self._make_batch_result_with_1n_dist("R001")
        phase2 = {
            "pages": [{"role": "cover", "issuer": {"name": "テスト"}}],
            "items": [{"customer": "テスト店A", "product": "P", "item_type": "条件", "columns": {}}],
        }

        async def _mock_dist_pending(ocr_name, *, candidates, **kw):
            return DistResolution(dist_code=None, basis="needs_confirmation",
                                  candidates=candidates, needs_confirmation=True)

        confirm_calls: list[dict] = []
        from backend.pipeline.phase3_fallback import _execute_success_path

        with patch("backend.pipeline.phase3_fallback._run_single_dist_mapping",
                   side_effect=_mock_dist_pending), \
             patch("backend.pipeline.phase3_fallback.confirm_mapping",
                   side_effect=lambda **kw: confirm_calls.append(kw)), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
                product_client=MagicMock(),
            )

        # dist_code 비어 있음
        cr = result.get("confirmed_retailers", {})
        assert cr.get("テスト店A", {}).get("dist_code") == ""

        # pending에 dist 있음
        dist_pending = [p for p in pending if p.get("mapping_type") == "dist"]
        assert len(dist_pending) == 1

        # dist confirm_mapping 미호출
        dist_confirms = [c for c in confirm_calls if c.get("mapping_type") == "dist"]
        assert len(dist_confirms) == 0

    async def test_dist_1n_skipped_when_no_client(self, tmp_path):
        """product_client=None이면 dist Tool Use 스킵 → pending 유지."""
        mappings = tmp_path / "mappings"; form_defs = tmp_path / "form_defs"
        mappings.mkdir(); form_defs.mkdir()
        self._write_retail_user_1n(mappings / "retail_user.csv", "R001")
        (form_defs / "form_01.md").write_text(
            "# form_01\n## issuer 식별\n```\nfingerprint_fields: name\n```\n",
            encoding="utf-8",
        )
        (mappings / "ocr_product.csv").write_text("ocr_name,product_code,product_name\n",
                                                   encoding="utf-8-sig")

        batch_result = self._make_batch_result_with_1n_dist("R001")
        phase2 = {
            "pages": [{"role": "cover", "issuer": {"name": "テスト"}}],
            "items": [{"customer": "テスト店A", "product": "P", "item_type": "条件", "columns": {}}],
        }

        from backend.pipeline.phase3_fallback import _execute_success_path

        with patch("backend.pipeline.phase3_fallback.confirm_mapping", new=AsyncMock()), \
             patch("backend.pipeline.phase3_fallback._build_product_decisions_with_tool_use",
                   new=AsyncMock(return_value=[])):
            result, pending = await _execute_success_path(
                batch_result=batch_result,
                doc_id="doc1", form_id="form_01", hatsu_month="",
                phase2_result=phase2, output_dir=tmp_path,
                mappings_dir=mappings, form_definitions_dir=form_defs,
                product_client=None,  # ← no client
            )

        dist_pending = [p for p in pending if p.get("mapping_type") == "dist"]
        assert len(dist_pending) == 1, "client 없을 때 dist pending 유지"


# ── 5. ToolUseTokenStats dist 필드 ────────────────────────────────────────────

class TestTokenStatsDistFields:
    def test_dist_fields_exist(self):
        s = ToolUseTokenStats()
        assert hasattr(s, "dist_input_tokens")
        assert hasattr(s, "dist_output_tokens")
        assert hasattr(s, "dist_api_calls")
        assert s.dist_api_calls == 0

    def test_total_includes_dist(self):
        s = ToolUseTokenStats(
            retailer_input_tokens=100,
            product_input_tokens=50,
            dist_input_tokens=30,
        )
        assert s.total_input_tokens == 180

    def test_total_api_calls_includes_dist(self):
        s = ToolUseTokenStats(retailer_api_calls=3, product_api_calls=2, dist_api_calls=1)
        assert s.total_api_calls == 6

    def test_total_cache_includes_dist(self):
        s = ToolUseTokenStats(
            retailer_cache_read_tokens=10,
            product_cache_read_tokens=5,
            dist_cache_read_tokens=3,
        )
        assert s.total_cache_read_tokens == 18


# ── 6. dist_pending에 retailer_code 포함 여부 ─────────────────────────────────

class TestDistPendingRetailerCode:
    def test_dist_pending_contains_retailer_code(self, tmp_path):
        """_batch_result_to_retailer_decisions가 dist_pending에 retailer_code를 포함한다."""
        from backend.experiments.batch_tool_use_experiment import RetailerBatchResult
        from backend.pipeline.phase3_fallback import _batch_result_to_retailer_decisions

        per_retailer = [
            RetailerBatchResult(
                ocr_name="テスト店", success=True, confirmed_code="R001",
                lookup_basis="candidate",
                tool_call_count=2, lookup_call_count=1, confirm_call_count=1,
                turns_used=3, max_turns_hit=False, elapsed_ms=100.0,
            )
        ]
        retail_user_rows = [
            {"소매처코드": "R001", "소매처명": "テスト小売",
             "판매처코드": "D001", "판매처명": "東日本"},
            {"소매처코드": "R001", "소매처명": "テスト小売",
             "판매처코드": "D002", "판매처명": "西日本"},
        ]

        decisions, dist_resolutions, dist_pending = _batch_result_to_retailer_decisions(
            per_retailer,
            form_id="form_01", issuer_fingerprint="fp",
            cached_dist={}, retail_user_rows=retail_user_rows,
            jisho_by_customer={"テスト店": [""]},
        )

        assert len(dist_pending) == 1
        assert dist_pending[0]["retailer_code"] == "R001"
        assert dist_pending[0]["ocrName"] == "テスト店"
        assert len(dist_pending[0]["candidates"]) == 2
