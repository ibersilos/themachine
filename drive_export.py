"""
Google Drive export – writes daily signal reports as Google Sheets / CSV.

Uses a service account (no OAuth browser flow needed).
Falls back gracefully if credentials are missing.
"""
import csv
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_drive_service = None
_sheets_service = None


def _build_services():
    global _drive_service, _sheets_service
    if _drive_service is not None:
        return True

    creds_path: Path = config.GOOGLE_SERVICE_ACCOUNT_JSON
    if not creds_path.exists():
        logger.warning(
            "Google service account JSON not found at %s – Drive export disabled",
            creds_path,
        )
        return False

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = service_account.Credentials.from_service_account_file(
            str(creds_path), scopes=scopes
        )
        _drive_service  = build("drive",  "v3", credentials=creds, cache_discovery=False)
        _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return True
    except Exception as exc:
        logger.error("Failed to build Google API clients: %s", exc)
        return False


def _upload_csv(name: str, csv_bytes: bytes) -> str | None:
    """Upload CSV to Drive folder, return file ID."""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(csv_bytes, mimetype="text/csv", resumable=False)
    meta  = {
        "name":    name,
        "parents": [config.GOOGLE_DRIVE_FOLDER_ID] if config.GOOGLE_DRIVE_FOLDER_ID else [],
    }
    try:
        f = _drive_service.files().create(
            body=meta, media_body=media, fields="id"
        ).execute()
        return f.get("id")
    except Exception as exc:
        logger.error("Drive upload failed: %s", exc)
        return None


def export_daily_signals(signals: list[dict]) -> str | None:
    """
    Exports today's scored signals as a CSV to Google Drive.
    Returns the Drive file URL or None on failure.
    """
    if not _build_services():
        return None
    if not signals:
        logger.info("No signals to export today")
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"the-machine-signals-{today}.csv"

    # Build CSV in memory
    buf = io.StringIO()
    fieldnames = [
        "timestamp", "source", "ticker", "score", "tier",
        "flags", "filing_url", "award_amount", "insider_name",
        "transaction_value_usd", "serenity_confidence", "pe_ratio",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()

    for sig in signals:
        row = {k: sig.get(k, "") for k in fieldnames}
        # Flatten list fields
        if isinstance(row.get("flags"), list):
            row["flags"] = "; ".join(row["flags"])
        writer.writerow(row)

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM for Excel compat
    file_id = _upload_csv(filename, csv_bytes)

    if file_id:
        url = f"https://drive.google.com/file/d/{file_id}/view"
        logger.info("Signals exported to Drive: %s", url)
        return url
    return None


def export_signals_to_sheet(signals: list[dict], spreadsheet_id: str | None = None) -> str | None:
    """
    Append today's signals to a Google Sheet (creates new sheet if
    spreadsheet_id is None). Returns the spreadsheet URL.
    """
    if not _build_services():
        return None
    if not signals:
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Create spreadsheet if needed
    if not spreadsheet_id:
        try:
            ss = _sheets_service.spreadsheets().create(body={
                "properties": {"title": f"the-machine {today}"},
                "sheets": [{"properties": {"title": "Signals"}}],
            }).execute()
            spreadsheet_id = ss["spreadsheetId"]
            # Move to Drive folder
            if config.GOOGLE_DRIVE_FOLDER_ID:
                _drive_service.files().update(
                    fileId=spreadsheet_id,
                    addParents=config.GOOGLE_DRIVE_FOLDER_ID,
                    fields="id, parents",
                ).execute()
        except Exception as exc:
            logger.error("Sheet creation failed: %s", exc)
            return None

    headers = [
        "Timestamp", "Source", "Ticker", "Score", "Tier",
        "Flags", "Filing URL", "Award Amount",
        "Insider Name", "Transaction Value USD",
        "Serenity Confidence", "PE Ratio",
    ]
    rows = [headers]
    for sig in signals:
        flags = sig.get("flags", [])
        if isinstance(flags, list):
            flags = "; ".join(flags)
        rows.append([
            sig.get("timestamp", ""),
            sig.get("source", ""),
            sig.get("ticker", ""),
            sig.get("score", ""),
            sig.get("tier", ""),
            flags,
            sig.get("filing_url") or sig.get("award_url", ""),
            sig.get("award_amount", ""),
            sig.get("insider_name", ""),
            sig.get("transaction_value_usd", ""),
            sig.get("serenity_confidence", ""),
            sig.get("pe_ratio", ""),
        ])

    try:
        _sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Signals!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
    except Exception as exc:
        logger.error("Sheet append failed: %s", exc)
        return None

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    logger.info("Signals appended to Sheet: %s", url)
    return url
