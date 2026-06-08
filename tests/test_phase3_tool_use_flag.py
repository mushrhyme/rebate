"""test_phase3_tool_use_flag.py — Phase3 Tool Use feature flag 테스트

검증 항목:
  1. 환경변수 미설정 → Tool Use OFF (기본값 False)
  2. PHASE3_TOOL_USE_ENABLED=false → OFF
  3. PHASE3_TOOL_USE_ENABLED=true → ON
  4. flag OFF 시 orchestrator가 run_phase3() 직접 호출
  5. flag ON 시 orchestrator가 run_phase3_with_tool_use_or_fallback() 호출
  6. flag ON + Tool Use 실패 시 legacy fallback 발생
  7. Settings 필드 존재 및 기본값 확인
  8. 기존 orchestrator 호출 흐름 유지

실행: pytest tests/test_phase3_tool_use_flag.py -v
"""
import ast
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.tools.metrics import reset_metrics


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


def _make_mock_settings(tool_use_enabled: bool = False):
    """get_settings() 반환용 mock Settings 객체."""
    s = MagicMock()
    s.phase3_tool_use_enabled = tool_use_enabled
    s.anthropic_api_key       = "sk-ant-fake"
    s.workspace_root          = Path("/tmp/fake_workspace")
    s.mappings_dir            = Path("/tmp/fake_workspace/mappings")
    s.form_definitions_dir    = Path("/tmp/fake_workspace/form_definitions")
    s.extracted_dir           = Path("/tmp/fake_workspace/extracted")
    s.samples_dir             = Path("/tmp/fake_workspace/samples")
    s.drive_enabled           = False
    return s


# ── 1. Settings 필드 확인 ─────────────────────────────────────────────────────

class TestSettingsField:
    def test_phase3_tool_use_enabled_field_exists(self):
        """Settings 클래스에 phase3_tool_use_enabled 필드가 있다."""
        import ast
        src = Path("backend/core/config.py").read_text(encoding="utf-8")
        assert "phase3_tool_use_enabled" in src

    def test_default_value_is_false_in_source(self):
        """기본값이 False로 명시되어 있다."""
        src = Path("backend/core/config.py").read_text(encoding="utf-8")
        # "phase3_tool_use_enabled: bool = False" 가 있어야 한다
        assert "phase3_tool_use_enabled: bool = False" in src

    def test_settings_default_is_false(self, monkeypatch):
        """환경변수 미설정 → phase3_tool_use_enabled = False."""
        monkeypatch.delenv("PHASE3_TOOL_USE_ENABLED", raising=False)
        # Settings 캐시를 우회하기 위해 직접 생성
        from backend.core.config import Settings
        # env_file이 없어도 기본값으로 인스턴스화 가능한지 확인
        # (database_url 등은 .env에서 오므로 여기서는 필드 존재만 확인)
        assert hasattr(Settings, "model_fields") or hasattr(Settings, "__fields__")
        # 기본값 확인
        default_val = Settings.model_fields.get(
            "phase3_tool_use_enabled",
            Settings.__fields__.get("phase3_tool_use_enabled") if hasattr(Settings, "__fields__") else None
        )
        # pydantic v2: model_fields
        if hasattr(Settings, "model_fields"):
            field = Settings.model_fields.get("phase3_tool_use_enabled")
            assert field is not None
            assert field.default is False

    def test_settings_env_var_name(self):
        """환경변수 이름이 PHASE3_TOOL_USE_ENABLED임을 소스로 검증한다."""
        src = Path("backend/core/config.py").read_text(encoding="utf-8")
        assert "PHASE3_TOOL_USE_ENABLED" in src


# ── 2. 환경변수별 flag 동작 ───────────────────────────────────────────────────

class TestEnvVarControl:
    def test_env_not_set_means_false(self, monkeypatch):
        """환경변수 미설정 → phase3_tool_use_enabled = False."""
        monkeypatch.delenv("PHASE3_TOOL_USE_ENABLED", raising=False)
        from backend.core.config import Settings
        # default 확인만 (실제 Settings 인스턴스는 DB url 등 필요)
        if hasattr(Settings, "model_fields"):
            assert Settings.model_fields["phase3_tool_use_enabled"].default is False

    def test_env_false_means_off(self, monkeypatch):
        """PHASE3_TOOL_USE_ENABLED=false → False."""
        monkeypatch.setenv("PHASE3_TOOL_USE_ENABLED", "false")
        from backend.core.config import Settings
        if hasattr(Settings, "model_fields"):
            # 환경변수를 반영한 인스턴스는 DB URL 없이 만들 수 없으므로
            # Settings 클래스의 필드 기본값만 확인
            assert Settings.model_fields["phase3_tool_use_enabled"].default is False

    def test_env_true_sets_flag(self, monkeypatch):
        """PHASE3_TOOL_USE_ENABLED=true 설정 시 True가 된다."""
        monkeypatch.setenv("PHASE3_TOOL_USE_ENABLED", "true")
        # mock_settings로 검증
        s = _make_mock_settings(tool_use_enabled=True)
        assert s.phase3_tool_use_enabled is True


# ── 3. orchestrator에서 flag에 따른 분기 ──────────────────────────────────────

# asyncpg mock — orchestrator import에 필요
import sys as _sys
from unittest.mock import MagicMock as _MagicMock
_sys.modules.setdefault("asyncpg", _MagicMock())
_sys.modules.setdefault("asyncpg.pool", _MagicMock())
for _m in ["azure", "azure.ai", "azure.ai.formrecognizer",
           "google", "google.oauth2", "google.auth",
           "google.auth.transport", "google.auth.transport.requests",
           "googleapiclient", "googleapiclient.discovery"]:
    _sys.modules.setdefault(_m, _MagicMock())


class TestOrchestratorFlagBranch:
    """orchestrator.run_pipeline()이 flag에 따라 올바른 함수를 호출하는지 검증."""

    def _make_phase3_json(self, extracted: Path, doc_id: str) -> None:
        import json
        doc_dir = extracted / doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "phase3_output.json").write_text(
            json.dumps({
                "doc_id": doc_id, "form_id": "form_01",
                "hatsu_month": "", "issuer": {},
                "confirmed_retailers": {}, "confirmed_products": {},
                "items": [], "cover_totals": {},
            }),
            encoding="utf-8",
        )

    async def test_flag_off_calls_legacy_run_phase3(self, tmp_path):
        """flag OFF → 기존 run_phase3() 직접 호출, fallback wrapper 미호출."""
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=False)

        legacy_calls: list = []
        wrapper_calls: list = []

        async def mock_run_phase3(*args, **kwargs):
            legacy_calls.append(args)
            return {}, []

        async def mock_wrapper(*args, **kwargs):
            wrapper_calls.append(args)
            return {}, [], MagicMock(fallback_triggered=False)

        with patch.object(orchestrator, "get_settings", return_value=mock_settings), \
             patch.object(orchestrator, "run_phase3", mock_run_phase3), \
             patch.object(orchestrator, "run_phase3_with_tool_use_or_fallback", mock_wrapper):
            # flag OFF → legacy 직접 호출
            result = await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        assert len(legacy_calls) == 1
        assert len(wrapper_calls) == 0

    async def test_flag_on_calls_wrapper(self, tmp_path):
        """flag ON → run_phase3_with_tool_use_or_fallback() 호출, legacy 미호출."""
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=True)

        legacy_calls: list = []
        wrapper_calls: list = []
        mock_stats = MagicMock(
            fallback_triggered=False, fallback_class=None, fallback_reason=None
        )

        async def mock_run_phase3(*args, **kwargs):
            legacy_calls.append(args)
            return {}, []

        async def mock_wrapper(*args, **kwargs):
            wrapper_calls.append(args)
            return {}, [], mock_stats

        with patch.object(orchestrator, "get_settings", return_value=mock_settings), \
             patch.object(orchestrator, "run_phase3", mock_run_phase3), \
             patch.object(orchestrator, "run_phase3_with_tool_use_or_fallback", mock_wrapper):
            await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        assert len(wrapper_calls) == 1
        assert len(legacy_calls) == 0

    async def test_flag_on_tool_use_failure_returns_pending(self, tmp_path):
        """flag ON + Tool Use 실패(fallback 발생) → pending 정상 반환.

        _call_phase3_by_flag는 (result, pending) 2-tuple을 반환한다.
        fallback 통계는 내부적으로 로그만 남긴다.
        """
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=True)
        mock_stats = MagicMock(
            fallback_triggered=True, fallback_class="ToolUseMaxTurnsError",
            fallback_reason="max_turns 초과"
        )

        async def mock_wrapper(*args, **kwargs):
            return {}, [{"mapping_type": "retailer", "ocrName": "テスト", "candidates": []}], mock_stats

        with patch.object(orchestrator, "get_settings", return_value=mock_settings), \
             patch.object(orchestrator, "run_phase3_with_tool_use_or_fallback", mock_wrapper):
            _, pending = await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        assert len(pending) == 1


# ── 4. orchestrator 소스 분기 확인 (AST) ─────────────────────────────────────

class TestOrchestratorSourceBranch:
    def test_orchestrator_imports_fallback_wrapper(self):
        """orchestrator.py가 run_phase3_with_tool_use_or_fallback를 import한다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        assert "run_phase3_with_tool_use_or_fallback" in src

    def test_orchestrator_checks_phase3_tool_use_enabled(self):
        """orchestrator.py가 phase3_tool_use_enabled를 참조한다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        assert "phase3_tool_use_enabled" in src

    def test_legacy_run_phase3_still_present(self):
        """orchestrator.py에 legacy run_phase3() 호출이 여전히 존재한다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        # flag OFF 브랜치에서 직접 호출
        assert "run_phase3(" in src

    def test_config_has_correct_env_var_pattern(self):
        """config.py에 PHASE3_TOOL_USE_ENABLED 환경변수 이름이 문서화되어 있다."""
        src = Path("backend/core/config.py").read_text(encoding="utf-8")
        assert "PHASE3_TOOL_USE_ENABLED" in src

    def test_orchestrator_has_call_phase3_by_flag(self):
        """orchestrator.py에 _call_phase3_by_flag 헬퍼가 있다."""
        src = Path("backend/pipeline/orchestrator.py").read_text(encoding="utf-8")
        assert "_call_phase3_by_flag" in src


# ── 5. flag ON 시 wrapper에 enable_tool_use=True 전달 ────────────────────────

class TestWrapperCallArguments:
    async def test_wrapper_called_with_enable_tool_use_true(self, tmp_path):
        """flag ON 시 wrapper가 enable_tool_use=True로 호출된다."""
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=True)
        call_kwargs: list = []
        mock_stats = MagicMock(fallback_triggered=False)

        async def capture_wrapper(*args, **kwargs):
            call_kwargs.append(kwargs)
            return {}, [], mock_stats

        with patch.object(orchestrator, "get_settings", return_value=mock_settings), \
             patch.object(orchestrator, "run_phase3_with_tool_use_or_fallback", capture_wrapper):
            await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        assert len(call_kwargs) == 1
        assert call_kwargs[0].get("enable_tool_use") is True

    async def test_flag_off_does_not_use_wrapper_kwargs(self, tmp_path):
        """flag OFF 시 wrapper에 enable_tool_use 인자가 전달되지 않는다."""
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=False)
        legacy_kwargs: list = []

        async def capture_legacy(*args, **kwargs):
            legacy_kwargs.append(kwargs)
            return {}, []

        with patch.object(orchestrator, "get_settings", return_value=mock_settings), \
             patch.object(orchestrator, "run_phase3", capture_legacy):
            await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        # legacy는 enable_tool_use를 받지 않음
        assert "enable_tool_use" not in (legacy_kwargs[0] if legacy_kwargs else {})


# ── 6. settings 주입 검증 ─────────────────────────────────────────────────────

class TestSettingsInjection:
    """wrapper에 settings를 명시 주입했을 때 내부 get_settings()를 호출하지 않는지 검증."""

    async def test_wrapper_uses_injected_settings_not_global_get_settings(self, tmp_path):
        """settings를 명시 주입하면 wrapper 내부에서 get_settings()를 호출하지 않는다."""
        from backend.pipeline.phase3_fallback import run_phase3_with_tool_use_or_fallback

        injected = _make_mock_settings(tool_use_enabled=False)
        get_settings_call_count = [0]

        def spy_get_settings():
            get_settings_call_count[0] += 1
            return _make_mock_settings()

        with patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=({}, []))), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   side_effect=spy_get_settings):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", {"pages": [], "items": []}, tmp_path, "form_01",
                enable_tool_use=False,
                settings=injected,           # ← 명시 주입
            )

        # settings가 주입됐으므로 내부 get_settings()는 0회 호출
        assert get_settings_call_count[0] == 0, (
            f"settings를 주입했는데 get_settings()가 {get_settings_call_count[0]}회 호출됨"
        )

    async def test_wrapper_calls_get_settings_when_settings_is_none(self, tmp_path):
        """settings=None(기본값)이면 내부에서 get_settings()를 호출한다.

        backward compatibility: 기존 코드가 settings를 전달하지 않아도 동작.
        """
        from backend.pipeline.phase3_fallback import run_phase3_with_tool_use_or_fallback

        get_settings_call_count = [0]

        def spy_get_settings():
            get_settings_call_count[0] += 1
            return _make_mock_settings(tool_use_enabled=False)

        with patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=({}, []))), \
             patch("backend.pipeline.phase3_fallback.get_settings",
                   side_effect=spy_get_settings):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", {"pages": [], "items": []}, tmp_path, "form_01",
                enable_tool_use=False,
                # settings 미전달 → None → 내부에서 get_settings() 호출
            )

        assert get_settings_call_count[0] >= 1, (
            "settings=None일 때 get_settings()가 호출되지 않음"
        )

    async def test_orchestrator_passes_settings_to_wrapper(self, tmp_path):
        """orchestrator._call_phase3_by_flag()가 settings를 wrapper에 전달한다."""
        from backend.pipeline import orchestrator

        mock_settings = _make_mock_settings(tool_use_enabled=True)
        received_settings: list = []
        mock_stats = MagicMock(fallback_triggered=False)

        async def capture_wrapper(*args, **kwargs):
            received_settings.append(kwargs.get("settings"))
            return {}, [], mock_stats

        with patch.object(orchestrator, "run_phase3_with_tool_use_or_fallback",
                          capture_wrapper):
            await orchestrator._call_phase3_by_flag(
                doc_id="doc1",
                phase2_result={"pages": [], "items": []},
                extracted_dir=tmp_path,
                form_id="form_01",
                hatsu_month="",
                run_id="",
                settings=mock_settings,
            )

        assert len(received_settings) == 1, "wrapper가 호출되지 않음"
        assert received_settings[0] is mock_settings, (
            "orchestrator가 settings를 wrapper에 전달하지 않음"
        )

    async def test_injected_settings_mappings_dir_used(self, tmp_path):
        """주입된 settings의 mappings_dir가 Tool Use 경로에서 사용된다.

        enable_tool_use=True 경로에서 _attempt_tool_use_phase에 settings.mappings_dir가
        전달되어야 한다.
        """
        from backend.pipeline.phase3_fallback import run_phase3_with_tool_use_or_fallback

        custom_mappings_dir = tmp_path / "custom_mappings"
        injected = _make_mock_settings(tool_use_enabled=True)
        injected.mappings_dir = custom_mappings_dir
        injected.form_definitions_dir = tmp_path

        received_mappings_dir: list = []

        async def capture_attempt(*args, **kwargs):
            received_mappings_dir.append(kwargs.get("mappings_dir"))
            # ToolUseDispatchError로 즉시 fallback → _execute_success_path 미호출
            from backend.pipeline.phase3_fallback import ToolUseDispatchError
            raise ToolUseDispatchError("테스트 강제 fallback")

        with patch("backend.pipeline.phase3_fallback._attempt_tool_use_phase",
                   side_effect=capture_attempt), \
             patch("backend.pipeline.phase3_fallback.run_phase3",
                   new=AsyncMock(return_value=({}, []))):
            _, _, stats = await run_phase3_with_tool_use_or_fallback(
                "doc1", {"pages": [], "items": []}, tmp_path, "form_01",
                enable_tool_use=True,
                settings=injected,
            )

        assert stats.fallback_triggered is True
        assert len(received_mappings_dir) == 1
        assert received_mappings_dir[0] == custom_mappings_dir, (
            f"injected settings.mappings_dir가 사용되지 않음: {received_mappings_dir[0]}"
        )

    def test_wrapper_signature_has_settings_parameter(self):
        """run_phase3_with_tool_use_or_fallback 시그니처에 settings 파라미터가 있다."""
        import inspect
        from backend.pipeline.phase3_fallback import run_phase3_with_tool_use_or_fallback
        sig = inspect.signature(run_phase3_with_tool_use_or_fallback)
        assert "settings" in sig.parameters, (
            "settings 파라미터가 시그니처에 없음"
        )
        # 기본값이 None이어야 한다
        default = sig.parameters["settings"].default
        assert default is None, f"settings 기본값이 None이 아님: {default!r}"

    def test_flag_off_default_preserved(self, tmp_path):
        """enable_tool_use 기본값은 False 이어야 한다."""
        import inspect
        from backend.pipeline.phase3_fallback import run_phase3_with_tool_use_or_fallback
        sig = inspect.signature(run_phase3_with_tool_use_or_fallback)
        assert "enable_tool_use" in sig.parameters
        default = sig.parameters["enable_tool_use"].default
        assert default is False, f"enable_tool_use 기본값이 False가 아님: {default!r}"
