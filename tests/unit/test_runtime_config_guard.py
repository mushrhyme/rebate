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


# ── 3 & 4. run_form_sync — 정본-only(블록 빌드) + 동시성 락 + 실패 상태 기록 ──────

def _md_with_block(form_id: str, block_obj: dict) -> str:
    """[config] 정본 블록을 가진 form_XX.md 텍스트."""
    block = json.dumps(block_obj, ensure_ascii=False, indent=2)
    return (
        f"# {form_id} 정의\n\n## 식별 패턴\nABC\n\n"
        f"## [config] 실행 설정 (정본 · build_form_types.py가 읽음)\n\n"
        f"```json\n{block}\n```\n"
    )


def _make_sync_env(tmp_path, monkeypatch, blocks: dict):
    """run_form_sync 실행 환경 구성 (정본-only 모델).

    blocks: form_id → 블록 dict(정본) 또는 None(블록 없음=blockless).
    sync는 [config] 블록을 form_types.json으로 빌드만 한다(LLM 없음). 블록 없으면 시끄럽게 실패.
    """
    workspace = tmp_path
    (workspace / "config").mkdir()
    form_defs = workspace / "form_definitions"
    form_defs.mkdir()
    for form_id, block in blocks.items():
        text = f"# {form_id} 정의\n" if block is None else _md_with_block(form_id, block)
        (form_defs / f"{form_id}.md").write_text(text, encoding="utf-8")

    settings = MagicMock()
    settings.workspace_root = workspace
    settings.form_definitions_dir = form_defs
    settings.anthropic_api_key = "test-key"
    monkeypatch.setattr(forms, "get_settings", lambda: settings)

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
            "form_a": {"label": "A양식"},
            "form_b": {"label": "B양식"},
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
            "form_a": {"net": {"formula_type": "expr", "expr": "shikiri - c1", "vars": {"c1": "条件"}}},
        })
        result = await forms.run_form_sync("form_a")
        assert result["formula_changed"] is True
        assert statuses and statuses[-1][1]["formula_changed"] is True

    async def test_blockless_sync_fails_loudly_and_records_status(self, tmp_path, monkeypatch):
        """정본-only: [config] 블록 없는 양식은 시끄럽게 실패 + 상태에 ok=False 기록 (무음 no-op 금지)."""
        _, _, statuses = _make_sync_env(tmp_path, monkeypatch, {"form_a": None})
        with pytest.raises(ValueError, match="정본 블록"):
            await forms.run_form_sync("form_a")
        assert statuses, "실패 시 sync status가 기록되지 않음"
        form_id, entry = statuses[-1]
        assert form_id == "form_a"
        assert entry["ok"] is False
        assert entry["error"]


# ── 4. Literate config — 정본-only(블록이 유일한 진실 소스, LLM 추론 없음) ──────────

class TestLiterateConfigBlockFirst:
    async def test_block_form_builds_deterministically(self, tmp_path, monkeypatch):
        """[config] 정본 블록을 그대로 form_types.json으로 빌드한다 (LLM 없음, 드리프트 불가)."""
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {"form_a": {"label": "블록정본"}})
        result = await forms.run_form_sync("form_a")
        assert result["ok"] is True
        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"] == {"label": "블록정본"}

    async def test_blockless_form_raises_loudly(self, tmp_path, monkeypatch):
        """[config] 블록 없는 양식 → 산문 파싱 fallback 없이 시끄럽게 실패(정본-only).

        신규 양식의 첫 블록은 cold-start/create가 골격으로 부착, 규칙은 '규칙 반영'으로 채운다.
        """
        _make_sync_env(tmp_path, monkeypatch, {"form_a": None})
        with pytest.raises(ValueError, match="정본 블록"):
            await forms.run_form_sync("form_a")


# ── 5. 정본-only: 블록이 정본, 산문은 근거 (드리프트 불가) ─────────────────────────

class TestBlockFirstUnified:
    async def test_block_first_ignores_prose_change(self, tmp_path, monkeypatch):
        """블록이 정본 — 산문(근거)을 고쳐도 form_types.json은 블록 값 그대로(드리프트 불가).

        규칙 변경은 채팅→블록(apply_block_update) 경로로 한다 — 산문→구조 재파싱은 존재하지 않는다.
        """
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {"form_a": {"label": "블록값"}})
        await forms.run_form_sync("form_a")  # 1차

        md_path = workspace / "form_definitions" / "form_a.md"
        md = md_path.read_text(encoding="utf-8")
        md_path.write_text(md.replace("ABC", "ABC 변경됨"), encoding="utf-8")  # 산문만 변경
        await forms.run_form_sync("form_a")  # 2차

        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert written["form_a"] == {"label": "블록값"}, "산문 변경이 form_types.json에 샜다(block-first 위반)"

    async def test_sync_output_key_order_matches_build_sort(self, tmp_path, monkeypatch):
        """동기화가 새 양식을 추가해도 키 순서가 form_id 정렬이라 build --check와 어긋나지 않는다.

        (런타임이 끝에 append하고 build는 파일명 정렬이면 build --check가 깨졌던 버그 회귀.)
        """
        workspace, _, _ = _make_sync_env(tmp_path, monkeypatch, {"form_04": {"label": "넷째"}})
        # 기존 json은 form_01만 (정렬상 form_04는 뒤). 동기화로 form_04 추가.
        (workspace / "config" / "form_types.json").write_text(
            json.dumps({"form_01": {"label": "첫째"}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await forms.run_form_sync("form_04")

        written = json.loads((workspace / "config" / "form_types.json").read_text(encoding="utf-8"))
        assert list(written.keys()) == sorted(written.keys()), \
            f"키가 form_id 정렬이 아님: {list(written.keys())} — build --check와 어긋남"


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


# ── 7. config 드리프트 가드 — form_types.json ≠ [config] 블록 차단 ──────────────

class TestConfigDriftGuard:
    """form_XX.md [config] 블록을 고쳤는데 form_types.json을 재빌드하지 않으면,
    엔진이 옛 json으로 '내가 고친 규칙과 다르게' 조용히 계산한다. 이 가드가 분석 전에 차단한다.
    """

    def _env(self, tmp_path, md_block: dict, json_entry):
        """form_03.md [config] 블록 = md_block, form_types.json[form_03] = json_entry 인 워크스페이스."""
        from unittest.mock import MagicMock
        (tmp_path / "config").mkdir()
        fd = tmp_path / "form_definitions"
        fd.mkdir()
        block = json.dumps(md_block, ensure_ascii=False, indent=2)
        (fd / "form_03.md").write_text(
            f"# form_03 정의\n\n## [config] 실행 설정\n\n```json\n{block}\n```\n",
            encoding="utf-8",
        )
        (tmp_path / "config" / "form_types.json").write_text(
            json.dumps({"form_03": json_entry}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        settings = MagicMock()
        settings.workspace_root = tmp_path
        settings.form_definitions_dir = fd
        return settings

    def test_in_sync_returns_none(self, tmp_path):
        from backend.pipeline.orchestrator import _check_config_drift
        settings = self._env(tmp_path, {"label": "X", "net": {"formula_type": "expr", "expr": "a-b"}},
                             {"label": "X", "net": {"formula_type": "expr", "expr": "a-b"}})
        assert _check_config_drift("form_03", settings) is None

    def test_drift_returns_message(self, tmp_path):
        """블록은 새 수식, json은 옛 수식 → 사람용 메시지 반환(차단 신호)."""
        from backend.pipeline.orchestrator import _check_config_drift
        settings = self._env(tmp_path, {"net": {"formula_type": "expr", "expr": "a - b - c"}},
                             {"net": {"formula_type": "expr", "expr": "a - b"}})
        msg = _check_config_drift("form_03", settings)
        assert msg and "재빌드" in msg and "form_03" in msg

    def test_key_order_insensitive(self, tmp_path):
        """키 순서만 다른 동일 내용은 드리프트가 아니다(오탐 방지)."""
        from backend.pipeline.orchestrator import _check_config_drift
        settings = self._env(tmp_path, {"a": 1, "b": 2}, {"b": 2, "a": 1})
        assert _check_config_drift("form_03", settings) is None

    def test_blockless_returns_none(self, tmp_path):
        """[config] 블록 없는 양식은 판단 불가 → None (sync 단계가 따로 막음)."""
        from unittest.mock import MagicMock
        from backend.pipeline.orchestrator import _check_config_drift
        (tmp_path / "config").mkdir()
        fd = tmp_path / "form_definitions"
        fd.mkdir()
        (fd / "form_03.md").write_text("# form_03\n블록 없음\n", encoding="utf-8")
        (tmp_path / "config" / "form_types.json").write_text("{}", encoding="utf-8")
        settings = MagicMock()
        settings.workspace_root = tmp_path
        settings.form_definitions_dir = fd
        assert _check_config_drift("form_03", settings) is None
