from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://ingest:ingest@localhost:5432/ingest"

    # Comma-separated providers expected to deliver files; each is checked for staleness.
    enabled_providers: str = "copart"

    # File locations. Each provider has its own inbox sub-folder: <inbox_dir>/<provider>/.
    inbox_dir: Path = Path("/data/inbox")
    archive_dir: Path = Path("/data/archive")
    failed_dir: Path = Path("/data/failed")
    archive_retention_days: int = 7
    # A file counts as fully written once its size and mtime have not changed for this long.
    file_stable_seconds: float = 5.0
    # Safety-net rescan of the inbox, in case a filesystem event is missed (Docker bind mounts).
    inbox_rescan_seconds: float = 30.0
    heartbeat_seconds: float = 15.0

    # Alert thresholds
    stale_after_minutes: int = 75  # Copart export is downloaded hourly
    heartbeat_timeout_seconds: int = 120
    rejected_warning_ratio: float = 0.01
    rejected_critical_ratio: float = 0.10
    row_count_deviation_ratio: float = 0.30
    slow_ingest_seconds: int = 300
    # Lots missing from a file are only marked removed in a scope (yard) where the file holds at
    # least this share of the lots currently active there; guards against truncated exports.
    removal_min_ratio: float = 0.5
    baseline_runs: int = 24
    monitor_interval_seconds: float = 30.0

    # Dashboard auth (API)
    dashboard_username: str = "admin"
    dashboard_password: str = Field(default="", repr=False)
    session_secret: str = Field(default="", repr=False)
    session_max_age_seconds: int = 12 * 3600
    cookie_secure: bool = False

    @property
    def providers(self) -> list[str]:
        return [p.strip() for p in self.enabled_providers.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
