from __future__ import annotations

import re
import time
from typing import Optional
from urllib.parse import urlparse

from googleapiclient.discovery import build
from loguru import logger

from .config import Settings
from .models import CandidateChannel

_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+|(?<![/\w])www\.[a-zA-Z0-9][^\s\"'<>]*",
    re.IGNORECASE,
)

# Domains that are social-media profiles or platforms, not business websites.
# URLs from these domains are useless for email finding.
_JUNK_DOMAINS = {
    "facebook.com", "fb.com",
    "instagram.com",
    "twitter.com", "x.com",
    "youtube.com", "youtu.be",
    "tiktok.com",
    "linkedin.com",
    "t.me", "telegram.me",
    "discord.gg", "discord.com",
    "linktr.ee",
    "bit.ly", "tinyurl.com",
}


def _build_youtube_client(api_key: str):
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def _extract_url_from_description(description: Optional[str]) -> Optional[str]:
    """Returns the first business website URL found in a channel description.
    Skips social media, YouTube, and other non-business domains.
    """
    if not description:
        return None
    for match in _URL_RE.finditer(description):
        url = match.group(0).rstrip(".,;:!?)\"'")
        if url.lower().startswith("www."):
            url = "https://" + url
        try:
            host = urlparse(url).netloc.lower().lstrip("www.")
            if any(host == d or host.endswith("." + d) for d in _JUNK_DOMAINS):
                continue
        except Exception:
            continue
        return url
    return None


def _search_channel_ids(youtube, query: str, max_results: int = 50) -> list[str]:
    """
    Calls YouTube search.list with type=channel.
    Quota cost: 100 units per page.
    """
    channel_ids: list[str] = []
    next_page_token = None

    while len(channel_ids) < max_results:
        batch_size = min(50, max_results - len(channel_ids))
        params: dict = {
            "part": "id",
            "q": query,
            "type": "channel",
            "maxResults": batch_size,
        }
        if next_page_token:
            params["pageToken"] = next_page_token

        response = youtube.search().list(**params).execute()
        for item in response.get("items", []):
            cid = item["id"].get("channelId")
            if cid:
                channel_ids.append(cid)

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return channel_ids


def _search_channel_ids_from_videos(youtube, query: str) -> list[str]:
    """
    Single-page video search — extracts unique channel IDs from up to 50 results.
    Kept to one page deliberately: video dedup means 50 videos yield far fewer
    unique channels, so paginating to hit a target would burn quota unpredictably.
    Quota cost: exactly 100 units (one call, no pagination).
    """
    params: dict = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 50,
    }
    response = youtube.search().list(**params).execute()
    seen: set[str] = set()
    channel_ids: list[str] = []
    for item in response.get("items", []):
        cid = item.get("snippet", {}).get("channelId")
        if cid and cid not in seen:
            seen.add(cid)
            channel_ids.append(cid)
    return channel_ids


def _fetch_channel_details(
    youtube,
    channel_ids: list[str],
    settings: Settings,
) -> list[CandidateChannel]:
    """
    Calls channels.list in batches of 50.
    Quota cost: 1 unit per batch.
    """
    results: list[CandidateChannel] = []
    allowed = settings.allowed_countries_set

    for i in range(0, len(channel_ids), 50):
        batch = channel_ids[i : i + 50]
        response = (
            youtube.channels()
            .list(
                part="snippet,statistics",
                id=",".join(batch),
                maxResults=50,
            )
            .execute()
        )

        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})

            if stats.get("hiddenSubscriberCount", False):
                continue

            sub_count = int(stats.get("subscriberCount", 0))
            if sub_count < settings.min_youtube_subscribers:
                continue

            country = snippet.get("country")
            if country and country.upper() not in allowed:
                logger.debug(
                    "Skipping channel {} — country {} not allowed",
                    item["id"],
                    country,
                )
                continue

            description = snippet.get("description", "")
            website_url = _extract_url_from_description(description)

            channel_id = item["id"]
            results.append(
                CandidateChannel(
                    channel_id=channel_id,
                    name=snippet.get("title", ""),
                    subscriber_count=sub_count,
                    country=country,
                    website_url=website_url,
                    youtube_url=f"https://www.youtube.com/channel/{channel_id}",
                    description_snippet=description[:500] if description else None,
                )
            )

    return results


def search_youtube_channels(
    trade: str,
    keywords: list[str],
    settings: Settings,
    max_results_per_keyword: int = 50,
) -> list[CandidateChannel]:
    """
    Discovers candidate channels for a given trade across all keywords.
    Runs both channel-search and video-search per keyword so that niche
    instructors who don't appear in channel results are still surfaced.
    Deduplicates by channel_id. Drops channels with no website URL.
    """
    youtube = _build_youtube_client(settings.youtube_api_key)
    seen_channel_ids: set[str] = set()
    all_candidates: list[CandidateChannel] = []

    for keyword in keywords:
        logger.info("Searching YouTube: '{}'", keyword)
        try:
            # Channel search: finds dedicated exam-prep channels
            channel_ids = _search_channel_ids(youtube, keyword, max_results_per_keyword)
            # Video search: finds active instructors who may not rank in channel search
            video_channel_ids = _search_channel_ids_from_videos(youtube, keyword)
            combined_ids = list(dict.fromkeys(channel_ids + video_channel_ids))

            new_ids = [cid for cid in combined_ids if cid not in seen_channel_ids]
            seen_channel_ids.update(new_ids)

            if not new_ids:
                logger.debug("No new channels for keyword '{}'", keyword)
                continue

            logger.debug(
                "Keyword '{}': {} channel-search + {} video-search = {} unique new",
                keyword,
                len(channel_ids),
                len(video_channel_ids),
                len(new_ids),
            )

            candidates = _fetch_channel_details(youtube, new_ids, settings)

            with_website = [c for c in candidates if c.website_url]
            dropped = len(candidates) - len(with_website)
            if dropped:
                logger.debug("{} channels skipped (no website) for '{}'", dropped, keyword)

            all_candidates.extend(with_website)
            # YouTube allows ~10 search.list calls/minute; each keyword burns 2
            # (channel + video search). Sleep 12s to stay safely under the cap.
            time.sleep(12)

        except Exception as exc:
            logger.warning("Error processing keyword '{}': {}", keyword, exc)
            continue

    # Final dedup
    seen: set[str] = set()
    deduped: list[CandidateChannel] = []
    for c in all_candidates:
        if c.channel_id not in seen:
            seen.add(c.channel_id)
            deduped.append(c)

    logger.info(
        "Discovery complete: {} unique channels with websites for trade '{}'",
        len(deduped),
        trade,
    )
    return deduped
