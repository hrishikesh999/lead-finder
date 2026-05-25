from __future__ import annotations

import re
import time
from typing import Optional

import httpx
from loguru import logger

from .config import Settings

_HUNTER_BASE = "https://api.hunter.io/v2"
_RATE_LIMIT_SLEEP = 0.1
_GENERIC_EMAIL_PREFIXES = {
    "info", "contact", "support", "hello", "admin",
    "sales", "team", "noreply", "no-reply", "mail", "office",
}

_EMAIL_RE = re.compile(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b')

# Domains belonging to platforms/CDNs — not the site owner's email
_JUNK_EMAIL_DOMAINS = {
    "example.com", "sentry.io", "gravatar.com", "w3.org",
    "wordpress.com", "wpengine.com", "cloudflare.com",
    "google.com", "googleapis.com", "amazonaws.com",
    "akamai.com", "fastly.com", "jsdelivr.net",
}

_SCRAPE_PATHS = ["/", "/contact", "/contact-us", "/about", "/about-us"]
_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def _is_personal_email(email: str) -> bool:
    prefix = email.split("@")[0].lower()
    return prefix not in _GENERIC_EMAIL_PREFIXES


def _scrape_contact_email(domain: str) -> Optional[tuple[str, int]]:
    """
    Fetches contact/about pages and extracts email addresses directly.
    Prefers emails on the site's own domain (e.g. john@hvacschool.com).
    Returns (email, 90) — confidence 90 since the email is on the site itself.
    Falls through silently if nothing is found or all requests fail.
    """
    own_domain: list[str] = []
    other: list[str] = []

    with httpx.Client(timeout=10.0, follow_redirects=True, headers=_SCRAPE_HEADERS) as client:
        for path in _SCRAPE_PATHS:
            try:
                resp = client.get(f"https://{domain}{path}")
                if resp.status_code != 200:
                    continue
                for match in _EMAIL_RE.finditer(resp.text):
                    email = match.group(0).lower()
                    local, _, email_domain = email.partition("@")
                    if not email_domain:
                        continue
                    if local in _GENERIC_EMAIL_PREFIXES:
                        continue
                    if email_domain in _JUNK_EMAIL_DOMAINS or any(
                        email_domain.endswith("." + j) for j in _JUNK_EMAIL_DOMAINS
                    ):
                        continue
                    is_own = email_domain == domain or email_domain.endswith("." + domain)
                    if is_own:
                        if email not in own_domain:
                            own_domain.append(email)
                    else:
                        if email not in other:
                            other.append(email)
            except Exception:
                continue

    best = own_domain[0] if own_domain else (other[0] if other else None)
    return (best, 90) if best else None


def find_email_for_founder(
    domain: str,
    first_name: str,
    last_name: str,
    settings: Settings,
) -> Optional[tuple[str, int]]:
    """
    Hunter.io Email Finder. Used when Claude identified a founder name.
    Returns (email, confidence) or None.
    Falls back to domain search if last_name is empty (single-name founders).
    """
    # Try scraping the site directly — catches emails Hunter.io never indexed
    scraped = _scrape_contact_email(domain)
    if scraped:
        logger.debug("Scraped email for {}: {}", domain, scraped[0])
        return scraped

    if not last_name:
        logger.debug("No last name for founder '{}' on {}, falling back to domain search", first_name, domain)
        return find_email_for_domain(domain, settings)

    params = {
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
        "api_key": settings.hunter_api_key,
    }
    try:
        resp = httpx.get(
            f"{_HUNTER_BASE}/email-finder", params=params, timeout=10.0
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        email = data.get("email")
        confidence = data.get("score", 0)
        deliverability = data.get("result", "")

        if not email:
            return None
        if deliverability == "undeliverable":
            logger.debug("Email {} marked undeliverable", email)
            return None
        if confidence < settings.min_email_confidence:
            logger.debug("Email {} confidence {} below threshold", email, confidence)
            return None

        return (email, confidence)

    except Exception as exc:
        logger.warning(
            "Hunter Email Finder failed for {}/{} {}: {}", domain, first_name, last_name, exc
        )
        return None
    finally:
        time.sleep(_RATE_LIMIT_SLEEP)


def find_email_for_domain(
    domain: str,
    settings: Settings,
) -> Optional[tuple[str, int]]:
    """
    Hunter.io Domain Search. Used when Claude could not identify a founder.
    Picks the best personal-pattern email. Returns (email, confidence) or None.
    """
    # Try scraping the site directly first
    scraped = _scrape_contact_email(domain)
    if scraped:
        logger.debug("Scraped email for {}: {}", domain, scraped[0])
        return scraped

    params = {
        "domain": domain,
        "type": "personal",
        "limit": 10,
        "api_key": settings.hunter_api_key,
    }
    try:
        resp = httpx.get(
            f"{_HUNTER_BASE}/domain-search", params=params, timeout=10.0
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        emails = data.get("emails", [])

        if not emails:
            return None

        candidates = [
            e
            for e in emails
            if _is_personal_email(e.get("value", ""))
            and e.get("verification", {}).get("result") != "undeliverable"
        ]
        candidates.sort(key=lambda e: e.get("confidence", 0), reverse=True)

        if not candidates:
            return None

        best = candidates[0]
        email = best.get("value")
        confidence = best.get("confidence", 0)

        if confidence < settings.min_email_confidence:
            logger.debug("Best email {} confidence {} below threshold", email, confidence)
            return None

        return (email, confidence)

    except Exception as exc:
        logger.warning("Hunter Domain Search failed for {}: {}", domain, exc)
        return None
    finally:
        time.sleep(_RATE_LIMIT_SLEEP)
