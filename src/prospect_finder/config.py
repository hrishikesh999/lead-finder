from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Required API keys
    youtube_api_key: str
    anthropic_api_key: str
    hunter_api_key: str
    google_sheet_id: str
    google_sheets_credentials_json: str
    neon_database_url: str

    # Tuning knobs
    min_youtube_subscribers: int = 10_000
    min_email_confidence: int = 80
    max_candidates_per_run: int = 400
    allowed_countries: str = "US,CA"
    claude_model: str = "claude-haiku-4-5-20251001"
    log_level: str = "INFO"

    # Optional: Brave Search API (direct website discovery)
    # If not set, Brave Search discovery is silently skipped.
    brave_search_api_key: Optional[str] = None

    @property
    def allowed_countries_set(self) -> set[str]:
        return {c.strip().upper() for c in self.allowed_countries.split(",")}

    @property
    def credentials_dict(self) -> dict:
        return json.loads(self.google_sheets_credentials_json)


def load_keywords(trade: str, country: str) -> list[str]:
    """Loads keywords for the given trade+country combination from config/keywords.yaml."""
    keywords_path = Path(__file__).parent.parent.parent / "config" / "keywords.yaml"
    with keywords_path.open() as f:
        data = yaml.safe_load(f)
    if trade not in data:
        raise KeyError(
            f"Trade '{trade}' not found in keywords.yaml. "
            f"Available: {list(data.keys())}"
        )
    trade_data = data[trade]
    if country not in trade_data:
        raise KeyError(
            f"No keywords for trade='{trade}' country='{country}'. "
            f"Valid countries for '{trade}': {list(trade_data.keys())}"
        )
    return trade_data[country]
