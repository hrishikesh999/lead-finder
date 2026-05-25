from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

from .config import Settings
from .models import CandidateChannel

# Podcast hosting platforms — if the feedUrl is on one of these domains
# it tells us nothing about the creator's own website.
_PODCAST_HOSTING_DOMAINS = {
    "libsyn.com", "buzzsprout.com", "anchor.fm", "spotify.com",
    "soundcloud.com", "podbean.com", "transistor.fm", "simplecast.com",
    "fireside.fm", "spreaker.com", "acast.com", "megaphone.fm",
    "omny.fm", "pinecast.com", "redcircle.com", "captivate.fm",
    "audioboom.com", "rss.com", "iheart.com", "podcastics.com",
    "podcasts.apple.com", "music.amazon.com", "podcastics.com",
}

# Domains to exclude from web search results.
_SEARCH_EXCLUDED_DOMAINS = {
    "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "reddit.com", "linkedin.com", "tiktok.com", "amazon.com", "ebay.com",
    "wikipedia.org", "wikihow.com", "quora.com", "udemy.com", "skillshare.com",
    "kaplan.com", "pennfoster.edu", "cengage.com", "wiley.com", "pearson.com",
    "google.com", "bing.com", "yahoo.com", "brave.com",
}


def _domain(url: str) -> Optional[str]:
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.lstrip("www.") if netloc else None
    except Exception:
        return None


def _root_url(url: str) -> Optional[str]:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return None


def _is_blocked(domain: str, blocklist: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in blocklist)


def discover_via_serper_search(
    keywords: list[str],
    country_code: str,
    settings: Settings,
) -> list[CandidateChannel]:
    """
    Discovers prospect websites via Serper.dev (Google Search API).
    Every result is a live website already ranking for exam-prep queries.
    Returns [] silently if SERPER_API_KEY is not configured.
    Free tier: 2,500 queries/month at serper.dev (no credit card required).
    """
    if not settings.serper_api_key:
        return []

    seen: set[str] = set()
    candidates: list[CandidateChannel] = []
    gl = country_code.lower()  # Serper uses lowercase ISO country codes

    for keyword in keywords:
        logger.info("Serper Search: '{}'", keyword)
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers={
                    "X-API-KEY": settings.serper_api_key,
                    "Content-Type": "application/json",
                },
                json={"q": keyword, "gl": gl, "num": 10},
                timeout=10.0,
            )
            resp.raise_for_status()

            results = resp.json().get("organic", [])
            for item in results:
                url = item.get("link", "")
                domain = _domain(url)
                if not domain or domain in seen:
                    continue
                if _is_blocked(domain, _SEARCH_EXCLUDED_DOMAINS):
                    continue

                seen.add(domain)
                candidates.append(
                    CandidateChannel(
                        channel_id=f"serper:{domain}",
                        name=item.get("title", domain)[:200],
                        subscriber_count=0,
                        country=country_code,
                        website_url=_root_url(url) or url,
                        youtube_url="",
                        description_snippet=(item.get("snippet") or "")[:500] or None,
                    )
                )

            time.sleep(0.1)

        except Exception as exc:
            logger.warning("Serper Search failed for '{}': {}", keyword, exc)

    logger.info("Serper Search: {} unique websites discovered", len(candidates))
    return candidates


def discover_via_podcasts(
    keywords: list[str],
    country_code: str,
) -> list[CandidateChannel]:
    """
    Discovers prospect websites via the iTunes/Apple Podcasts search API.
    Podcast educators are high-value targets: active audience, typically sell
    courses or study guides alongside their show. Free, no auth required.
    We derive the website from the RSS feedUrl — when the feed is self-hosted
    (not on Buzzsprout/Libsyn/etc.) the domain IS the creator's site.
    """
    seen: set[str] = set()
    candidates: list[CandidateChannel] = []
    itunes_country = country_code.lower()
    if itunes_country == "gb":
        itunes_country = "gb"

    for keyword in keywords:
        logger.info("Podcast search: '{}'", keyword)
        try:
            resp = httpx.get(
                "https://itunes.apple.com/search",
                params={
                    "term": keyword,
                    "media": "podcast",
                    "entity": "podcast",
                    "limit": 50,
                    "country": itunes_country,
                },
                timeout=10.0,
            )
            resp.raise_for_status()

            for podcast in resp.json().get("results", []):
                feed_url = podcast.get("feedUrl", "")
                if not feed_url:
                    continue
                domain = _domain(feed_url)
                if not domain or domain in seen:
                    continue
                if _is_blocked(domain, _PODCAST_HOSTING_DOMAINS):
                    continue

                seen.add(domain)
                candidates.append(
                    CandidateChannel(
                        channel_id=f"podcast:{domain}",
                        name=(podcast.get("collectionName") or domain)[:200],
                        subscriber_count=0,
                        country=country_code,
                        website_url=_root_url(feed_url) or feed_url,
                        youtube_url=podcast.get("trackViewUrl", ""),
                        description_snippet=None,
                    )
                )

            time.sleep(0.1)

        except Exception as exc:
            logger.warning("Podcast search failed for '{}': {}", keyword, exc)

    logger.info("Podcast search: {} unique websites discovered", len(candidates))
    return candidates
