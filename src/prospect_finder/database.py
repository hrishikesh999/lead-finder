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
    website TEXT UNIQUE,
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
ON CONFLICT (website) DO NOTHING;
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


def _prospect_to_params(prospect: VerifiedProspect) -> dict:
    c = prospect.channel
    e = prospect.extraction
    return {
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


def record_prospects(conn_string: str, prospects: list[VerifiedProspect]) -> None:
    """Batch-inserts verified prospects into Neon in a single connection."""
    if not prospects:
        return
    with _connect(conn_string) as conn:
        with conn.cursor() as cur:
            for prospect in prospects:
                cur.execute(_INSERT_SQL, _prospect_to_params(prospect))
        conn.commit()
    logger.info("Recorded {} prospects in Neon", len(prospects))


def get_trade_stats(
    conn_string: str, trade: str | None = None
) -> tuple[list[tuple], tuple | None]:
    """
    Returns (trade_counts, detail) where:
    - trade_counts: list of (trade, count) rows
    - detail: (with_email, with_founder, last_run) for the filtered trade, or None
    """
    with _connect(conn_string) as conn:
        with conn.cursor() as cur:
            if trade:
                cur.execute(
                    "SELECT trade, COUNT(*) FROM prospects WHERE trade = %s GROUP BY trade;",
                    (trade,),
                )
            else:
                cur.execute(
                    "SELECT trade, COUNT(*) FROM prospects GROUP BY trade ORDER BY trade;"
                )
            trade_counts = cur.fetchall()

            detail = None
            if trade:
                cur.execute(
                    """
                    SELECT
                        SUM(CASE WHEN founder_email IS NOT NULL THEN 1 ELSE 0 END),
                        SUM(CASE WHEN founder_name IS NOT NULL THEN 1 ELSE 0 END),
                        MAX(run_date)
                    FROM prospects WHERE trade = %s;
                    """,
                    (trade,),
                )
                detail = cur.fetchone()

    return trade_counts, detail
