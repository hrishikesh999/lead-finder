from __future__ import annotations

import psycopg2
from loguru import logger

from .models import VerifiedProspect

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS prospects (
    id SERIAL PRIMARY KEY,
    run_date DATE NOT NULL,
    trade TEXT NOT NULL,
    company_name TEXT,
    website TEXT,
    founder_name TEXT,
    founder_role TEXT,
    founder_email TEXT,
    email_confidence_score INTEGER,
    youtube_channel_url TEXT,
    youtube_subscriber_count INTEGER,
    team_size_signal TEXT,
    has_newsletter_signal BOOLEAN,
    has_lead_magnet_signal BOOLEAN,
    country TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_EMAIL_IDX = "CREATE INDEX IF NOT EXISTS prospects_email_idx ON prospects (LOWER(founder_email));"
_CREATE_WEBSITE_IDX = "CREATE INDEX IF NOT EXISTS prospects_website_idx ON prospects (LOWER(website));"

_INSERT_SQL = """
INSERT INTO prospects (
    run_date, trade, company_name, website, founder_name, founder_role,
    founder_email, email_confidence_score, youtube_channel_url,
    youtube_subscriber_count, team_size_signal, has_newsletter_signal,
    has_lead_magnet_signal, country, notes
) VALUES (
    %(run_date)s, %(trade)s, %(company_name)s, %(website)s, %(founder_name)s,
    %(founder_role)s, %(founder_email)s, %(email_confidence_score)s,
    %(youtube_channel_url)s, %(youtube_subscriber_count)s, %(team_size_signal)s,
    %(has_newsletter_signal)s, %(has_lead_magnet_signal)s, %(country)s, %(notes)s
)
ON CONFLICT DO NOTHING;
"""


def _connect(conn_string: str):
    return psycopg2.connect(conn_string)


def init_db(conn_string: str) -> None:
    """Creates the prospects table and indexes if they don't exist. Idempotent."""
    with _connect(conn_string) as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            cur.execute(_CREATE_EMAIL_IDX)
            cur.execute(_CREATE_WEBSITE_IDX)
        conn.commit()
    logger.debug("Database initialized")


def load_seen_sets(conn_string: str) -> tuple[set[str], set[str]]:
    """
    Returns (email_set, website_set) of all previously recorded prospects.
    Both sets are lowercase-normalized for case-insensitive dedup.
    """
    email_set: set[str] = set()
    website_set: set[str] = set()

    with _connect(conn_string) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT LOWER(founder_email), LOWER(website) FROM prospects;")
            for row in cur.fetchall():
                email, website = row
                if email:
                    email_set.add(email)
                if website:
                    website_set.add(website)

    logger.info(
        "Loaded {} existing emails and {} existing websites from Neon",
        len(email_set),
        len(website_set),
    )
    return email_set, website_set


def record_prospect(conn_string: str, prospect: VerifiedProspect) -> None:
    """Inserts a verified prospect into Neon. ON CONFLICT DO NOTHING is a safety net."""
    c = prospect.channel
    e = prospect.extraction
    params = {
        "run_date": prospect.run_date,
        "trade": prospect.trade,
        "company_name": e.company_name or c.name,
        "website": c.website_url,
        "founder_name": e.founder_name,
        "founder_role": e.founder_role,
        "founder_email": prospect.founder_email,
        "email_confidence_score": prospect.email_confidence_score,
        "youtube_channel_url": c.youtube_url,
        "youtube_subscriber_count": c.subscriber_count,
        "team_size_signal": e.team_size_signal,
        "has_newsletter_signal": e.has_newsletter_signal,
        "has_lead_magnet_signal": e.has_lead_magnet_signal,
        "country": c.country or "Unknown",
        "notes": e.notes,
    }
    with _connect(conn_string) as conn:
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL, params)
        conn.commit()
    logger.debug("Recorded prospect in Neon: {}", c.website_url)
