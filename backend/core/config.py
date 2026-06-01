from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    azure_api_key: str
    azure_api_endpoint: str
    anthropic_api_key: str = ""
    workspace_root: Path = Path(__file__).parents[2]

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
