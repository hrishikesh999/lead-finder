from unittest.mock import MagicMock

import httpx
import pytest
import respx

from prospect_finder.sources import (
    _domain,
    _is_blocked,
    _PODCAST_HOSTING_DOMAINS,
    _root_url,
    _SEARCH_EXCLUDED_DOMAINS,
    discover_via_serper_search,
    discover_via_podcasts,
)

_SERPER_RESPONSE = {
    "organic": [
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


def _mock_settings(with_serper=True):
    m = MagicMock()
    m.serper_api_key = "test-key" if with_serper else None
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


# ── Serper Search discovery ───────────────────────────────────────────────────

@respx.mock
def test_serper_returns_empty_without_credentials():
    candidates = discover_via_serper_search(["hvac exam prep"], "US", _mock_settings(with_serper=False))
    assert candidates == []


@respx.mock
def test_serper_filters_junk_domains():
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json=_SERPER_RESPONSE)
    )
    candidates = discover_via_serper_search(["hvac exam prep"], "US", _mock_settings())
    domains = [c.website_url for c in candidates]
    assert "https://hvacschool.com" in domains
    assert "https://examprep.com" in domains
    assert not any("youtube.com" in d for d in domains)


@respx.mock
def test_serper_deduplicates_across_keywords():
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json=_SERPER_RESPONSE)
    )
    candidates = discover_via_serper_search(
        ["hvac exam prep", "hvac license exam"], "US", _mock_settings()
    )
    urls = [c.website_url for c in candidates]
    assert len(urls) == len(set(urls))


@respx.mock
def test_serper_channel_id_format():
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(200, json=_SERPER_RESPONSE)
    )
    candidates = discover_via_serper_search(["hvac exam prep"], "US", _mock_settings())
    assert all(c.channel_id.startswith("serper:") for c in candidates)


@respx.mock
def test_serper_handles_api_error():
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(429, json={"error": "rate limit"})
    )
    candidates = discover_via_serper_search(["hvac exam prep"], "US", _mock_settings())
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
