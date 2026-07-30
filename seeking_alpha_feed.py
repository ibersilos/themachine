"""
Seeking Alpha per-ticker RSS feed — sostituisce Serenity (fonte morta/rischiosa).

Feed ufficiale, uso personale/non-commerciale (vedi termini nel canale RSS):
  https://seekingalpha.com/api/sa/combined/{TICKER}.xml

Copre anche i micro/small cap (verificato su DLPN), a differenza dei tool
tematici IBKR che coprono solo le large cap.
"""
import logging
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_FEED_URL = "https://seekingalpha.com/api/sa/combined/{ticker}.xml"

# Pattern per estrarre un earnings surprise dal titolo, es:
# "X GAAP EPS of -$0.22 misses by $0.12, revenue of $12.8M misses by $0.76M"
_EPS_RE = re.compile(r"EPS of \$?(-?[\d.]+)\s+(misses|beats)\s+by\s+\$?([\d.]+)", re.IGNORECASE)

_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 3600  # 1h — le notizie non cambiano ogni minuto


def _is_fresh(ticker: str) -> bool:
    if ticker not in _cache:
        return False
    fetched_at, _ = _cache[ticker]
    return (time.time() - fetched_at) < _CACHE_TTL


def get_recent_news(ticker: str, max_items: int = 10) -> dict:
    """
    Ritorna: {
        "sa_item_count": int,
        "sa_latest_headline": str | None,
        "sa_latest_date": str | None,   # ISO
        "sa_days_since_latest": int | None,
        "sa_eps_surprise": "beat" | "miss" | None,
    }
    Dict vuoto/default su errore o ticker senza copertura.
    """
    empty = {
        "sa_item_count": 0,
        "sa_latest_headline": None,
        "sa_latest_date": None,
        "sa_days_since_latest": None,
        "sa_eps_surprise": None,
    }
    if not ticker:
        return empty

    if _is_fresh(ticker):
        return _cache[ticker][1]

    try:
        resp = requests.get(_FEED_URL.format(ticker=ticker), headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        items = soup.find_all("item")[:max_items]
    except Exception as exc:
        logger.debug("seeking_alpha_feed %s: %s", ticker, exc)
        return empty

    if not items:
        _cache[ticker] = (time.time(), empty)
        return empty

    latest = items[0]
    title = latest.find("title").get_text(strip=True) if latest.find("title") else None
    pub_date_tag = latest.find("pubDate")
    latest_date = None
    days_since = None
    if pub_date_tag:
        try:
            dt = datetime.strptime(pub_date_tag.get_text(strip=True), "%a, %d %b %Y %H:%M:%S %z")
            latest_date = dt.isoformat()
            days_since = (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass

    eps_surprise = None
    for item in items:
        t = item.find("title").get_text(strip=True) if item.find("title") else ""
        m = _EPS_RE.search(t)
        if m:
            eps_surprise = "beat" if m.group(2).lower() == "beats" else "miss"
            break

    result = {
        "sa_item_count": len(items),
        "sa_latest_headline": title,
        "sa_latest_date": latest_date,
        "sa_days_since_latest": days_since,
        "sa_eps_surprise": eps_surprise,
    }
    _cache[ticker] = (time.time(), result)
    return result


def enrich_signal(signal: dict) -> dict:
    """Attach Seeking Alpha news data to a signal dict in place."""
    ticker = signal.get("ticker")
    if not ticker:
        return signal
    signal.update(get_recent_news(ticker))
    return signal
