from __future__ import annotations

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


def _is_personal_email(email: str) -> bool:
    prefix = email.split("@")[0].lower()
    return prefix not in _GENERIC_EMAIL_PREFIXES


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
