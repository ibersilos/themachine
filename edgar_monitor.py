"""
SEC EDGAR real-time 8-K and Form-4 RSS monitor.

Polls EDGAR full-index RSS feed for new filings and emits
structured signal dicts consumed by scoring_engine.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Generator

import requests
from bs4 import BeautifulSoup

import config
from database import save_signal

logger = logging.getLogger(__name__)

_EDGAR_BASE  = "https://www.sec.gov"
_RSS_8K      = f"{_EDGAR_BASE}/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
_RSS_FORM4   = f"{_EDGAR_BASE}/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom"

_SEEN_IDS: set[str] = set()

_HEADERS = {
    "User-Agent": config.EDGAR_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# 8-K items that typically carry market-moving information
_HIGH_VALUE_ITEMS = {
    "1.01",  # Entry into material agreement
    "1.02",  # Termination of agreement
    "1.03",  # Bankruptcy / receivership
    "2.01",  # Completion of acquisition / disposal
    "2.02",  # Results of operations (earnings)
    "2.06",  # Material impairment
    "4.01",  # Changes in registrant's auditor
    "5.02",  # Director / officer departure
    "7.01",  # Regulation FD disclosure
    "8.01",  # Other events
    "9.01",  # Financial statements & exhibits
}


def _get(url: str, timeout: int = 15) -> requests.Response:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    # EDGAR rate-limit: max 10 req/s – we stay well below by sleeping in the loop
    time.sleep(0.15)
    return resp


_TICKER_CACHE: dict[str, str | None] = {}

_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"


def _parse_ticker_from_filing(filing_url: str) -> str | None:
    """Extract ticker via EDGAR submissions API (data.sec.gov) using CIK from URL."""
    if not filing_url:
        return None

    cik_match = re.search(r"/data/(\d+)/", filing_url)
    if not cik_match:
        return None
    cik_raw = cik_match.group(1)

    if cik_raw in _TICKER_CACHE:
        return _TICKER_CACHE[cik_raw]

    ticker = None
    try:
        url = f"{_SUBMISSIONS_BASE}/CIK{cik_raw.zfill(10)}.json"
        data = _get(url).json()
        tickers = data.get("tickers", [])
        if tickers:
            ticker = tickers[0].upper()
    except Exception:
        pass

    _TICKER_CACHE[cik_raw] = ticker
    return ticker


def _extract_8k_items(filing_index_url: str) -> list[str]:
    """Return list of item numbers mentioned in an 8-K filing."""
    try:
        soup = BeautifulSoup(_get(filing_index_url).text, "html.parser")
        text = soup.get_text()
        return re.findall(r"Item\s+(\d+\.\d+)", text)
    except Exception:
        return []


def _parse_rss_entries(xml_text: str) -> list[dict]:
    soup = BeautifulSoup(xml_text, "xml")
    entries = []
    for entry in soup.find_all("entry"):
        eid    = (entry.find("id") or entry.find("accession-number") or entry.find("accession_number"))
        title  = entry.find("title")
        link   = entry.find("link")
        updated = entry.find("updated")
        cik_tag = entry.find("cik") or entry.find("company-cik")

        entry_id = eid.get_text(strip=True) if eid else None
        if not entry_id or entry_id in _SEEN_IDS:
            continue

        href = link["href"] if link and link.get("href") else None
        entries.append({
            "id":       entry_id,
            "title":    title.get_text(strip=True) if title else "",
            "url":      href,
            "updated":  updated.get_text(strip=True) if updated else datetime.now(timezone.utc).isoformat(),
            "cik":      cik_tag.get_text(strip=True) if cik_tag else None,
        })
    return entries


# ── Public generators ─────────────────────────────────────────────────────────

def poll_8k() -> Generator[dict, None, None]:
    """
    Yields signal dicts for new 8-K filings.
    Call in a loop; respects EDGAR_POLL_INTERVAL.
    """
    global _SEEN_IDS
    try:
        xml = _get(_RSS_8K).text
    except Exception as exc:
        logger.warning("EDGAR 8-K poll failed: %s", exc)
        return

    entries = _parse_rss_entries(xml)
    for entry in entries:
        _SEEN_IDS.add(entry["id"])
        items = _extract_8k_items(entry["url"]) if entry["url"] else []
        high_value_items = [i for i in items if i in _HIGH_VALUE_ITEMS]

        ticker = _parse_ticker_from_filing(entry["url"]) if entry["url"] else None

        signal = {
            "source":            "edgar_8k",
            "ticker":            ticker,
            "filing_title":      entry["title"],
            "filing_url":        entry["url"],
            "items":             items,
            "high_value_items":  high_value_items,
            "cik":               entry["cik"],
            "timestamp":         entry["updated"],
            # raw score hints used by scoring_engine
            "_has_high_value":   bool(high_value_items),
            "_item_count":       len(high_value_items),
        }

        sig_id = save_signal(
            source="edgar_8k",
            ticker=ticker,
            score=0,  # will be set by scoring engine
            payload=json.dumps(signal),
        )
        signal["_db_id"] = sig_id
        yield signal


def poll_form4() -> Generator[dict, None, None]:
    """
    Yields signal dicts for new Form 4 (insider transactions) filings.
    Insider *purchases* are the bullish signal; sales are neutral/bearish.
    """
    global _SEEN_IDS
    try:
        xml = _get(_RSS_FORM4).text
    except Exception as exc:
        logger.warning("EDGAR Form-4 poll failed: %s", exc)
        return

    entries = _parse_rss_entries(xml)
    for entry in entries:
        _SEEN_IDS.add(entry["id"])

        # Skip non-Form-4 filings that EDGAR returns (e.g. 424B2, SC 13G, etc.)
        title = entry.get("title", "")
        if not re.match(r"^4[\s/]", title) and not title.startswith("4 -"):
            # Accept only filings whose type is "4" or "4/A"
            # Title format: "4 - PERSON NAME ..."  or "4/A - ..."
            if not re.search(r"^\b4(/A)?\b", title):
                logger.debug("Form-4 poll: skipping non-Form-4 entry: %s", title[:80])
                continue

        ticker = _parse_ticker_from_filing(entry["url"]) if entry["url"] else None

        signal = {
            "source":       "form4",
            "ticker":       ticker,
            "filing_title": entry["title"],
            "filing_url":   entry["url"],
            "cik":          entry["cik"],
            "timestamp":    entry["updated"],
            "_raw_title":   entry["title"],
        }

        sig_id = save_signal(
            source="form4",
            ticker=ticker,
            score=0,
            payload=json.dumps(signal),
        )
        signal["_db_id"] = sig_id
        yield signal


def run_forever(on_signal) -> None:
    """
    Blocking loop: polls 8-K and Form-4 feeds, calls on_signal(dict) for each.
    on_signal should be non-blocking (e.g. put onto a queue).
    """
    logger.info("EDGAR monitor started (interval=%ds)", config.EDGAR_POLL_INTERVAL)
    while True:
        for sig in poll_8k():
            try:
                on_signal(sig)
            except Exception as exc:
                logger.error("on_signal error (8k): %s", exc)

        for sig in poll_form4():
            try:
                on_signal(sig)
            except Exception as exc:
                logger.error("on_signal error (form4): %s", exc)

        time.sleep(config.EDGAR_POLL_INTERVAL)
