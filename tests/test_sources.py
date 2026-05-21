from unittest.mock import MagicMock, patch

import pytest
import respx
import httpx

from prospect_finder.sources import (
    _domain,
    _root_url,
    _is_blocked,
    _CSE_EXCLUDED_DOMAINS,
    _PODCAST_HOSTING_DOMAINS,
    discover_via_google_cse,
    discover_via_podcasts,
)

_CSE_RESPONSE = {
    "items": [
        {
            "link": "https://hvacschool.com/study-guide",
            "title": "HVAC School Study Guide",
            "snippet": "Best HVAC exam prep resource",
        },
        {
            "link": "https://youtube.com/channel/ABC",
            "title": "YouTube channel",
            "snippet": "Should be filtered",
        },
        {
            "link": "https://examprep.com/hvac",
            "title": "HVAC Exam Prep",
            "snippet": "Study for your HVAC license",
        },
    ]
}

_PODCAST_RESPONSE = {
    "results": [
        {
            "collectionName": "HVAC School Podcast",
            "feedUrl": "https://hvacschool.com/feed/podcast",
            "trackViewUrl": "https://podcasts.apple.com/us/podcast/hvac-school/id123",
        },
        {
            "collectionName": "Trade Talk Podcast",
            "feedUrl": "https://buzzsprout.com/123456/feed",  # hosting platform — skip
            "trackViewUrl": "https://podcasts.apple.com/us/podcast/trade-talk/id456",
        },
        {
            "collectionName": "HVAC Insider",
            "feedUrl": "https://hvacinsider.com/rss",
            "trackViewUrl": "https://podcasts.apple.com/us/podcast/hvac-insider/id789",
        },
    ]
}


def _mock_settings(with_cse=True):
    m = MagicMock()
    m.google_cse_api_key = "test-key" if with_cse else None
    m.google_cse_cx = "test-cx" if with_cse else None
    return m


# ── _domain / _root_url helpers ──────────────────────────────────────────────

def test_domain_strips_www():
    assert _domain("https://www.hvacschool.com/foo") == "hvacschool.com"


def test_domain_no_www():
    assert _domain("https://hvacschool.com") == "hvacschool.com"


def test_root_url_strips_path():
    assert _root_url("https://hvacschool.com/courses/hvac") == "https://hvacschool.com"


def test_root_url_bad_input():
    assert _root_url("not-a-url") is None


# ── CSE discovery ────────────────────────────────────────────────────────────

@respx.mock
def test_cse_returns_empty_without_credentials():
    candidates = discover_via_google_cse(["hvac exam prep"], "US", _mock_settings(with_cse=False))
    assert candidates == []


@respx.mock
def test_cse_filters_junk_domains():
    respx.get("https://www.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(200, json=_CSE_RESPONSE)
    )
    candidates = discover_via_google_cse(["hvac exam prep"], "US", _mock_settings())
    domains = [c.website_url for c in candidates]
    assert "https://hvacschool.com" in domains
    assert "https://examprep.com" in domains
    assert not any("youtube.com" in d for d in domains)


@respx.mock
def test_cse_deduplicates_across_keywords():
    respx.get("https://www.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(200, json=_CSE_RESPONSE)
    )
    candidates = discover_via_google_cse(
        ["hvac exam prep", "hvac license exam"], "US", _mock_settings()
    )
    urls = [c.website_url for c in candidates]
    assert len(urls) == len(set(urls))


@respx.mock
def test_cse_channel_id_format():
    respx.get("https://www.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(200, json=_CSE_RESPONSE)
    )
    candidates = discover_via_google_cse(["hvac exam prep"], "US", _mock_settings())
    assert all(c.channel_id.startswith("cse:") for c in candidates)


@respx.mock
def test_cse_handles_api_error():
    respx.get("https://www.googleapis.com/customsearch/v1").mock(
        return_value=httpx.Response(403, json={"error": {"message": "quota exceeded"}})
    )
    # Should not raise — returns empty list and logs warning
    candidates = discover_via_google_cse(["hvac exam prep"], "US", _mock_settings())
    assert candidates == []


# ── Podcast discovery ────────────────────────────────────────────────────────

@respx.mock
def test_podcasts_filters_hosting_platforms():
    respx.get("https://itunes.apple.com/search").mock(
        return_value=httpx.Response(200, json=_PODCAST_RESPONSE)
    )
    candidates = discover_via_podcasts(["hvac exam prep"], "US")
    domains = [c.website_url for c in candidates]
    assert "https://hvacschool.com" in domains
    assert "https://hvacinsider.com" in domains
    assert not any("buzzsprout.com" in d for d in domains)


@respx.mock
def test_podcasts_channel_id_format():
    respx.get("https://itunes.apple.com/search").mock(
        return_value=httpx.Response(200, json=_PODCAST_RESPONSE)
    )
    candidates = discover_via_podcasts(["hvac exam prep"], "US")
    assert all(c.channel_id.startswith("podcast:") for c in candidates)


@respx.mock
def test_podcasts_handles_api_error():
    respx.get("https://itunes.apple.com/search").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    candidates = discover_via_podcasts(["hvac exam prep"], "US")
    assert candidates == []
