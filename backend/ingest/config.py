from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://ingest:ingest@localhost:5432/ingest"
    # The API's own database, kept in step by the worker (api_sync.py). Empty = no sync.
    api_database_url: str = Field(default="", repr=False)
    # The API database keeps a removed lot until its sale time plus this (auctions run 2-4 h).
    api_keep_after_sale_minutes: int = 240
    # How often the worker syncs the API database besides right after each ingested file.
    api_sync_interval_seconds: float = 900.0
    # The final price model (pricing.py); without the file, lots are not priced.
    price_model_path: Path = Path("/data/models/final_price.ubj")
    # Third-party auction data APIs with final sale prices (api_check.py). Empty = not used.
    apibara_api_key: str = Field(default="", repr=False)
    apiauctions_token: str = Field(default="", repr=False)

    # AI condition and damage analysis of a lot (vehicle_analysis.py), run by the worker on
    # request. The provider is a key of `ai.CLIENTS`; the prompt and schema versions must be
    # ones vehicle_analysis.py knows.
    ai_provider: str = "anthropic"
    ai_model: str = "claude-opus-5-5"  # preselected on the lot page; one of ai.MODELS
    ai_language: str = "en"  # default output language of a requested analysis
    ai_prompt_version: str = "vehicle-damage-v1"
    ai_schema_version: str = "1.0"
    ai_max_images: int = 5  # photos sent per analysis, at most (fewer when fewer are useful)
    ai_image_max_px: int = 1024  # longest side of a photo as sent; smaller ones are kept as is
    analysis_poll_seconds: float = 3.0  # how soon the worker notices a requested analysis
    anthropic_api_key: str = Field(default="", repr=False)

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
    # The downloader heartbeats every 30 s; after this long without one it counts as stopped.
    downloader_timeout_seconds: int = 150
    rejected_warning_ratio: float = 0.01
    rejected_critical_ratio: float = 0.10
    row_count_deviation_ratio: float = 0.30
    slow_ingest_seconds: int = 300
    # Lots missing from a file are only marked removed in a scope (yard) where the file holds at
    # least this share of the lots currently active there; guards against truncated exports.
    removal_min_ratio: float = 0.5
    # A lot is only marked removed once its sale time plus this grace period has passed: the
    # auction opens at the sale time, but bidding on the lot itself ends some time later.
    removal_grace_minutes: int = 120
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
