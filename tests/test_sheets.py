from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from prospect_finder.models import CandidateChannel, ExtractionResult, VerifiedProspect
from prospect_finder.sheets import TAB_FOUNDER_IDENTIFIED, TAB_FOUNDER_UNKNOWN, append_prospect


def _make_prospect(tab=TAB_FOUNDER_IDENTIFIED, **kwargs):
    channel = CandidateChannel(
        channel_id="UC123",
        name="HVAC Pro",
        subscriber_count=15_000,
        youtube_url="https://www.youtube.com/channel/UC123",
        website_url="https://hvacpro.com",
    )
    extraction = ExtractionResult(
        company_name="HVAC Pro LLC",
        founder_name="John Smith",
        trade_focus_matches_target=True,
    )
    defaults = dict(
        channel=channel,
        extraction=extraction,
        founder_email="john@hvacpro.com",
        email_confidence_score=92,
        run_date=date(2026, 5, 20),
        trade="hvac",
        tab=tab,
    )
    defaults.update(kwargs)
    return VerifiedProspect(**defaults)


def test_append_prospect_calls_append_row():
    """append_prospect should call ws.append_row with the correct number of columns."""
    mock_ws = MagicMock()
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws
    mock_spreadsheet.worksheets.return_value = [
        MagicMock(title=TAB_FOUNDER_IDENTIFIED),
        MagicMock(title=TAB_FOUNDER_UNKNOWN),
    ]
    mock_client = MagicMock()
    mock_client.open_by_key.return_value = mock_spreadsheet

    with patch("prospect_finder.sheets._get_client", return_value=mock_client):
        prospect = _make_prospect()
        append_prospect("sheet-id", {}, prospect, TAB_FOUNDER_IDENTIFIED)

    mock_ws.append_row.assert_called_once()
    args = mock_ws.append_row.call_args[0][0]
    assert len(args) == 15


def test_append_prospect_invalid_tab():
    with pytest.raises(ValueError, match="Invalid tab name"):
        append_prospect("sheet-id", {}, _make_prospect(), "Invalid Tab")


def test_sheet_headers_count():
    assert len(VerifiedProspect.sheet_headers()) == 15


def test_to_sheet_row_matches_headers():
    p = _make_prospect()
    assert len(p.to_sheet_row()) == len(VerifiedProspect.sheet_headers())
