from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from typing import Optional
from urllib.parse import urlparse

import click
from loguru import logger

from .config import Settings, load_keywords
from .database import init_db, load_seen_sets, record_prospect
from .discovery import search_youtube_channels
from .extraction import batch_extract
from .models import RunStats, VerifiedProspect
from .sheets import (
    TAB_FOUNDER_IDENTIFIED,
    TAB_FOUNDER_UNKNOWN,
    append_prospects_batch,
)
from .verification import find_email_for_domain, find_email_for_founder


def _configure_logging(level: str) -> None:
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), colorize=True)


def _extract_domain(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or None
    except Exception:
        return None


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


@click.group()
def main():
    """Trades Exam Prep Prospect Finder."""
    pass


@main.command()
@click.option(
    "--trade",
    required=True,
    type=click.Choice(["hvac", "electrical", "plumbing", "cdl"]),
    help="Trade vertical to search",
)
@click.option(
    "--limit",
    default=400,
    show_default=True,
    help="Max candidates to process",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Skip Hunter.io and Sheet/DB writes; print results to terminal",
)
def run(trade: str, limit: int, dry_run: bool) -> None:
    """Run the full prospect discovery pipeline for a given trade."""
    start_time = time.monotonic()
    settings = Settings()
    _configure_logging(settings.log_level)

    logger.info(
        "Starting prospect finder: trade={}, limit={}, dry_run={}", trade, limit, dry_run
    )

    stats = RunStats(trade=trade)
    today = date.today()

    # ── Load keywords ────────────────────────────────────────────────────────
    keywords = load_keywords(trade)
    logger.info("Loaded {} keywords for trade '{}'", len(keywords), trade)

    # ── Init DB and load dedup sets ──────────────────────────────────────────
    email_set: set[str] = set()
    website_set: set[str] = set()
    if not dry_run:
        init_db(settings.neon_database_url)
        email_set, website_set = load_seen_sets(settings.neon_database_url)

    # ── YouTube Discovery ────────────────────────────────────────────────────
    raw_candidates = search_youtube_channels(
        trade=trade,
        keywords=keywords,
        settings=settings,
        max_results_per_keyword=50,
    )
    stats.channels_discovered = len(raw_candidates)
    logger.info("Discovered {} channels", stats.channels_discovered)

    # ── Website dedup filter ─────────────────────────────────────────────────
    deduped = [
        c
        for c in raw_candidates
        if (c.website_url or "").lower() not in website_set
    ]
    stats.channels_after_website_dedup = len(deduped)
    logger.info("{} channels after website dedup", stats.channels_after_website_dedup)

    # ── Apply limit ──────────────────────────────────────────────────────────
    limited = deduped[:limit]

    # ── Async website extraction ─────────────────────────────────────────────
    enriched = asyncio.run(batch_extract(limited, trade, settings))
    stats.channels_extracted = len(enriched)

    # ── Filter enterprise players ────────────────────────────────────────────
    non_enterprise = []
    for ec in enriched:
        if ec.extraction.is_enterprise_player or ec.extraction.team_size_signal == "enterprise":
            stats.channels_dropped_enterprise += 1
            logger.debug("Dropping enterprise: {}", ec.channel.name)
        else:
            non_enterprise.append(ec)

    # ── Filter trade focus mismatch ──────────────────────────────────────────
    trade_matched = []
    for ec in non_enterprise:
        if not ec.extraction.trade_focus_matches_target:
            stats.channels_dropped_trade_mismatch += 1
            logger.debug("Dropping trade mismatch: {}", ec.channel.name)
        else:
            trade_matched.append(ec)

    # ── Split into buckets ───────────────────────────────────────────────────
    bucket_a = [ec for ec in trade_matched if ec.extraction.founder_name]
    bucket_b = [ec for ec in trade_matched if not ec.extraction.founder_name]
    stats.bucket_a_count = len(bucket_a)
    stats.bucket_b_count = len(bucket_b)
    logger.info(
        "Bucket A (founder known): {}, Bucket B (founder unknown): {}",
        len(bucket_a),
        len(bucket_b),
    )

    # ── Email verification ───────────────────────────────────────────────────
    verified_a: list[VerifiedProspect] = []
    verified_b: list[VerifiedProspect] = []

    for ec in bucket_a:
        domain = _extract_domain(ec.channel.website_url or "")
        if not domain:
            continue

        email_result = None
        if not dry_run:
            first, last = _split_name(ec.extraction.founder_name)
            email_result = find_email_for_founder(domain, first, last, settings)
            if email_result:
                stats.emails_found += 1
            else:
                stats.emails_dropped_low_confidence += 1

        email, confidence = email_result if email_result else (None, None)
        verified_a.append(
            VerifiedProspect(
                channel=ec.channel,
                extraction=ec.extraction,
                founder_email=email,
                email_confidence_score=confidence,
                run_date=today,
                trade=trade,
                tab=TAB_FOUNDER_IDENTIFIED,
            )
        )

    for ec in bucket_b:
        domain = _extract_domain(ec.channel.website_url or "")
        if not domain:
            continue

        email_result = None
        if not dry_run:
            email_result = find_email_for_domain(domain, settings)
            if email_result:
                stats.emails_found += 1

        email, confidence = email_result if email_result else (None, None)
        verified_b.append(
            VerifiedProspect(
                channel=ec.channel,
                extraction=ec.extraction,
                founder_email=email,
                email_confidence_score=confidence,
                run_date=today,
                trade=trade,
                tab=TAB_FOUNDER_UNKNOWN,
            )
        )

    # ── Dedup check + write ──────────────────────────────────────────────────
    if dry_run:
        all_verified = verified_a + verified_b
        for prospect in all_verified:
            click.echo(json.dumps(prospect.model_dump(mode="json"), indent=2))
    else:
        def _should_write(prospect: VerifiedProspect) -> bool:
            email_key = (prospect.founder_email or "").lower()
            website_key = (prospect.channel.website_url or "").lower()
            if email_key and email_key in email_set:
                stats.emails_dropped_duplicate += 1
                logger.debug("Duplicate email {}, skipping", email_key)
                return False
            if website_key in website_set:
                stats.emails_dropped_duplicate += 1
                logger.debug("Duplicate website {}, skipping", website_key)
                return False
            if email_key:
                email_set.add(email_key)
            if website_key:
                website_set.add(website_key)
            return True

        new_a = [p for p in verified_a if _should_write(p)]
        new_b = [p for p in verified_b if _should_write(p)]

        # Batch write to Google Sheets
        if new_a:
            append_prospects_batch(
                settings.google_sheet_id,
                settings.credentials_dict,
                new_a,
                TAB_FOUNDER_IDENTIFIED,
            )
        if new_b:
            append_prospects_batch(
                settings.google_sheet_id,
                settings.credentials_dict,
                new_b,
                TAB_FOUNDER_UNKNOWN,
            )

        # Record in Neon
        for prospect in new_a + new_b:
            record_prospect(settings.neon_database_url, prospect)

        stats.rows_written_founder_identified = len(new_a)
        stats.rows_written_founder_unknown = len(new_b)

    stats.runtime_seconds = time.monotonic() - start_time
    _print_summary(stats, dry_run)


def _print_summary(stats: RunStats, dry_run: bool) -> None:
    click.echo("\n" + "=" * 52)
    click.echo(f"  Prospect Finder Run Summary — {stats.trade.upper()}")
    click.echo("=" * 52)
    click.echo(f"  Channels discovered:           {stats.channels_discovered}")
    click.echo(f"  After website dedup:           {stats.channels_after_website_dedup}")
    click.echo(f"  Extracted successfully:        {stats.channels_extracted}")
    click.echo(f"  Dropped (enterprise):          {stats.channels_dropped_enterprise}")
    click.echo(f"  Dropped (trade mismatch):      {stats.channels_dropped_trade_mismatch}")
    click.echo(f"  Bucket A (founder known):      {stats.bucket_a_count}")
    click.echo(f"  Bucket B (founder unknown):    {stats.bucket_b_count}")
    click.echo(f"  Emails found:                  {stats.emails_found}")
    click.echo(f"  Dropped (low confidence):      {stats.emails_dropped_low_confidence}")
    click.echo(f"  Dropped (duplicate):           {stats.emails_dropped_duplicate}")
    if not dry_run:
        click.echo(f"  Rows → Founder Identified:     {stats.rows_written_founder_identified}")
        click.echo(f"  Rows → Founder Unknown:        {stats.rows_written_founder_unknown}")
    click.echo(f"  Runtime:                       {stats.runtime_seconds:.1f}s")
    if dry_run:
        click.echo("  [DRY RUN — no writes performed]")
    click.echo("=" * 52)


@main.command()
@click.option(
    "--trade",
    required=False,
    type=click.Choice(["hvac", "electrical", "plumbing", "cdl"]),
    default=None,
    help="Filter stats to a specific trade (omit for all trades)",
)
def stats(trade: Optional[str]) -> None:
    """Read Neon and print per-trade prospect counts."""
    settings = Settings()
    _configure_logging(settings.log_level)

    import psycopg2

    with psycopg2.connect(settings.neon_database_url) as conn:
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
            rows = cur.fetchall()

            # Also get tab breakdown
            if trade:
                cur.execute(
                    """
                    SELECT
                        SUM(CASE WHEN founder_email IS NOT NULL THEN 1 ELSE 0 END) AS with_email,
                        SUM(CASE WHEN founder_name IS NOT NULL THEN 1 ELSE 0 END) AS with_founder,
                        MAX(run_date) AS last_run
                    FROM prospects WHERE trade = %s;
                    """,
                    (trade,),
                )
                detail = cur.fetchone()
            else:
                detail = None

    click.echo("\n" + "=" * 40)
    click.echo("  Prospect Stats (from Neon)")
    click.echo("=" * 40)
    total = 0
    for row in rows:
        click.echo(f"  {row[0]}: {row[1]} prospects")
        total += row[1]
    click.echo(f"  Total: {total}")
    if detail:
        click.echo(f"\n  With email:   {detail[0]}")
        click.echo(f"  With founder: {detail[1]}")
        click.echo(f"  Last run:     {detail[2]}")
    click.echo("=" * 40)
