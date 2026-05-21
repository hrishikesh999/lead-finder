from __future__ import annotations

import asyncio
import json
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic
from bs4 import BeautifulSoup
from loguru import logger

from .config import Settings
from .models import CandidateChannel, EnrichedCandidate, ExtractionResult

_FOLLOW_PATHS = ["/about", "/about-us", "/team", "/our-story", "/contact"]
_MAX_PAGES = 5
_FETCH_TIMEOUT = 15.0
_MAX_CONCURRENT = 20
_MAX_HTML_CHARS = 40_000

_CLAUDE_SYSTEM_PROMPT = (
    "You are an information extraction assistant specializing in identifying "
    "business founders and operators from website content. Extract information "
    "accurately and conservatively — if you are not confident, leave fields null "
    "rather than guessing."
)

_ENTERPRISE_PLAYERS: dict[str, str] = {
    "US": "Kaplan, Penn Foster, ABA, Becker, Mike Holt Enterprises, ICC, NASCLA, AHIT, Cengage, Wiley, McGraw-Hill",
    "CA": "Kaplan, Pearson VUE, Humber College, George Brown College, Algonquin College",
    "GB": "City & Guilds, EAL, BPEC, Logic4training, Pearson, NOCN, Training Express, CORGI",
    "AU": "TAFE, Pearson, Cengage, Wiley, Builders Academy Australia, Master Builders",
}
_COUNTRY_LABELS: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "AU": "Australia",
}

_CLAUDE_USER_PROMPT_TEMPLATE = """\
You are extracting structured data from website HTML for a B2B prospect list.

Website URL: {url}
Target trade: {trade} exam preparation courses, study guides, or practice tests.
Target country: {country_label}

Given the HTML content below, return ONLY a JSON object with these fields. No prose, no markdown, just JSON.

{{
  "company_name": "string",
  "founder_name": "string or null",
  "founder_role": "string or null",
  "team_size_signal": "solo | small | mid | enterprise | unknown",
  "trade_focus_matches_target": true | false,
  "has_newsletter_signal": true | false,
  "has_lead_magnet_signal": true | false,
  "is_enterprise_player": true | false,
  "notes": "brief string or null",
  "extraction_confidence": "high | medium | low"
}}

Target trade for this extraction: {trade}

Known enterprise players in {country_label} to flag as is_enterprise_player=true:
- {enterprise_list}

Definitions:
- "solo" = clearly one person running it
- "small" = 2-10 people
- "mid" = 11-50 people
- "enterprise" = 50+ people or part of a larger education company
- "has_newsletter_signal" = visible email signup form, popup, or "subscribe" button
- "has_lead_magnet_signal" = free downloadable resource (study guide, practice test, etc.) gated behind email
- trade_focus_matches_target = false if the site is clearly not about {trade} licensing or exam prep

HTML content:
{content}"""


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:_MAX_HTML_CHARS]


def _build_follow_urls(base_url: str) -> list[str]:
    """Returns follow-up URLs for about/team pages, normalized to root domain."""
    try:
        parsed = urlparse(base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        return [f"{root}{path}" for path in _FOLLOW_PATHS]
    except Exception:
        base = base_url.rstrip("/")
        return [f"{base}{path}" for path in _FOLLOW_PATHS]


async def _fetch_page(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        resp = await client.get(url, follow_redirects=True, timeout=_FETCH_TIMEOUT)
        if resp.status_code == 200 and "text/html" in resp.headers.get(
            "content-type", ""
        ):
            return _strip_html(resp.text)
    except Exception as exc:
        logger.debug("Failed to fetch {}: {}", url, exc)
    return None


def _call_claude(
    client: Anthropic,
    content: str,
    url: str,
    trade: str,
    country_code: str,
    model: str,
) -> Optional[ExtractionResult]:
    prompt = _CLAUDE_USER_PROMPT_TEMPLATE.format(
        trade=trade,
        url=url,
        content=content,
        country_label=_COUNTRY_LABELS.get(country_code, country_code),
        enterprise_list=_ENTERPRISE_PLAYERS.get(country_code, _ENTERPRISE_PLAYERS["US"]),
    )
    try:
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_CLAUDE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown fences if Claude wraps the JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        return ExtractionResult(**data)
    except Exception as exc:
        logger.warning("Claude extraction failed for {}: {}", url, exc)
        return None


async def fetch_and_extract(
    candidate: CandidateChannel,
    trade: str,
    country_code: str,
    settings: Settings,
    client: httpx.AsyncClient,
    anthropic_client: Anthropic,
    semaphore: asyncio.Semaphore,
) -> Optional[EnrichedCandidate]:
    """Fetches website, extracts founder info via Claude."""
    async with semaphore:
        url = candidate.website_url
        pages_text: list[str] = []
        pages_fetched = 0

        homepage_text = await _fetch_page(client, url)
        if homepage_text:
            pages_text.append(homepage_text)
            pages_fetched += 1

        if not pages_text:
            logger.warning("Could not fetch homepage for {}", url)
            return None

        combined = "\n\n---PAGE BREAK---\n\n".join(pages_text)
        result = await asyncio.to_thread(
            _call_claude, anthropic_client, combined, url, trade, country_code, settings.claude_model
        )

        if result and result.founder_name:
            return EnrichedCandidate(
                channel=candidate, extraction=result, pages_fetched=pages_fetched
            )

        # Follow additional pages if founder not found
        follow_urls = _build_follow_urls(url)
        for follow_url in follow_urls:
            if pages_fetched >= _MAX_PAGES:
                break
            page_text = await _fetch_page(client, follow_url)
            if page_text:
                pages_text.append(page_text)
                pages_fetched += 1

        if pages_fetched > 1 or not result:
            combined = "\n\n---PAGE BREAK---\n\n".join(pages_text)
            result = await asyncio.to_thread(
                _call_claude, anthropic_client, combined, url, trade, country_code, settings.claude_model
            )

        if result is None:
            logger.warning("Extraction completely failed for {}", url)
            return None

        return EnrichedCandidate(
            channel=candidate, extraction=result, pages_fetched=pages_fetched
        )


async def batch_extract(
    candidates: list[CandidateChannel],
    trade: str,
    country_code: str,
    settings: Settings,
) -> list[EnrichedCandidate]:
    """Runs fetch_and_extract for all candidates concurrently."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    anthropic_client = Anthropic(api_key=settings.anthropic_api_key)
    enriched: list[EnrichedCandidate] = []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    async with httpx.AsyncClient(headers=headers) as http_client:
        tasks = [
            fetch_and_extract(
                candidate=c,
                trade=trade,
                country_code=country_code,
                settings=settings,
                client=http_client,
                anthropic_client=anthropic_client,
                semaphore=semaphore,
            )
            for c in candidates
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for candidate, result in zip(candidates, results):
        if isinstance(result, Exception):
            logger.warning(
                "Unexpected error extracting {}: {}", candidate.website_url, result
            )
        elif result is not None:
            enriched.append(result)

    logger.info(
        "Extraction complete: {}/{} candidates enriched", len(enriched), len(candidates)
    )
    return enriched
