from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "合同对比与金额审核系统"
    data_dir: Path = Path("./data")
    max_files_per_type: int = 10
    max_file_size_mb: int = 20
    max_page_count: int = 50
    worker_poll_seconds: float = 1.0
    comparison_adapter: str = "mock"
    textin_app_id: str = ""
    textin_secret_code: str = ""
    textin_api_base: str = "https://doc-compare.intsig.com"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "contract_review.sqlite3"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings

