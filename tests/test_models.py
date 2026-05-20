from datetime import date

from prospect_finder.models import (
    CandidateChannel,
    ExtractionResult,
    RunStats,
    VerifiedProspect,
)


def _make_channel(**kwargs):
    defaults = dict(
        channel_id="UC123",
        name="HVAC Pro",
        subscriber_count=15_000,
        youtube_url="https://www.youtube.com/channel/UC123",
        website_url="https://hvacpro.com",
    )
    defaults.update(kwargs)
    return CandidateChannel(**defaults)


def _make_extraction(**kwargs):
    defaults = dict(
        company_name="HVAC Pro LLC",
        founder_name="John Smith",
        trade_focus_matches_target=True,
    )
    defaults.update(kwargs)
    return ExtractionResult(**defaults)


def _make_prospect(**kwargs):
    defaults = dict(
        channel=_make_channel(),
        extraction=_make_extraction(),
        run_date=date(2026, 5, 20),
        trade="hvac",
        tab="Founder Identified",
    )
    defaults.update(kwargs)
    return VerifiedProspect(**defaults)


def test_extraction_defaults():
    e = ExtractionResult()
    assert e.has_newsletter_signal is False
    assert e.has_lead_magnet_signal is False
    assert e.is_enterprise_player is False
    assert e.trade_focus_matches_target is True
    assert e.founder_name is None


def test_sheet_row_column_count():
    p = _make_prospect()
    assert len(p.to_sheet_row()) == len(VerifiedProspect.sheet_headers())


def test_sheet_row_count_is_15():
    p = _make_prospect()
    assert len(p.to_sheet_row()) == 15
    assert len(VerifiedProspect.sheet_headers()) == 15


def test_sheet_row_order():
    p = _make_prospect(
        founder_email="john@hvacpro.com",
        email_confidence_score=95,
    )
    row = p.to_sheet_row()
    assert row[0] == "2026-05-20"        # run_date
    assert row[1] == "hvac"              # trade
    assert row[2] == "HVAC Pro LLC"      # company_name
    assert row[3] == "https://hvacpro.com"  # website
    assert row[4] == "John Smith"        # founder_name
    assert row[6] == "john@hvacpro.com"  # founder_email
    assert row[7] == 95                  # email_confidence_score
    assert row[9] == 15_000              # youtube_subscriber_count
    assert row[11] == "No"               # has_newsletter_signal
    assert row[12] == "No"               # has_lead_magnet_signal
    assert row[13] == "Unknown"          # country (not set)


def test_sheet_row_newsletter_yes():
    e = _make_extraction(has_newsletter_signal=True, has_lead_magnet_signal=True)
    p = _make_prospect(extraction=e)
    row = p.to_sheet_row()
    assert row[11] == "Yes"
    assert row[12] == "Yes"


def test_run_stats_defaults():
    s = RunStats(trade="hvac")
    assert s.channels_discovered == 0
    assert s.emails_found == 0
    assert s.runtime_seconds == 0.0
