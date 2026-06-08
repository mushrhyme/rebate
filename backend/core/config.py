from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = ""  # 더 이상 필수 아님 — S3 전환 후 제거 예정
    azure_api_key: str
    azure_api_endpoint: str
    anthropic_api_key: str = ""
    workspace_root: Path = Path(__file__).parents[2]

    admin_delete_password: str = ""  # ADMIN_DELETE_PASSWORD in .env — required for form deletion

    # ── Phase 3 Tool Use feature flag ──────────────────────────────────────────
    # 기본값 False — 명시적으로 환경변수를 켠 경우에만 Tool Use 경로 활성화
    # PHASE3_TOOL_USE_ENABLED=true 로 설정하면 run_phase3_with_tool_use_or_fallback() 사용
    # Tool Use 실패 시 자동으로 legacy run_phase3()로 fallback
    # 아직 experimental — product Tool Use, dist 1:N Claude 결정 미완성
    phase3_tool_use_enabled: bool = False

    # ── Phase 3 Tool Use 모델 ───────────────────────────────────────────────────
    # PHASE3_TOOL_USE_MODEL 환경변수로 override 가능
    # retailer/product Claude 호출 및 DB token usage 기록에 공통 사용
    phase3_tool_use_model: str = "claude-haiku-4-5-20251001"

    # ── Phase 3 Tool Use 병렬 concurrency ──────────────────────────────────────
    # PHASE3_TOOL_USE_CONCURRENCY 환경변수로 override 가능
    # retailer batch / product Tool Use 동시 실행 수 제한 (문서 내부)
    # 기본값 1 (순차) — rate limit 안전. 2 이상으로 올릴 때는 API rate limit 확인 필요
    # 0 이하 값은 run_phase3_with_tool_use_or_fallback에서 1로 보정
    phase3_tool_use_concurrency: int = 1

    # ── Phase 3 Tool Use 전역 문서 동시 처리 수 ───────────────────────────────────
    # PHASE3_TOOL_USE_GLOBAL_CONCURRENCY 환경변수로 override 가능
    # 여러 사용자가 동시에 문서를 올릴 때 Claude API Rate Limit 방지
    # 앱 전체에서 동시에 Tool Use Claude API를 호출할 수 있는 문서 수 상한
    # 기본값 3 — 초과 요청은 세마포어 대기 후 순차 처리 (Tier 2+에서 5~10으로 조정 가능)
    phase3_tool_use_global_concurrency: int = 3

    # AWS S3
    aws_s3_bucket: str = "rebate-prod-590183751473"

    # JWT — set JWT_SECRET in .env (generated once, never change after deploy)
    jwt_secret: str = ""
    jwt_expire_hours: int = 24

    # Google Sheets — set GOOGLE_SHEETS_MAPPINGS_ID in .env to enable
    google_sheets_mappings_id: str = ""

    # Google Drive — set DRIVE_ROOT_FOLDER_ID in .env to enable
    drive_root_folder_id: str = ""
    drive_credentials_path: Path = Path(__file__).parents[2] / "credentials.json"
    drive_token_path: Path = Path(__file__).parents[2] / "token.json"
    drive_service_account_path: Path = Path(__file__).parents[2] / "service_account.json"

    model_config = {"env_file": Path(__file__).parents[1] / ".env", "extra": "ignore"}

    @property
    def samples_dir(self) -> Path:
        return self.workspace_root / "samples"

    @property
    def extracted_dir(self) -> Path:
        return self.workspace_root / "extracted"

    @property
    def form_definitions_dir(self) -> Path:
        return self.workspace_root / "form_definitions"

    @property
    def mappings_dir(self) -> Path:
        return self.workspace_root / "mappings"

    @property
    def drive_enabled(self) -> bool:
        return bool(self.drive_root_folder_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()


_drive = None


def get_drive():
    """Return DriveStorage singleton, or None if Drive not configured."""
    settings = get_settings()
    if not settings.drive_enabled:
        return None
    global _drive
    if _drive is None:
        from .drive_storage import DriveStorage
        _drive = DriveStorage(
            settings.drive_credentials_path,
            settings.drive_token_path,
            service_account_path=settings.drive_service_account_path,
        )
    return _drive
