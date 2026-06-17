"""test_runtime_config_guard.py — 2026-06-12 재진단 수정분 회귀

대상:
  1. _is_rules_stale — 분석 시점 규칙 해시 vs 현재 해시 비교 (#4)
  2. inbox dedup — 파일명 기반 doc_id + 기존 문서 재처리 생략 (#2)
  3. run_form_sync 동시성 — form_types.json lost-update 방지 락 (#3)
  4. sync 실패 시 상태 기록 — 실패가 조용히 묻히지 않음 (#5)

실행: pytest tests/unit/test_runtime_config_guard.py -v
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from backend.api.routes.documents import _is_rules_stale, _make_doc_id  # noqa: E402
import backend.api.routes.forms as forms  # noqa: E402
import backend.core.inbox_poller as inbox  # noqa: E402


# ── 1. _is_rules_stale ───────────────────────────────────────────────────────

class TestIsRulesStale:
    def test_same_hashes_not_stale(self):
        h = {"form_definition_hash": "aaa", "form_types_hash": "bbb"}
        assert _is_rules_stale(h, dict(h)) is False

    def test_md_changed_is_stale(self):
        stored = {"form_definition_hash": "aaa", "form_types_hash": "bbb"}
        current = {"form_definition_hash": "CHANGED", "form_types_hash": "bbb"}
        assert _is_rules_stale(stored, current) is True

    def test_form_types_changed_is_stale(self):
        stored = {"form_definition_hash": "aaa", "form_types_hash": "bbb"}
        current = {"form_definition_hash": "aaa", "form_types_hash": "CHANGED"}
        assert _is_rules_stale(stored, current) is True

    def test_no_comparable_keys_returns_none(self):
        # 구버전 분석(해시 미기록) → 판단 불가 (오탐 방지)
        assert _is_rules_stale({}, {"form_definition_hash": "aaa"}) is None
        assert _is_rules_stale({"form_definition_hash": "aaa"}, {}) is None


# ── 2. inbox dedup ───────────────────────────────────────────────────────────

class TestInboxDedup:
    def _settings(self, tmp_path):
        s = MagicMock()
        s.samples_dir = tmp_path / "samples"
        return s

    async def test_existing_doc_skipped(self, tmp_path, monkeypatch):
        """동일 파일명 PDF 재유입(processed.json 유실 등) 시 재처리하지 않는다."""
        import backend.db.queries as queries
        monkeypatch.setattr(queries, "get_document",
                            AsyncMock(return_value={"status": "done"}))
        create = AsyncMock()
        monkeypatch.setattr(queries, "create_document", create)

        drive = MagicMock()
        await inbox._process_inbox_file(
            drive, "file123", "5月テスト商事.pdf", "202605", self._settings(tmp_path)
        )

        drive.download_file.assert_not_called()
        create.assert_not_called()

    async def test_new_doc_uses_filename_based_id(self, tmp_path, monkeypatch):
        """doc_id가 uuid가 아니라 파일명 기반이어야 한다 (1차 dedup 보장)."""
        import backend.db.queries as queries
        import backend.core.s3_store as s3
        monkeypatch.setattr(queries, "get_document", AsyncMock(return_value=None))
        create = AsyncMock()
        monkeypatch.setattr(queries, "create_document", create)
        monkeypatch.setattr(s3, "upload_file", MagicMock())
        monkeypatch.setattr(inbox, "_trigger_pipeline", MagicMock())

        drive = MagicMock()
        await inbox._process_inbox_file(
            drive, "file123", "5月テスト商事.pdf", "202605", self._settings(tmp_path)
        )

        expected = _make_doc_id("5月テスト商事")
        assert create.call_args.kwargs["doc_id"] == expected
        drive.download_file.assert_called_once()


# ── 3 & 4. run_form_sync — 동시성 락 + 실패 상태 기록 ────────────────────────

def _make_sync_env(tmp_path, monkeypatch, responses: dict[str, str]):
    """run_form_sync 실행 환경 구성.

    responses: form_id → Claude가 반환할 JSON 문자열
    """
    workspace = tmp_path
    (workspace / "config").mkdir()
    form_defs = workspace / "form_definitions"
    form_defs.mkdir()
    for form_id in responses:
        (form_defs / f"{form_id}.md").write_text(f"# {form_id} 정의", encoding="utf-8")

    settings = MagicMock()
    settings.workspace_root = workspace
    settings.form_definitions_dir = form_defs
    settings.anthropic_api_key = "test-key"
    monkeypatch.setattr(forms, "get_settings", lambda: settings)

    def _create(model, max_tokens, messages):
        prompt = messages[0]["content"]
        for form_id, payload in responses.items():
            if f"{form_id}.md" in prompt:
                block = MagicMock()
                block.text = payload
                resp = MagicMock()
                resp.content = [block]
                return resp
        raise AssertionError("프롬프트에서 form_id를 찾지 못함")

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.messages.create = _create
    monkeypatch.setattr(forms, "_anthropic", fake_anthropic)

    # S3 미러·상태 기록은 외부 의존 — 기록 내용만 캡처
    mirrors: list[tuple] = []
    statuses: list[tuple] = []
    monkeypatch.setattr(forms, "_mirror_to_s3", lambda k, t: mirrors.append((k, t)))
    monkeypatch.setattr(forms, "_update_sync_status", lambda f, e: statuses.append((f, e)))
    return workspace, mirrors, statuses


class TestFormSyncConcurrency:
    async def test_concurrent_syncs_preserve_both_entries(self, tmp_path, monkeypatch):
        """동시 sync 시 늦게 끝난 쪽이 먼저 끝난 쪽의 항목을 덮어쓰지 않는다."""
        workspace, mirrors, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "A양식"}),
            "form_b": json.dumps({"label": "B양식"}),
        })

        await asyncio.gather(
            forms.run_form_sync("form_a"),
            forms.run_form_sync("form_b"),
        )

        result = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert result.get("form_a") == {"label": "A양식"}
        assert result.get("form_b") == {"label": "B양식"}
        # 성공 시 S3 미러도 기록됨 (#1a)
        assert any(k == "config/form_types.json" for k, _ in mirrors)

    async def test_formula_change_flagged(self, tmp_path, monkeypatch):
        """net 수식 변경 시 formula_changed=True (현업 검산 경고)."""
        _, _, statuses = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"net": {"expr": "shikiri - c1"}}),
        })
        result = await forms.run_form_sync("form_a")
        assert result["formula_changed"] is True
        assert statuses and statuses[-1][1]["formula_changed"] is True

    async def test_sync_failure_recorded_in_status(self, tmp_path, monkeypatch):
        """Claude 응답 파싱 실패 → 예외 + 상태에 ok=False 기록 (조용한 실패 금지)."""
        _, _, statuses = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": "이건 JSON이 아님",
        })
        with pytest.raises(ValueError):
            await forms.run_form_sync("form_a")
        assert statuses, "실패 시 sync status가 기록되지 않음"
        form_id, entry = statuses[-1]
        assert form_id == "form_a"
        assert entry["ok"] is False
        assert entry["error"]


# ── 4. Literate config — 블록 우선(결정적), Claude 폴백 ───────────────────────

class TestLiterateConfigBlockFirst:
    async def test_block_form_skips_claude(self, tmp_path, monkeypatch):
        """[config] 정본 블록이 있는 양식은 결정적 추출 — Claude를 호출하지 않는다 (드리프트 불가)."""
        workspace, mirrors, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "폴백이면 이 값(틀려야 함)"}),
        })
        block = json.dumps({"label": "블록정본"}, ensure_ascii=False, indent=2)
        (workspace / "form_definitions" / "form_a.md").write_text(
            f"# form_a 정의\n\n## [config] 실행 설정\n\n```json\n{block}\n```\n",
            encoding="utf-8",
        )
        # 블록 양식인데 Claude가 호출되면 즉시 실패
        def _boom(*a, **k):
            raise AssertionError("블록 양식인데 Claude(LLM) 파싱 경로가 호출됨")
        monkeypatch.setattr(forms._anthropic.Anthropic.return_value.messages, "create", _boom)

        result = await forms.run_form_sync("form_a")

        assert result["ok"] is True
        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"] == {"label": "블록정본"}, "블록이 아니라 폴백 값이 저장됨"

    async def test_blockless_form_uses_claude_fallback(self, tmp_path, monkeypatch):
        """[config] 블록 없는 (미마이그레이션) 양식은 기존 Claude 파싱 폴백을 그대로 탄다."""
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "폴백값"}),
        })
        result = await forms.run_form_sync("form_a")
        assert result["ok"] is True
        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"] == {"label": "폴백값"}


# ── 5. 자동 [config] 블록 — UI 동기화가 산문→블록 생성 (개발자 손 안 타게) ────────

def _count_claude_calls(monkeypatch):
    """현재 mock된 Claude create 호출 수를 세는 카운터를 끼운다. (호출 리스트 반환)"""
    calls = []
    orig = forms._anthropic.Anthropic.return_value.messages.create
    def _wrapped(*a, **k):
        calls.append(1)
        return orig(*a, **k)
    monkeypatch.setattr(forms._anthropic.Anthropic.return_value.messages, "create", _wrapped)
    return calls


class TestAutoConfigBlock:
    async def test_first_sync_writes_auto_block(self, tmp_path, monkeypatch):
        """블록 없는 신규 양식: 첫 동기화가 산문을 파싱해 [config] 자동 블록을 MD에 기록한다."""
        workspace, mirrors, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "A양식"}),
        })
        md_path = workspace / "form_definitions" / "form_a.md"
        md_path.write_text("# form_a 정의\n\n## 식별 패턴\nABC\n", encoding="utf-8")

        await forms.run_form_sync("form_a")

        md = md_path.read_text(encoding="utf-8")
        assert "## [config]" in md and "config-block: auto" in md, "자동 블록이 MD에 기록되지 않음"
        from scripts.build_form_types import extract_config_block
        assert extract_config_block(md, "form_a.md") == {"label": "A양식"}
        # MD도 S3 미러됨 (개발자 파일편집 없이 EC2 상태로 보존)
        assert any("form_definitions/form_a.md" in k for k, _ in mirrors)

    async def test_auto_block_reused_when_prose_unchanged(self, tmp_path, monkeypatch):
        """자동 블록 생성 후 산문이 그대로면 재동기화는 Claude를 다시 부르지 않는다 (결정적)."""
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "A양식"}),
        })
        md_path = workspace / "form_definitions" / "form_a.md"
        md_path.write_text("# form_a 정의\n\n## 식별 패턴\nABC\n", encoding="utf-8")
        await forms.run_form_sync("form_a")  # 1차: 자동 블록 생성

        calls = _count_claude_calls(monkeypatch)
        await forms.run_form_sync("form_a")  # 2차: 산문 무변경
        assert calls == [], "산문이 그대로인데 Claude가 다시 호출됨 (block-first 실패)"

    async def test_auto_block_regenerated_on_prose_change(self, tmp_path, monkeypatch):
        """자동 블록 양식의 산문이 바뀌면 재동기화가 블록을 재생성한다 (현업은 산문만 만지면 됨)."""
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "구버전"}),
        })
        md_path = workspace / "form_definitions" / "form_a.md"
        md_path.write_text("# form_a 정의\n\n## 식별 패턴\nABC\n", encoding="utf-8")
        await forms.run_form_sync("form_a")

        # 산문 변경 + Claude가 새 값 반환하도록 교체
        md = md_path.read_text(encoding="utf-8")
        md_path.write_text(md.replace("ABC", "ABC 변경됨"), encoding="utf-8")
        def _new(model, max_tokens, messages):
            block = MagicMock(); block.text = json.dumps({"label": "신버전"})
            resp = MagicMock(); resp.content = [block]; return resp
        monkeypatch.setattr(forms._anthropic.Anthropic.return_value.messages, "create", _new)

        await forms.run_form_sync("form_a")
        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"] == {"label": "신버전"}, "산문 변경이 반영되지 않음"

    async def test_sync_output_key_order_matches_build_sort(self, tmp_path, monkeypatch):
        """동기화가 새 양식을 추가해도 키 순서가 form_id 정렬이라 build --check와 어긋나지 않는다.

        (런타임이 끝에 append하고 build는 파일명 정렬이면 build --check가 깨졌던 버그 회귀.)
        """
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_04": json.dumps({"label": "넷째"}),
        })
        # 기존 json은 form_01만 (정렬상 form_04는 뒤). 동기화로 form_04 추가.
        (workspace / "config" / "form_types.json").write_text(
            json.dumps({"form_01": {"label": "첫째"}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (workspace / "form_definitions" / "form_04.md").write_text("# form_04\n\nABC\n", encoding="utf-8")

        await forms.run_form_sync("form_04")

        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert list(written.keys()) == sorted(written.keys()), \
            f"키가 form_id 정렬이 아님: {list(written.keys())} — build --check와 어긋남"

    async def test_hand_block_not_clobbered_on_prose_change(self, tmp_path, monkeypatch):
        """손으로 만든 정본 블록(auto 마커 없음)은 산문이 바뀌어도 산문에서 재파싱하지 않는다."""
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {
            "form_a": json.dumps({"label": "폴백이면 틀림"}),
        })
        block = json.dumps({"label": "손정본", "net": {"expr": "shikiri - c1"}}, ensure_ascii=False, indent=2)
        md_path = workspace / "form_definitions" / "form_a.md"
        md_path.write_text(
            f"# form_a 정의\n\n## 식별 패턴\nDIFFERENT\n\n## [config] 실행 설정\n\n```json\n{block}\n```\n",
            encoding="utf-8",
        )
        calls = _count_claude_calls(monkeypatch)
        await forms.run_form_sync("form_a")
        assert calls == [], "손 블록인데 산문에서 재파싱(Claude 호출)됨 — 개발자 튜닝 유실 위험"
        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"]["label"] == "손정본"


# ── 6. post-sync 와이어링 훅 (동기화 직후 자동 gap 점검) ──────────────────────

class TestPostSyncWiringHook:
    def test_hook_skips_on_tmp_workspace(self, tmp_path, monkeypatch):
        """워크스페이스가 스크립트 기준과 다르면(테스트 등) 실파일 오염 방지로 생략한다."""
        settings = MagicMock()
        settings.workspace_root = tmp_path
        settings.form_definitions_dir = tmp_path
        monkeypatch.setattr(forms, "get_settings", lambda: settings)
        res = forms._run_wiring_check("form_04")
        assert res["available"] is False

    def test_hook_runs_on_real_workspace_nondestructive(self, monkeypatch):
        """실 워크스페이스에서 form_04(등록·정합 완료)를 점검 — 비파괴, 구조 반환."""
        import scripts.verify_form_wiring as vfw
        base = vfw.BASE
        settings = MagicMock()
        settings.workspace_root = base
        settings.form_definitions_dir = base / "form_definitions"
        monkeypatch.setattr(forms, "get_settings", lambda: settings)
        # form_04는 이미 등록·정합 → safe-fix 없음 → 미러 호출 없음. 호출되면 무해 처리.
        monkeypatch.setattr(forms, "_mirror_to_s3", lambda k, t: None)
        res = forms._run_wiring_check("form_04")
        assert res["available"] is True
        assert isinstance(res["owner"], list) and isinstance(res["dev"], list)
        assert res["safe_fixed"] == [], "등록·정합 양식인데 파일을 수정함(비파괴 위반)"
