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
    # Apple — both subdomains appear in iTunes API responses
    "podcasts.apple.com", "apps.apple.com", "apple.com",
    "music.amazon.com",
}

# Domains to exclude from web search results.
_SEARCH_EXCLUDED_DOMAINS = {
    "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "reddit.com", "linkedin.com", "tiktok.com", "amazon.com", "ebay.com",
    "wikipedia.org", "wikihow.com", "quora.com", "udemy.com", "skillshare.com",
    "kaplan.com", "pennfoster.edu", "cengage.com", "wiley.com", "pearson.com",
    "google.com", "bing.com", "yahoo.com", "brave.com",
    # Google utility pages (forms, docs, etc. — not websites)
    "forms.gle", "docs.google.com", "drive.google.com",
    # Job boards
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com", "careerbuilder.com",
    # Large study/MOOC platforms (not independent creators)
    "quizlet.com", "coursera.org", "edx.org", "khanacademy.org", "chegg.com",
    # Trade software / directories (not exam prep)
    "jobber.com", "housecallpro.com", "servicetitan.com", "angi.com", "thumbtack.com",
    # Testing/licensing agencies — not educators
    "prometric.com", "psiexams.com", "psionline.com", "provexam.com",
    "nictesting.org", "pearsonvue.com", "prometrics.com",
    # Education directories and aggregators
    "educations.com", "research.com", "academicinfo.net", "niche.com",
    "petersons.com", "cappex.com", "collegedunia.com",
    # Large national publishers / CE platforms (not independent creators)
    "milady.com", "miladypro.com", "miladytraining.com",
    "elitelearning.com", "elitecme.com",
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
    # Reject government domains globally — state/federal licensing boards are not prospects.
    if domain.endswith(".gov") or domain.endswith(".gov.uk") or domain.endswith(".gov.au"):
        return True
    return any(domain == d or domain.endswith("." + d) for d in blocklist)


def _run_serper_queries(
    queries: list[str],
    country_code: str,
    settings: Settings,
    id_prefix: str,
    seen: set[str],
) -> list[CandidateChannel]:
    """Shared Serper HTTP logic. `seen` is passed in so callers can share a dedup set."""
    candidates: list[CandidateChannel] = []
    gl = country_code.lower()

    for query in queries:
        logger.info("Serper [{}]: '{}'", id_prefix, query)
        try:
            resp = httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
                json={"q": query, "gl": gl, "num": 10},
                timeout=10.0,
            )
            resp.raise_for_status()

            for item in resp.json().get("organic", []):
                url = item.get("link", "")
                domain = _domain(url)
                if not domain or domain in seen:
                    continue
                if _is_blocked(domain, _SEARCH_EXCLUDED_DOMAINS):
                    continue
                seen.add(domain)
                candidates.append(
                    CandidateChannel(
                        channel_id=f"{id_prefix}:{domain}",
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
            logger.warning("Serper [{}] failed for '{}': {}", id_prefix, query, exc)

    return candidates


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
    results = _run_serper_queries(keywords, country_code, settings, "serper", seen)
    logger.info("Serper Search: {} unique websites discovered", len(results))
    return results


def discover_via_course_search(
    keywords: list[str],
    country_code: str,
    settings: Settings,
) -> list[CandidateChannel]:
    """
    Runs creator-focused Serper queries to find independent trades educators
    who sell online courses or training content but may not rank for exam-prep
    terms. Generates query variants like "HVAC online training course" and
    "learn electrical online" from the primary trade keywords.
    Returns [] silently if SERPER_API_KEY is not configured.
    """
    if not settings.serper_api_key:
        return []

    # Pull the primary trade term from each keyword (first word), deduplicated.
    # e.g. ["HVAC exam prep", "EPA 608 study guide"] → ["HVAC", "EPA"]
    seen_terms: set[str] = set()
    trade_terms: list[str] = []
    for kw in keywords:
        term = kw.split()[0]
        if term.lower() not in seen_terms:
            seen_terms.add(term.lower())
            trade_terms.append(term)
    trade_terms = trade_terms[:4]  # cap to keep Serper quota reasonable

    queries: list[str] = []
    for term in trade_terms:
        queries += [
            f"{term} online training course",
            f"{term} instructor online course",
            f"learn {term} online certification",
            f"{term} skills training program online",
            f"{term} apprenticeship training online",
        ]

    seen: set[str] = set()
    results = _run_serper_queries(queries, country_code, settings, "course", seen)
    logger.info("Course Search: {} unique websites discovered", len(results))
    return results


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
