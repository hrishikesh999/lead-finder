from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials
from loguru import logger

from .models import VerifiedProspect

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TAB_FOUNDER_IDENTIFIED = "Founder Identified"
TAB_FOUNDER_UNKNOWN = "Founder Unknown"
_ALL_TABS = [TAB_FOUNDER_IDENTIFIED, TAB_FOUNDER_UNKNOWN]


def _get_client(credentials_dict: dict) -> gspread.Client:
    creds = Credentials.from_service_account_info(credentials_dict, scopes=_SCOPES)
    return gspread.authorize(creds)


def _ensure_tabs_exist(spreadsheet: gspread.Spreadsheet) -> None:
    """Creates missing tabs and adds headers if the tab is empty. Idempotent."""
    existing_titles = {ws.title for ws in spreadsheet.worksheets()}
    headers = VerifiedProspect.sheet_headers()

    for tab_name in _ALL_TABS:
        if tab_name not in existing_titles:
            logger.info("Creating tab: {}", tab_name)
            ws = spreadsheet.add_worksheet(
                title=tab_name, rows=1000, cols=len(headers)
            )
            ws.append_row(headers)
        else:
            ws = spreadsheet.worksheet(tab_name)
            first_row = ws.row_values(1)
            if not first_row:
                ws.append_row(headers)


def append_prospect(
    sheet_id: str,
    credentials_dict: dict,
    prospect: VerifiedProspect,
    tab: str,
) -> None:
    """Appends one row to the specified tab."""
    if tab not in _ALL_TABS:
        raise ValueError(f"Invalid tab name: {tab}. Must be one of {_ALL_TABS}")

    client = _get_client(credentials_dict)
    spreadsheet = client.open_by_key(sheet_id)
    _ensure_tabs_exist(spreadsheet)
    ws = spreadsheet.worksheet(tab)
    ws.append_row(prospect.to_sheet_row(), value_input_option="USER_ENTERED")
    logger.debug("Appended row to tab '{}' for {}", tab, prospect.channel.website_url)


def append_prospects_batch(
    sheet_id: str,
    credentials_dict: dict,
    prospects: list[VerifiedProspect],
    tab: str,
) -> None:
    """Appends multiple rows to the specified tab in a single API call."""
    if not prospects:
        return
    if tab not in _ALL_TABS:
        raise ValueError(f"Invalid tab name: {tab}. Must be one of {_ALL_TABS}")

    client = _get_client(credentials_dict)
    spreadsheet = client.open_by_key(sheet_id)
    _ensure_tabs_exist(spreadsheet)
    ws = spreadsheet.worksheet(tab)
    rows = [p.to_sheet_row() for p in prospects]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    logger.info("Appended {} rows to tab '{}'", len(rows), tab)
