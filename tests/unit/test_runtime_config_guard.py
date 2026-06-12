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
