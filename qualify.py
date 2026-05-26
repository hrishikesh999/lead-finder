#!/usr/bin/env python3
"""
qualify.py — BuiltWith prospect qualifier for ActiveCampaign and Keap users.

Reads two BuiltWith CSVs, filters junk, deduplicates against an existing
Google Sheet, samples N rows, qualifies each with Claude Haiku, and writes
approved prospects to the sheet.

Usage:
    python qualify.py [--sample-size N] [--include-org]

Configuration (set in .env or as environment variables):
    ANTHROPIC_API_KEY              — required
    GOOGLE_SHEET_ID                — required
    GOOGLE_SHEETS_CREDENTIALS_JSON — required (minified JSON blob)
    PROSPECT_TAB_NAME              — sheet tab name (default: Prospects)
    INPUT_FOLDER                   — folder with CSVs (default: input)
    DEFAULT_SAMPLE_SIZE            — rows per run (default: 500)
    MAX_FETCH_CONCURRENCY          — parallel homepage fetches (default: 10)
    MAX_CLAUDE_CONCURRENCY         — parallel Claude calls (default: 5)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

import anthropic
import gspread
import httpx
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials


# ── INLINE .env LOADER ────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# ── CONFIGURATION ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "{}")
TAB_NAME = os.environ.get("PROSPECT_TAB_NAME", "Prospects")
INPUT_FOLDER = Path(os.environ.get("INPUT_FOLDER", "input"))
DEFAULT_SAMPLE_SIZE = int(os.environ.get("DEFAULT_SAMPLE_SIZE", "500"))
MAX_FETCH_CONCURRENCY = int(os.environ.get("MAX_FETCH_CONCURRENCY", "10"))
MAX_CLAUDE_CONCURRENCY = int(os.environ.get("MAX_CLAUDE_CONCURRENCY", "5"))

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
FETCH_TIMEOUT = 10
MAX_TEXT_CHARS = 4000

# Haiku pricing approximation: $0.80/M input, $4.00/M output
_COST_PER_INPUT_TOKEN = 0.80 / 1_000_000
_COST_PER_OUTPUT_TOKEN = 4.00 / 1_000_000


# ── FILTER CONSTANTS ──────────────────────────────────────────────────────────

_BLOCKED_TLDS = frozenset((".gov", ".edu", ".mil", ".org"))
_FREE_SUBDOMAINS = (
    "wixsite.com",
    "weebly.com",
    "godaddysites.com",
    "squarespace.com",
    "wordpress.com",
    "blogspot.com",
    "tumblr.com",
    "mystrikingly.com",
    "webflow.io",
    "carrd.co",
    "notion.site",
    "webador.com",
    "jimdosite.com",
    "yolasite.com",
)
_REDFLAG_KEYWORDS = (
    "mlm", "crypto", "casino", "gambling",
    "adult", "xxx", "forex", "binary-option",
    "poker", "betting", "cannabis", "dispensary",
    "escort", "porn",
)
_VALID_COUNTRIES = frozenset({
    "united states", "us", "usa", "u.s.", "u.s.a.",
    "canada", "ca", "can",
})


# ── SHEET SCHEMA ──────────────────────────────────────────────────────────────

SHEET_HEADERS = [
    "Processed date",
    "Platform",
    "Domain",
    "Company name",
    "Founder name",
    "Founder role",
    "Offer summary",
    "Country",
    "Status",
    "Email",
]

_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


# ── CLAUDE PROMPT ─────────────────────────────────────────────────────────────

_HAIKU_PROMPT = """\
You are screening business homepages to find prospects for a copywriting agency that writes email sequences and sales copy for small and mid-size businesses.

Homepage text:
<homepage>
{text}
</homepage>

Return a JSON object with exactly these fields and no other text:
{{
  "company_name": "Business name in plain English (often differs from domain)",
  "founder_name": "First Last if visible on THIS page, else empty string",
  "founder_role": "Title like Founder/CEO/Owner if visible on THIS page, else empty string",
  "offer_summary": "One sentence: what does this business sell? Be specific and concrete. Example: \\"Helps real estate agents close more deals through email-driven lead nurture.\\"",
  "country": "US or Canada or other/unclear — infer from currency, phone format, address, or language on the page",
  "is_real_business": true or false,
  "rejection_reason": "Empty string if is_real_business is true. Otherwise one of: parked domain / no visible offer / personal blog / MLM or network marketing / copywriting or marketing agency / large enterprise / religious or political org / outside US-Canada / non-English / other"
}}

Set is_real_business to TRUE only when ALL three conditions are met:
1. The page shows a real business actively selling a product, service, or program
2. Email list activity is visible: a subscribe form, lead magnet, newsletter CTA, or opt-in offer
3. The business appears small or mid-size — NOT a large corporation, enterprise software company, or brand with hundreds of employees

Set is_real_business to FALSE (and fill in rejection_reason) if ANY of these are true:
- Parked domain, under construction, or dead/empty site
- Personal blog or portfolio with no product or service for sale
- MLM, network marketing, or direct-sales distributor page (an individual rep, not the brand)
- The business IS a copywriting agency, marketing agency, PR firm, email marketing consultant, or similar — they are competitors, not prospects
- Large enterprise, Fortune 500, or company that clearly has a substantial in-house team
- Church, religious organisation, political organisation, or nonprofit
- Content is primarily non-English or the business clearly serves outside US/Canada

When uncertain, set is_real_business to false. Return ONLY the JSON object, no markdown.\
"""


# ── CSV READING ───────────────────────────────────────────────────────────────

def _find_col(headers: list[str], *candidates: str) -> Optional[str]:
    lower = {h.strip().lower(): h for h in headers}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def _strip_protocol(domain: str) -> str:
    domain = re.sub(r"^https?://", "", domain.strip().lower())
    return domain.split("/")[0].rstrip(".")


def read_csv(path: Path, platform: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        domain_col = _find_col(headers, "domain", "website", "url", "site")
        country_col = _find_col(headers, "country", "location", "region")

        if not domain_col:
            print(f"  WARNING: No domain column found in {path.name}")
            print(f"  Available columns: {headers}")
            return rows

        for row in reader:
            domain = _strip_protocol(row.get(domain_col, ""))
            if not domain:
                continue
            country = row.get(country_col, "").strip() if country_col else ""
            rows.append({"domain": domain, "country": country, "platform": platform})

    return rows


# ── DOMAIN FILTERING ──────────────────────────────────────────────────────────

def _tld(domain: str) -> str:
    parts = domain.split(".")
    return "." + parts[-1] if len(parts) >= 2 else ""


def _passes_domain_filter(domain: str, include_org: bool) -> bool:
    tld = _tld(domain)
    blocked = _BLOCKED_TLDS if not include_org else _BLOCKED_TLDS - {".org"}
    if tld in blocked:
        return False
    for pattern in _FREE_SUBDOMAINS:
        if domain == pattern or domain.endswith("." + pattern):
            return False
    for kw in _REDFLAG_KEYWORDS:
        if kw in domain:
            return False
    return True


def _valid_country(country: str) -> bool:
    if not country.strip():
        return True  # unknown country — let Claude decide
    return country.strip().lower() in _VALID_COUNTRIES


def apply_filters(rows: list[dict], include_org: bool) -> tuple[list[dict], int]:
    kept, dropped = [], 0
    for row in rows:
        if not _valid_country(row["country"]):
            dropped += 1
            continue
        if not _passes_domain_filter(row["domain"], include_org):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────

def _sheet_client() -> gspread.Client:
    creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=_GOOGLE_SCOPES)
    return gspread.authorize(creds)


def get_existing_domains(sheet_id: str, tab_name: str) -> set[str]:
    try:
        client = _sheet_client()
        spreadsheet = client.open_by_key(sheet_id)
        try:
            ws = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            return set()
        all_values = ws.get_all_values()
        if not all_values:
            return set()
        headers = [h.strip() for h in all_values[0]]
        try:
            domain_idx = headers.index("Domain")
        except ValueError:
            return set()
        return {
            row[domain_idx].strip().lower()
            for row in all_values[1:]
            if len(row) > domain_idx and row[domain_idx].strip()
        }
    except Exception as exc:
        print(f"  WARNING: Could not read existing sheet ({exc}); proceeding without dedup.")
        return set()


def append_row_to_sheet(sheet_id: str, tab_name: str, prospect: dict) -> None:
    client = _sheet_client()
    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=5000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)
    row = [prospect.get(h, "") for h in SHEET_HEADERS]
    ws.append_row(row, value_input_option="USER_ENTERED")


# ── HTML TEXT EXTRACTION ──────────────────────────────────────────────────────

def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "head", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TEXT_CHARS]


# ── CLAUDE HAIKU CALL ─────────────────────────────────────────────────────────

async def call_claude(
    client: anthropic.AsyncAnthropic,
    text: str,
    semaphore: asyncio.Semaphore,
) -> tuple[Optional[dict], int, int]:
    """Returns (parsed_json, input_tokens, output_tokens). json is None on error."""
    prompt = _HAIKU_PROMPT.format(text=text)
    async with semaphore:
        try:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            in_tok = response.usage.input_tokens
            out_tok = response.usage.output_tokens

            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw.rstrip())

            return json.loads(raw), in_tok, out_tok
        except json.JSONDecodeError:
            return None, 0, 0
        except Exception:
            return None, 0, 0


# ── PER-ROW PROCESSING ────────────────────────────────────────────────────────

_FAKE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def process_row(
    row: dict,
    fetch_sem: asyncio.Semaphore,
    claude_sem: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
    claude_client: anthropic.AsyncAnthropic,
) -> tuple[Optional[dict], str, int, int]:
    """
    Returns (prospect_dict, status, input_tokens, output_tokens).
    status is one of: "kept" | "fetch_failed" | "claude_error" | "dropped:<reason>"
    """
    domain = row["domain"]
    url = f"https://{domain}"

    # Fetch homepage
    async with fetch_sem:
        try:
            resp = await http_client.get(url, timeout=FETCH_TIMEOUT)
            html = resp.text
        except Exception:
            return None, "fetch_failed", 0, 0

    text = extract_text(html)
    if not text:
        return None, "fetch_failed", 0, 0

    # Claude qualification
    result, in_tok, out_tok = await call_claude(claude_client, text, claude_sem)

    if result is None:
        return None, "claude_error", 0, 0

    if not result.get("is_real_business", False):
        reason = (result.get("rejection_reason") or "unknown").strip()
        return None, f"dropped:{reason}", in_tok, out_tok

    country_raw = (result.get("country") or "other/unclear").strip()
    if country_raw.lower() not in ("us", "canada"):
        return None, "dropped:outside US/Canada", in_tok, out_tok

    prospect = {
        "Processed date": date.today().isoformat(),
        "Platform": row["platform"],
        "Domain": domain,
        "Company name": result.get("company_name", ""),
        "Founder name": result.get("founder_name", ""),
        "Founder role": result.get("founder_role", ""),
        "Offer summary": result.get("offer_summary", ""),
        "Country": country_raw,
        "Status": "needs review",
        "Email": "",
    }
    return prospect, "kept", in_tok, out_tok


# ── CONCURRENT ORCHESTRATION ──────────────────────────────────────────────────

async def run_async(
    rows: list[dict],
    sheet_id: str,
    tab_name: str,
) -> dict:
    fetch_sem = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)
    claude_sem = asyncio.Semaphore(MAX_CLAUDE_CONCURRENCY)
    write_lock = asyncio.Lock()

    stats: dict = {
        "kept": 0,
        "fetch_failed": 0,
        "claude_error": 0,
        "dropped": defaultdict(int),
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }

    total = len(rows)
    completed = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": _FAKE_UA},
        follow_redirects=True,
    ) as http_client:
        claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        async def handle(row: dict) -> None:
            nonlocal completed

            prospect, status, in_tok, out_tok = await process_row(
                row, fetch_sem, claude_sem, http_client, claude_client
            )

            stats["total_input_tokens"] += in_tok
            stats["total_output_tokens"] += out_tok

            if status == "kept" and prospect:
                # Serialize sheet writes; gspread is not thread-safe
                async with write_lock:
                    try:
                        await asyncio.to_thread(
                            append_row_to_sheet, sheet_id, tab_name, prospect
                        )
                        stats["kept"] += 1
                    except Exception as exc:
                        print(f"\n  WARNING: Sheet write failed for {row['domain']}: {exc}")
            elif status == "fetch_failed":
                stats["fetch_failed"] += 1
            elif status == "claude_error":
                stats["claude_error"] += 1
            elif status.startswith("dropped:"):
                reason = status[len("dropped:"):]
                stats["dropped"][reason] += 1

            completed += 1
            if completed % 10 == 0 or completed == total:
                print(f"  Progress: {completed}/{total}  ", end="\r", flush=True)

        await asyncio.gather(*[handle(row) for row in rows])

    print()  # newline after progress line
    return stats


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Qualify BuiltWith AC/Keap prospects into a Google Sheet."
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        metavar="N",
        help=f"Rows to sample per run (default: {DEFAULT_SAMPLE_SIZE})",
    )
    parser.add_argument(
        "--include-org",
        action="store_true",
        help="Include .org domains (excluded by default)",
    )
    args = parser.parse_args()

    # Validate required config
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID")
    if GOOGLE_SHEETS_CREDENTIALS_JSON in ("{}", ""):
        missing.append("GOOGLE_SHEETS_CREDENTIALS_JSON")
    if missing:
        print(f"ERROR: Missing required config: {', '.join(missing)}")
        print("Set them in a .env file or as environment variables.")
        sys.exit(1)

    start_time = time.time()
    print("=" * 60)
    print("BuiltWith Prospect Qualifier")
    print("=" * 60)

    # Step 1: Read CSVs
    print("\nStep 1: Reading CSVs...")
    ac_path = INPUT_FOLDER / "activecampaign.csv"
    keap_path = INPUT_FOLDER / "keap.csv"

    if not ac_path.exists() and not keap_path.exists():
        print(f"ERROR: No CSVs found in {INPUT_FOLDER}/")
        print("Expected: activecampaign.csv and/or keap.csv")
        sys.exit(1)

    all_rows: list[dict] = []
    for path, platform in [(ac_path, "ActiveCampaign"), (keap_path, "Keap")]:
        if path.exists():
            rows = read_csv(path, platform)
            print(f"  {path.name}: {len(rows):,} rows")
            all_rows.extend(rows)
        else:
            print(f"  {path.name}: not found, skipping")

    total_read = len(all_rows)
    print(f"  Combined: {total_read:,} rows")

    # Step 2: Domain filters
    print("\nStep 2: Applying domain filters...")
    filtered_rows, dropped_filter = apply_filters(all_rows, include_org=args.include_org)
    print(f"  Dropped: {dropped_filter:,}")
    print(f"  Remaining: {len(filtered_rows):,}")

    # Step 3: Dedup against sheet
    print("\nStep 3: Deduplicating against existing sheet...")
    existing_domains = get_existing_domains(GOOGLE_SHEET_ID, TAB_NAME)
    print(f"  Existing domains in sheet: {len(existing_domains):,}")
    before_dedup = len(filtered_rows)
    filtered_rows = [r for r in filtered_rows if r["domain"] not in existing_domains]
    skipped_dedup = before_dedup - len(filtered_rows)
    print(f"  Skipped (already in sheet): {skipped_dedup:,}")
    print(f"  Remaining: {len(filtered_rows):,}")

    # Step 4: Sample
    print(f"\nStep 4: Sampling up to {args.sample_size:,} rows...")
    if len(filtered_rows) <= args.sample_size:
        sample = filtered_rows
        if len(filtered_rows) < args.sample_size:
            print(
                f"  WARNING: Only {len(filtered_rows):,} rows available "
                f"(requested {args.sample_size:,})"
            )
    else:
        sample = random.sample(filtered_rows, args.sample_size)
    print(f"  Sample size: {len(sample):,}")

    if not sample:
        print("\nNothing to process. All eligible domains may already be in the sheet.")
        sys.exit(0)

    # Step 5: Process
    print(
        f"\nStep 5: Processing {len(sample):,} rows "
        f"({MAX_FETCH_CONCURRENCY} fetch / {MAX_CLAUDE_CONCURRENCY} Claude concurrent)..."
    )
    print()

    stats = asyncio.run(run_async(sample, GOOGLE_SHEET_ID, TAB_NAME))

    # Summary
    elapsed = time.time() - start_time
    cost = (
        stats["total_input_tokens"] * _COST_PER_INPUT_TOKEN
        + stats["total_output_tokens"] * _COST_PER_OUTPUT_TOKEN
    )
    total_dropped_claude = sum(stats["dropped"].values())

    print("\n" + "=" * 60)
    print("RUN SUMMARY")
    print("=" * 60)
    print(f"  Rows read from CSVs:          {total_read:>7,}")
    print(f"  Dropped by domain filters:    {dropped_filter:>7,}")
    print(f"  Skipped (in sheet already):   {skipped_dedup:>7,}")
    print(f"  Rows sampled:                 {len(sample):>7,}")
    print(f"  Homepage fetch failures:      {stats['fetch_failed']:>7,}")
    if stats["claude_error"]:
        print(f"  Claude errors (bad JSON):     {stats['claude_error']:>7,}")
    print(f"  Dropped by Claude:            {total_dropped_claude:>7,}")
    for reason, count in sorted(stats["dropped"].items(), key=lambda x: -x[1]):
        print(f"    - {reason}: {count}")
    print(f"  Written to sheet:             {stats['kept']:>7,}")
    print(f"  Est. Claude API spend:        ${cost:>8.4f}")
    print(f"  Total runtime:                {elapsed:>6.0f}s  ({elapsed / 60:.1f} min)")
    print("=" * 60)


if __name__ == "__main__":
    main()
