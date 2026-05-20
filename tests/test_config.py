import json
from unittest.mock import patch

import pytest

from prospect_finder.config import Settings, load_keywords

_BASE_ENV = {
    "YOUTUBE_API_KEY": "yt-key",
    "ANTHROPIC_API_KEY": "ant-key",
    "HUNTER_API_KEY": "hunt-key",
    "GOOGLE_SHEET_ID": "sheet-id",
    "GOOGLE_SHEETS_CREDENTIALS_JSON": '{"type":"service_account"}',
    "NEON_DATABASE_URL": "postgresql://user:pass@host/db",
}


def test_allowed_countries_default():
    with patch.dict("os.environ", _BASE_ENV, clear=True):
        s = Settings()
        assert s.allowed_countries_set == {"US", "CA"}


def test_allowed_countries_custom():
    env = {**_BASE_ENV, "ALLOWED_COUNTRIES": "US,CA,GB"}
    with patch.dict("os.environ", env, clear=True):
        s = Settings()
        assert s.allowed_countries_set == {"US", "CA", "GB"}


def test_allowed_countries_uppercase():
    env = {**_BASE_ENV, "ALLOWED_COUNTRIES": "us,ca"}
    with patch.dict("os.environ", env, clear=True):
        s = Settings()
        assert "US" in s.allowed_countries_set
        assert "CA" in s.allowed_countries_set


def test_credentials_dict():
    creds = {"type": "service_account", "project_id": "test"}
    env = {**_BASE_ENV, "GOOGLE_SHEETS_CREDENTIALS_JSON": json.dumps(creds)}
    with patch.dict("os.environ", env, clear=True):
        s = Settings()
        assert s.credentials_dict == creds


def test_load_keywords_valid_trade():
    keywords = load_keywords("hvac")
    assert isinstance(keywords, list)
    assert len(keywords) > 0
    assert all(isinstance(k, str) for k in keywords)


def test_load_keywords_all_trades():
    for trade in ["hvac", "electrical", "plumbing", "cdl"]:
        kw = load_keywords(trade)
        assert len(kw) > 0, f"No keywords for trade '{trade}'"


def test_load_keywords_invalid_trade():
    with pytest.raises(KeyError, match="underwater_welding"):
        load_keywords("underwater_welding")
