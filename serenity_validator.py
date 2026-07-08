"""
Serenity tweet archive validator.

Loads the yan-labs/serenity-aleabitoreddit tweet archive
(local JSON/CSV or fetched from GitHub raw) and checks whether
a ticker has been mentioned recently with bullish/bearish sentiment.

Expected archive format (JSON array):
  [
    {
      "date": "2024-03-15",
      "text": "$PLTR buy signal confirmed...",
      "sentiment": "bullish",   // optional, we also detect via keywords
      "ticker": "PLTR"          // optional, we also extract via $SYMBOL
    },
    ...
  ]
"""
import csv
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_GITHUB_RAW_URLS = [
    "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/tweets.json",
    "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/archive.json",
    "https://raw.githubusercontent.com/yan-labs/serenity-aleabitoreddit/main/data/tweets.json",
]

_BULLISH_KEYWORDS = {
    "buy", "long", "bullish", "breakout", "accumulate", "load",
    "strong buy", "target", "upside", "squeeze", "moon", "catalyst",
}
_BEARISH_KEYWORDS = {
    "sell", "short", "bearish", "dump", "avoid", "exit",
    "overvalued", "red flag", "downside",
}

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")

_archive_cache: list[dict] | None = None
_cache_loaded_at: datetime | None = None
_CACHE_TTL_HOURS = 6


def _load_archive() -> list[dict]:
    global _archive_cache, _cache_loaded_at

    now = datetime.utcnow()
    if _archive_cache is not None and _cache_loaded_at:
        if (now - _cache_loaded_at).total_seconds() < _CACHE_TTL_HOURS * 3600:
            return _archive_cache

    records = []

    # 1. Try local path first
    local = config.SERENITY_ARCHIVE_PATH
    if local.exists():
        records = _parse_file(local)
        logger.info("Serenity: loaded %d records from %s", len(records), local)

    # 2. Fall back to GitHub raw
    if not records:
        for url in _GITHUB_RAW_URLS:
            try:
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                content = resp.text
                # Try JSON first, then CSV
                try:
                    records = _normalise(json.loads(content))
                except json.JSONDecodeError:
                    records = _parse_csv_text(content)
                if records:
                    logger.info("Serenity: loaded %d records from %s", len(records), url)
                    # Cache to disk for next run
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(json.dumps(records, indent=2), encoding="utf-8")
                    break
            except Exception as exc:
                logger.debug("Serenity fetch failed %s: %s", url, exc)

    if not records:
        logger.warning("Serenity archive not found locally or on GitHub – validator disabled")

    _archive_cache = records
    _cache_loaded_at = now
    return records


def _parse_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            return _normalise(json.loads(text))
        except json.JSONDecodeError:
            pass
    return _parse_csv_text(text)


def _parse_csv_text(text: str) -> list[dict]:
    records = []
    lines = text.splitlines()
    reader = csv.DictReader(lines)
    for row in reader:
        records.append({
            "date":      row.get("date") or row.get("Date") or "",
            "text":      row.get("text") or row.get("Text") or row.get("tweet") or "",
            "ticker":    row.get("ticker") or row.get("Ticker") or "",
            "sentiment": row.get("sentiment") or row.get("Sentiment") or "",
        })
    return records


def _normalise(raw: list | dict) -> list[dict]:
    if isinstance(raw, dict):
        # Handle {tweets: [...]} or {data: [...]}
        raw = raw.get("tweets") or raw.get("data") or []
    out = []
    for item in raw:
        out.append({
            "date":      str(item.get("date") or item.get("created_at") or ""),
            "text":      str(item.get("text") or item.get("content") or ""),
            "ticker":    str(item.get("ticker") or item.get("symbol") or ""),
            "sentiment": str(item.get("sentiment") or ""),
        })
    return out


def _detect_sentiment(text: str) -> str:
    text_lower = text.lower()
    bull = sum(1 for kw in _BULLISH_KEYWORDS if kw in text_lower)
    bear = sum(1 for kw in _BEARISH_KEYWORDS if kw in text_lower)
    if bull > bear:
        return "bullish"
    if bear > bull:
        return "bearish"
    return "neutral"


def _extract_tickers(text: str) -> list[str]:
    return _TICKER_RE.findall(text)


def validate_ticker(ticker: str) -> dict:
    """
    Check the Serenity archive for mentions of `ticker`.
    Returns a dict with keys used by scoring_engine:
      - serenity_confidence (0.0-1.0)
      - serenity_recent_mention (bool)
      - serenity_sentiment (bullish/bearish/neutral)
      - serenity_mention_count (int)
      - serenity_latest_date (str or None)
    """
    archive = _load_archive()
    result = {
        "serenity_confidence":     0.0,
        "serenity_recent_mention": False,
        "serenity_sentiment":      "neutral",
        "serenity_mention_count":  0,
        "serenity_latest_date":    None,
    }

    if not archive or not ticker:
        return result

    cutoff = datetime.utcnow() - timedelta(days=config.SERENITY_RECENCY_DAYS)
    ticker_upper = ticker.upper()

    matches = []
    for record in archive:
        # Match by explicit ticker field or $TICKER in text
        rec_ticker = record.get("ticker", "").upper()
        mentioned_tickers = _extract_tickers(record.get("text", ""))
        if rec_ticker != ticker_upper and ticker_upper not in mentioned_tickers:
            continue
        matches.append(record)

    if not matches:
        return result

    result["serenity_mention_count"] = len(matches)

    # Determine recency and latest date
    dates = []
    for m in matches:
        date_str = m.get("date", "")
        try:
            d = datetime.fromisoformat(date_str[:10])
            dates.append(d)
        except ValueError:
            pass

    if dates:
        latest = max(dates)
        result["serenity_latest_date"] = latest.date().isoformat()
        if latest >= cutoff:
            result["serenity_recent_mention"] = True

    # Sentiment: majority vote across all matches
    sentiments = []
    for m in matches:
        s = m.get("sentiment", "").lower()
        if s not in ("bullish", "bearish", "neutral"):
            s = _detect_sentiment(m.get("text", ""))
        sentiments.append(s)

    bull_count = sentiments.count("bullish")
    bear_count = sentiments.count("bearish")
    total = len(sentiments)

    if total > 0:
        if bull_count > bear_count:
            result["serenity_sentiment"] = "bullish"
        elif bear_count > bull_count:
            result["serenity_sentiment"] = "bearish"
        # confidence: proportion of majority sentiment, scaled by recency bonus
        majority = max(bull_count, bear_count)
        confidence = majority / total
        if result["serenity_recent_mention"]:
            confidence = min(confidence * 1.25, 1.0)
        result["serenity_confidence"] = round(confidence, 3)

    return result


def enrich_signal(signal: dict) -> dict:
    """Attach Serenity data to a signal dict in place."""
    ticker = signal.get("ticker")
    if not ticker:
        return signal
    serenity = validate_ticker(ticker)
    signal.update(serenity)
    return signal
