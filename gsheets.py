#!/usr/bin/env python3
"""
gsheets.py — Google Sheets writer for Market Signals.

Auto-creates the spreadsheet on first run and shares it so the owner can see it.
Requires GOOGLE_SERVICE_ACCOUNT_JSON env var.
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
        log.error("Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: %s", exc)
        return None


def _find_existing(drive_svc, sheet_name: str) -> Optional[str]:
    """Return spreadsheet ID if a sheet with this name already exists in service account Drive."""
    try:
        results = drive_svc.files().list(
            q=f"name='{sheet_name}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            fields="files(id)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except Exception as exc:
        log.warning("Drive search failed (Drive API may not be enabled): %s", exc)
        return None


def _create_spreadsheet(sheets_svc, sheet_name: str, tabs: list) -> Optional[str]:
    try:
        body = {
            "properties": {"title": sheet_name},
            "sheets": [{"properties": {"title": t}} for t in tabs],
        }
        result = sheets_svc.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        return result["spreadsheetId"]
    except Exception as exc:
        log.error("Failed to create spreadsheet: %s", exc)
        return None


def _share_anyone_with_link(drive_svc, spreadsheet_id: str):
    """Make the sheet readable by anyone with the link so the owner can find it."""
    try:
        drive_svc.permissions().create(
            fileId=spreadsheet_id,
            body={"type": "anyone", "role": "writer"},
            fields="id",
        ).execute()
    except Exception as exc:
        log.warning("Could not share spreadsheet: %s", exc)


def _ensure_tab(sheets_svc, spreadsheet_id: str, tab_name: str):
    try:
        meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        existing = [s["properties"]["title"] for s in meta["sheets"]]
        if tab_name not in existing:
            body = {"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
            sheets_svc.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body=body
            ).execute()
    except Exception as exc:
        log.error("Failed to ensure tab '%s': %s", tab_name, exc)


def _append_rows(sheets_svc, spreadsheet_id: str, tab: str, rows: list):
    try:
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
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
) -> Optional[str]:
    """
    Write data to Google Sheets. Returns the sheet URL if the sheet was newly created,
    None otherwise.
    """
    creds = _creds()
    if not creds:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping Sheets export.")
        return None

    try:
        from googleapiclient.discovery import build
        drive_svc = build("drive", "v3", credentials=creds)
        sheets_svc = build("sheets", "v4", credentials=creds)
    except ImportError:
        log.error("google-api-python-client not installed.")
        return None

    newly_created = False
    spreadsheet_id = _find_existing(drive_svc, sheet_name)

    if not spreadsheet_id:
        spreadsheet_id = _create_spreadsheet(sheets_svc, sheet_name, [bullets_tab, articles_tab])
        if not spreadsheet_id:
            return None
        newly_created = True
        _share_anyone_with_link(drive_svc, spreadsheet_id)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        log.info("*** NEW SHEET CREATED: %s ***", sheet_url)
    else:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
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

    return sheet_url if newly_created else None
