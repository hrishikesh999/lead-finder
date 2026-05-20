from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel


class CandidateChannel(BaseModel):
    """Raw data harvested from YouTube APIs — no enrichment yet."""

    channel_id: str
    name: str
    subscriber_count: int
    country: Optional[str] = None
    website_url: Optional[str] = None
    youtube_url: str
    description_snippet: Optional[str] = None


class ExtractionResult(BaseModel):
    """Structured output from Claude after analyzing the candidate's website."""

    company_name: Optional[str] = None
    founder_name: Optional[str] = None
    founder_role: Optional[str] = None
    team_size_signal: Optional[str] = None  # solo | small | mid | enterprise | unknown
    has_newsletter_signal: bool = False
    has_lead_magnet_signal: bool = False
    is_enterprise_player: bool = False
    trade_focus_matches_target: bool = True
    notes: Optional[str] = None
    extraction_confidence: Optional[str] = None  # high | medium | low


class EnrichedCandidate(BaseModel):
    """CandidateChannel after website extraction."""

    channel: CandidateChannel
    extraction: ExtractionResult
    pages_fetched: int = 1


class VerifiedProspect(BaseModel):
    """Final record ready to be written to Google Sheets and Neon."""

    channel: CandidateChannel
    extraction: ExtractionResult
    founder_email: Optional[str] = None
    email_confidence_score: Optional[int] = None
    run_date: date
    trade: str
    tab: str  # "Founder Identified" or "Founder Unknown"

    @classmethod
    def sheet_headers(cls) -> list[str]:
        return [
            "run_date",
            "trade",
            "company_name",
            "website",
            "founder_name",
            "founder_role",
            "founder_email",
            "email_confidence_score",
            "youtube_channel_url",
            "youtube_subscriber_count",
            "team_size_signal",
            "has_newsletter_signal",
            "has_lead_magnet_signal",
            "country",
            "notes",
        ]

    def to_sheet_row(self) -> list:
        """Returns values in the exact column order for the Google Sheet."""
        c = self.channel
        e = self.extraction
        return [
            self.run_date.isoformat(),
            self.trade,
            e.company_name or c.name,
            c.website_url or "",
            e.founder_name or "",
            e.founder_role or "",
            self.founder_email or "",
            self.email_confidence_score or "",
            c.youtube_url,
            c.subscriber_count,
            e.team_size_signal or "",
            "Yes" if e.has_newsletter_signal else "No",
            "Yes" if e.has_lead_magnet_signal else "No",
            c.country or "Unknown",
            e.notes or "",
        ]


class RunStats(BaseModel):
    """Counters accumulated during a pipeline run for the summary printout."""

    trade: str
    channels_discovered: int = 0
    channels_after_website_dedup: int = 0
    channels_extracted: int = 0
    channels_dropped_enterprise: int = 0
    channels_dropped_trade_mismatch: int = 0
    bucket_a_count: int = 0
    bucket_b_count: int = 0
    emails_found: int = 0
    emails_dropped_low_confidence: int = 0
    emails_dropped_duplicate: int = 0
    rows_written_founder_identified: int = 0
    rows_written_founder_unknown: int = 0
    runtime_seconds: float = 0.0
