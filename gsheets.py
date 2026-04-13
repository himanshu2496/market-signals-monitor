#!/usr/bin/env python3
"""
gsheets.py — Google Sheets writer for Market Signals.

Exports action bullets and raw article data to a Google Sheet.
Sheet is created automatically if it doesn't exist.

Requires GOOGLE_SERVICE_ACCOUNT_JSON env var (JSON string of service account credentials).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _creds():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        from google.oauth2.service_account import Credentials
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception as exc:
        log.error("Failed to load service account credentials: %s", exc)
        return None


def _get_or_create_spreadsheet(drive_svc, sheets_svc, sheet_name: str, tabs: list) -> Optional[str]:
    """Return spreadsheet ID, creating it if necessary."""
    try:
        results = drive_svc.files().list(
            q=f"name='{sheet_name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            fields="files(id, name)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
    except Exception as exc:
        log.error("Drive search failed: %s", exc)
        return None

    try:
        body = {
            "properties": {"title": sheet_name},
            "sheets": [{"properties": {"title": tab}} for tab in tabs],
        }
        result = sheets_svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        spreadsheet_id = result["spreadsheetId"]
        log.info("Created spreadsheet '%s' (%s)", sheet_name, spreadsheet_id)
        return spreadsheet_id
    except Exception as exc:
        log.error("Failed to create spreadsheet: %s", exc)
        return None


def _ensure_tab(sheets_svc, spreadsheet_id: str, tab_name: str):
    """Add tab if it doesn't already exist."""
    try:
        meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        existing = [s["properties"]["title"] for s in meta["sheets"]]
        if tab_name not in existing:
            body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
            sheets_svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body=body
            ).execute()
            log.info("Created tab '%s'", tab_name)
    except Exception as exc:
        log.error("Failed to ensure tab '%s': %s", tab_name, exc)


def _append_rows(sheets_svc, spreadsheet_id: str, tab: str, rows: list):
    try:
        body = {"values": rows}
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()
    except Exception as exc:
        log.error("Failed to append to '%s': %s", tab, exc)


def write_to_sheets(
    sheet_name: str,
    bullets_tab: str,
    articles_tab: str,
    summary: Optional[str],
    articles: list,
    source_label: str = "",
):
    """Write action bullets and articles to Google Sheets. No-op if credentials missing."""
    creds = _creds()
    if not creds:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping Sheets export.")
        return

    try:
        from googleapiclient.discovery import build
        drive_svc = build("drive", "v3", credentials=creds)
        sheets_svc = build("sheets", "v4", credentials=creds)
    except ImportError:
        log.error("google-api-python-client not installed.")
        return

    spreadsheet_id = _get_or_create_spreadsheet(
        drive_svc, sheets_svc, sheet_name, [bullets_tab, articles_tab]
    )
    if not spreadsheet_id:
        return

    _ensure_tab(sheets_svc, spreadsheet_id, bullets_tab)
    _ensure_tab(sheets_svc, spreadsheet_id, articles_tab)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if summary:
        _append_rows(sheets_svc, spreadsheet_id, bullets_tab, [[now, source_label, summary]])
        log.info("Wrote action bullets to Sheets.")

    if articles:
        rows = [
            [
                now,
                source_label,
                a.get("company", ""),
                a.get("title", ""),
                a.get("url", ""),
                a.get("source", ""),
                a.get("published", ""),
                str(a.get("relevance_score", "")),
                a.get("relevance_reason", ""),
            ]
            for a in articles
        ]
        _append_rows(sheets_svc, spreadsheet_id, articles_tab, rows)
        log.info("Wrote %d articles to Sheets.", len(rows))
