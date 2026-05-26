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
from .database import get_trade_stats, init_db, load_seen_sets, record_prospects
from .discovery import search_youtube_channels
from .extraction import batch_extract
from .sources import discover_via_serper_search, discover_via_course_search, discover_via_podcasts
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


_VALID_TRADES = [
    "hvac", "electrical", "plumbing", "gas", "welding",
    "cdl", "hgv", "heavy_vehicle", "heavy_equipment",
    "auto_mechanic", "cosmetology", "barbering", "real_estate",
    "general_contractor", "home_inspector", "solar", "cscs", "white_card",
]
_COUNTRY_CODES = {"us": "US", "ca": "CA", "uk": "GB", "au": "AU"}


@click.group()
def main():
    """Trades Exam Prep Prospect Finder."""
    pass


@main.command()
@click.option(
    "--trade",
    required=True,
    type=click.Choice(_VALID_TRADES),
    help="Trade vertical to search",
)
@click.option(
    "--country",
    required=True,
    type=click.Choice(["us", "ca", "uk", "au"]),
    help="Country to target (us, ca, uk, au)",
)
@click.option(
    "--limit",
    default=400,
    show_default=True,
    help="Max qualified prospects to write to output (all discovered candidates are extracted first)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Skip Hunter.io and Sheet/DB writes; print results to terminal",
)
def run(trade: str, country: str, limit: int, dry_run: bool) -> None:
    """Run the full prospect discovery pipeline for a given trade."""
    start_time = time.monotonic()
    settings = Settings()
    _configure_logging(settings.log_level)

    country_code = _COUNTRY_CODES[country]

    logger.info(
        "Starting prospect finder: trade={}, country={}, limit={}, dry_run={}",
        trade, country_code, limit, dry_run,
    )

    stats = RunStats(trade=trade)
    today = date.today()

    # ── Load keywords ────────────────────────────────────────────────────────
    keywords = load_keywords(trade, country)
    logger.info("Loaded {} keywords for trade='{}' country='{}'", len(keywords), trade, country_code)

    # ── Override allowed_countries to target only the requested country ──────
    settings = settings.model_copy(update={"allowed_countries": country_code})

    # ── Init DB and load dedup sets ──────────────────────────────────────────
    email_set: set[str] = set()
    website_set: set[str] = set()
    if not dry_run:
        init_db(settings.neon_database_url)
        email_set, website_set = load_seen_sets(settings.neon_database_url)

    # ── Multi-source Discovery ───────────────────────────────────────────────
    yt_candidates = search_youtube_channels(
        trade=trade,
        keywords=keywords,
        settings=settings,
        max_results_per_keyword=50,
    )
    serper_candidates = discover_via_serper_search(keywords, country_code, settings)
    course_candidates = discover_via_course_search(trade, country_code, settings)
    podcast_candidates = discover_via_podcasts(keywords, country_code)

    # Merge all sources, deduplicating by domain
    seen_domains: set[str] = set()
    raw_candidates: list = []
    for c in yt_candidates + serper_candidates + course_candidates + podcast_candidates:
        d = _extract_domain(c.website_url or "")
        if d and d not in seen_domains:
            seen_domains.add(d)
            raw_candidates.append(c)

    stats.channels_discovered = len(raw_candidates)
    logger.info(
        "Discovered {} total ({} YouTube, {} Serper, {} course, {} podcasts)",
        len(raw_candidates), len(yt_candidates), len(serper_candidates), len(course_candidates), len(podcast_candidates),
    )

    # ── Website dedup filter ─────────────────────────────────────────────────
    deduped = [
        c
        for c in raw_candidates
        if (c.website_url or "").lower() not in website_set
    ]
    stats.channels_after_website_dedup = len(deduped)
    logger.info("{} channels after website dedup", stats.channels_after_website_dedup)

    # ── Async website extraction ─────────────────────────────────────────────
    # Process up to max_candidates_per_run regardless of --limit.
    # --limit caps the number of qualified prospects written to output, not how
    # many candidates we inspect. Without this, a limit=20 run only looks at
    # 20 sites and most get filtered out as non-exam-prep, leaving 0-1 results.
    to_extract = deduped[:settings.max_candidates_per_run]
    logger.info("Processing {} candidates through extraction (limit={} output)", len(to_extract), limit)
    enriched = asyncio.run(batch_extract(to_extract, trade, country_code, settings))
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

    # ── Apply output limit after quality filtering ───────────────────────────
    trade_matched = trade_matched[:limit]

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

        # Record in Neon (single connection, batch insert)
        record_prospects(settings.neon_database_url, new_a + new_b)

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
    type=click.Choice(_VALID_TRADES),
    default=None,
    help="Filter stats to a specific trade (omit for all trades)",
)
def stats(trade: Optional[str]) -> None:
    """Read Neon and print per-trade prospect counts."""
    settings = Settings()
    _configure_logging(settings.log_level)

    trade_counts, detail = get_trade_stats(settings.neon_database_url, trade)

    click.echo("\n" + "=" * 40)
    click.echo("  Prospect Stats (from Neon)")
    click.echo("=" * 40)
    total = 0
    for row in trade_counts:
        click.echo(f"  {row[0]}: {row[1]} prospects")
        total += row[1]
    click.echo(f"  Total: {total}")
    if detail:
        click.echo(f"\n  With email:   {detail[0]}")
        click.echo(f"  With founder: {detail[1]}")
        click.echo(f"  Last run:     {detail[2]}")
    click.echo("=" * 40)
