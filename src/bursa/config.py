"""Application settings, loaded from the environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+pysqlite:///./bursa.sqlite"

    storage_backend: str = "local"
    storage_local_root: Path = PROJECT_ROOT / "storage"
    inbox_dir: Path = PROJECT_ROOT / "inbox"

    r2_account_id: str | None = None
    r2_bucket: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    # Optional: overrides the endpoint derived from R2_ACCOUNT_ID (any
    # S3-compatible endpoint; also lets tests/dev point at a local fake).
    r2_endpoint_url: str | None = None
    # Local cache for remote blobs materialized for extraction. None means
    # <system temp>/bursa-blobs.
    blob_cache_dir: Path | None = None

    anthropic_api_key: str | None = None
    mapper_model: str = "claude-opus-5"
    mapper_effort: str = "high"

    min_mapping_confidence: float = 0.80
    validation_tolerance_units: float = 1.0

    # Scraper politeness. Fill in real contact details - an anonymous or
    # spoofed User-Agent is bad practice regardless of legality.
    scraper_user_agent: str = (
        "BursaEquityPipeline-Research/0.1 (contact: set SCRAPER_USER_AGENT in .env)"
    )
    scraper_min_delay_seconds: float = 1.0
    scraper_max_concurrency: int = 8
    scraper_max_retries: int = 3
    scraper_timeout_seconds: float = 20.0

    # IR-site crawl depth (bursa.scrapers.ir_fallback.sniff_annual_report).
    # `scraper_base_hops` is the starting hop budget - generous enough for a
    # "newest year only" request. When more distinct years are asked for
    # than that turns up, the crawl escalates its own hop limit at runtime
    # rather than giving up - by at least `scraper_hop_escalation_step` hops
    # at a time, never past `scraper_max_hops_ceiling` regardless of how
    # many years are still missing, and never fetching more than
    # `scraper_max_pages_per_sniff` pages total in one company's sniff. All
    # four are overridable per-environment (`.env`) rather than requiring a
    # code change, since the right depth is a property of the *site*
    # (confirmed to vary: AMMB/Public Bank need real depth, most sites don't)
    # not something this codebase can know in advance.
    scraper_base_hops: int = 3
    scraper_hop_escalation_step: int = 2
    scraper_max_hops_ceiling: int = 9
    scraper_max_pages_per_sniff: int = 40

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
