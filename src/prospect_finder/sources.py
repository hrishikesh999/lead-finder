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


def discover_via_brave_search(
    keywords: list[str],
    country_code: str,
    settings: Settings,
) -> list[CandidateChannel]:
    """
    Discovers prospect websites directly from Brave Search results.
    Every result is a live website already ranking for exam-prep queries —
    far higher signal than hoping a YouTube channel put its URL in its description.
    Returns [] silently if BRAVE_SEARCH_API_KEY is not configured.
    Free tier: 2,000 queries/month at api.search.brave.com.
    """
    if not settings.brave_search_api_key:
        return []

    seen: set[str] = set()
    candidates: list[CandidateChannel] = []

    for keyword in keywords:
        logger.info("Brave Search: '{}'", keyword)
        try:
            resp = httpx.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": settings.brave_search_api_key,
                },
                params={
                    "q": keyword,
                    "count": 10,
                    "country": country_code,
                    "search_lang": "en",
                    "result_filter": "web",
                },
                timeout=10.0,
            )
            resp.raise_for_status()

            results = resp.json().get("web", {}).get("results", [])
            for item in results:
                url = item.get("url", "")
                domain = _domain(url)
                if not domain or domain in seen:
                    continue
                if _is_blocked(domain, _SEARCH_EXCLUDED_DOMAINS):
                    continue

                seen.add(domain)
                candidates.append(
                    CandidateChannel(
                        channel_id=f"brave:{domain}",
                        name=item.get("title", domain)[:200],
                        subscriber_count=0,
                        country=country_code,
                        website_url=_root_url(url) or url,
                        youtube_url="",
                        description_snippet=(item.get("description") or "")[:500] or None,
                    )
                )

            time.sleep(0.1)

        except Exception as exc:
            logger.warning("Brave Search failed for '{}': {}", keyword, exc)

    logger.info("Brave Search: {} unique websites discovered", len(candidates))
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
