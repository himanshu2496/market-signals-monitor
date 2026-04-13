#!/usr/bin/env python3
"""
gsheets.py
Google Sheets writer for Market Signals.

Automatically creates a spreadsheet named "Market Signals" on first run
(owned by the service account), then appends rows to two tabs:
  - "Action Bullets"  — one row per run: timestamp + AI-generated bullets
  - "Articles"        — one row per article: date, company, title, URL, source,
                        relevance score, relevance reason

The spreadsheet ID is printed to logs on creation so you can find it at:
  https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}

To share the sheet with yourself:
  Open the URL above → Share → add your email

Requires:
  - GOOGLE_SERVICE_ACCOUNT_JSON env var (full JSON contents of service account key)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

SPREADSHEET_NAME = "Market Signals"
BULLETS_SHEET = "Action Bullets"
ARTICLES_SHEET = "Articles"

BULLETS_HEADERS = ["Timestamp (UTC)", "Mode", "Bullet Points"]
ARTICLES_HEADERS = [
    "Timestamp (UTC)", "Company", "Title", "URL",
    "Source", "Signal Type", "Relevance Score", "Relevance Reason", "Published At",
]


def _get_service():
    """
    Return an authenticated Google Sheets + Drive service tuple, or (None, None).
    Reads credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var.
    """
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        log.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — skipping Google Sheets export.")
        return None, None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_dict = json.loads(raw)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        sheets = build("sheets", "v4", credentials=creds)
        drive = build("drive", "v3", credentials=creds)
        return sheets, drive
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install google-api-python-client google-auth")
        return None, None
    except Exception as exc:
        log.error("Failed to initialise Google Sheets service: %s", exc)
        return None, None


def _get_or_create_spreadsheet(sheets, drive) -> Optional[str]:
    """
    Find existing 'Market Signals' spreadsheet or create a new one.
    Returns the spreadsheet ID.
    """
    # Search Drive for existing sheet
    try:
        results = drive.files().list(
            q=f"name='{SPREADSHEET_NAME}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
            fields="files(id, name)",
        ).execute()
        files = results.get("files", [])
        if files:
            sheet_id = files[0]["id"]
            log.info("Found existing spreadsheet: %s", sheet_id)
            return sheet_id
    except Exception as exc:
        log.error("Drive search failed: %s", exc)
        return None

    # Create new spreadsheet with both tabs
    try:
        body = {
            "properties": {"title": SPREADSHEET_NAME},
            "sheets": [
                {"properties": {"title": BULLETS_SHEET}},
                {"properties": {"title": ARTICLES_SHEET}},
            ],
        }
        result = sheets.spreadsheets().create(body=body).execute()
        sheet_id = result["spreadsheetId"]
        log.info(
            "Created new spreadsheet: https://docs.google.com/spreadsheets/d/%s",
            sheet_id,
        )

        # Write headers to both tabs
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{BULLETS_SHEET}!A1",
            valueInputOption="RAW",
            body={"values": [BULLETS_HEADERS]},
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{ARTICLES_SHEET}!A1",
            valueInputOption="RAW",
            body={"values": [ARTICLES_HEADERS]},
        ).execute()

        # Bold the header rows
        sheet_meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                   for s in sheet_meta["sheets"]}
        requests = []
        for tab_name in [BULLETS_SHEET, ARTICLES_SHEET]:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": tab_ids[tab_name],
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
                            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            })
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": requests},
        ).execute()

        return sheet_id
    except Exception as exc:
        log.error("Failed to create spreadsheet: %s", exc)
        return None


def append_bullets(summary: str, mode: str) -> bool:
    """
    Append one row to the 'Action Bullets' tab.
    Returns True on success.
    """
    sheets, drive = _get_service()
    if sheets is None:
        return False

    sheet_id = _get_or_create_spreadsheet(sheets, drive)
    if sheet_id is None:
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    row = [now, mode.upper(), summary]

    try:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{BULLETS_SHEET}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        log.info("Appended action bullets to Google Sheet.")
        return True
    except Exception as exc:
        log.error("Failed to append bullets: %s", exc)
        return False


def append_articles(company_results: list, mode: str) -> bool:
    """
    Append one row per article to the 'Articles' tab.
    company_results: list of (company_name, articles_list) tuples.
    Returns True on success.
    """
    sheets, drive = _get_service()
    if sheets is None:
        return False

    sheet_id = _get_or_create_spreadsheet(sheets, drive)
    if sheet_id is None:
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for company, articles in company_results:
        for article in articles:
            pub_at = article.get("published_at")
            pub_str = pub_at.strftime("%Y-%m-%d %H:%M UTC") if pub_at else ""
            rows.append([
                now,
                company,
                article.get("title", ""),
                article.get("url", ""),
                article.get("source", ""),
                article.get("signal_type", "news"),
                article.get("relevance_score", ""),
                article.get("relevance_reason", ""),
                pub_str,
            ])

    if not rows:
        return True

    try:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{ARTICLES_SHEET}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        log.info("Appended %d article rows to Google Sheet.", len(rows))
        return True
    except Exception as exc:
        log.error("Failed to append articles: %s", exc)
        return False
