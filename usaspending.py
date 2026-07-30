"""
USAspending.gov API monitor.

Polls for new contract awards and grants, maps recipients to stock tickers
via yfinance company name lookup, then emits scored signal dicts.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Generator

import requests
import yfinance as yf

import config
from database import save_signal

logger = logging.getLogger(__name__)

_BASE = "https://api.usaspending.gov/api/v2"
_SEEN_IDS: set[str] = set()

# Cache nome-beneficiario -> ticker (o None). Senza questa, lo stesso
# fornitore ricorrente (comune per contratti governativi abituali) ripaga
# la stessa yf.Search di rete a ogni scan da 2h, per sempre.
_TICKER_LOOKUP_CACHE: dict[str, str | None] = {}

# Company-name fragments → ticker overrides for common defense/gov contractors
_KNOWN_CONTRACTORS: dict[str, str] = {
    "lockheed":        "LMT",
    "raytheon":        "RTX",
    "northrop":        "NOC",
    "general dynamics": "GD",
    "boeing":          "BA",
    "l3harris":        "LHX",
    "leidos":          "LDOS",
    "saic":            "SAIC",
    "booz allen":      "BAH",
    "palantir":        "PLTR",
    "anduril":         None,   # private
    "microsoft":       "MSFT",
    "amazon":          "AMZN",
    "google":          "GOOGL",
    "ibm":             "IBM",
    "oracle":          "ORCL",
    "accenture":       "ACN",
}


# Frammenti che indicano un ente governativo/municipale, mai un'azienda quotata.
# Query USAspending ordinate per importo pescano quasi solo questo genere di
# beneficiario (stato/contea/città, consorzi LLC di gestione impianti federali)
# — filtrarli qui evita chiamate yfinance sprecate e falsi "ticker non trovato".
_GOV_ENTITY_MARKERS = (
    "state of ", "city of ", "county of ", "office of ", "department of ",
    "division of ", "commonwealth of ", " county", " municipal", " township",
    "public works", "emergency management", "emergency services",
    "national security, llc", "national laboratory", "cooperative, inc.",
)


def _looks_like_gov_entity(name: str) -> bool:
    name_lower = name.lower()
    return any(marker in name_lower for marker in _GOV_ENTITY_MARKERS)


def _recipient_to_ticker(name: str) -> str | None:
    if name in _TICKER_LOOKUP_CACHE:
        return _TICKER_LOOKUP_CACHE[name]

    if _looks_like_gov_entity(name):
        _TICKER_LOOKUP_CACHE[name] = None
        return None

    name_lower = name.lower()
    for fragment, ticker in _KNOWN_CONTRACTORS.items():
        if fragment in name_lower:
            _TICKER_LOOKUP_CACHE[name] = ticker
            return ticker
    # Fallback: try yfinance search (slow, use sparingly — result cached)
    ticker = None
    try:
        results = yf.Search(name, max_results=1).quotes
        if results:
            ticker = results[0].get("symbol")
    except Exception:
        pass
    _TICKER_LOOKUP_CACHE[name] = ticker
    return ticker


def _fetch_recent_awards(days_back: int = 1, limit: int = 50) -> list[dict]:
    """Fetch recent contract awards from USAspending."""
    today = datetime.utcnow().date()
    start = (today - timedelta(days=days_back)).isoformat()

    payload = {
        "filters": {
            "time_period": [{"start_date": start, "end_date": today.isoformat()}],
            "award_type_codes": ["A", "B", "C", "D"],  # contracts only
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Awarding Agency Name", "Description",
            "Action Date", "Period of Performance Start Date",
            "Last Modified Date",
        ],
        # Ordinato per data di ultima modifica, non per importo: ordinare per
        # importo decrescente polarizza verso mega-contratti a consorzi/enti
        # non quotabili (visto nei dati reali: laboratori nazionali, enti
        # statali) invece di dare spazio a small/mid cap con contratti
        # materiali ma non giganteschi. "Action Date" non e' ordinabile
        # sull'API — "Last Modified Date" e' il campo valido piu' vicino.
        "sort": "Last Modified Date",
        "order": "desc",
        "limit": limit,
        "page": 1,
    }

    try:
        resp = requests.post(
            f"{_BASE}/search/spending_by_award/",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as exc:
        logger.warning("USAspending awards fetch failed: %s", exc)
        return []


def _fetch_recent_grants(days_back: int = 1, limit: int = 30) -> list[dict]:
    """Fetch recent grants – useful for biotech/pharma NIH/DARPA grants."""
    today = datetime.utcnow().date()
    start = (today - timedelta(days=days_back)).isoformat()

    payload = {
        "filters": {
            "time_period": [{"start_date": start, "end_date": today.isoformat()}],
            "award_type_codes": ["02", "03", "04", "05"],  # grants
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Awarding Agency Name", "Description", "Action Date",
            "Last Modified Date",
        ],
        # Vedi commento in _fetch_recent_awards: "Last Modified Date" e' il
        # campo ordinabile valido piu' vicino a "piu' recente prima".
        "sort": "Last Modified Date",
        "order": "desc",
        "limit": limit,
        "page": 1,
    }

    try:
        resp = requests.post(
            f"{_BASE}/search/spending_by_award/",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as exc:
        logger.warning("USAspending grants fetch failed: %s", exc)
        return []


def poll_awards() -> Generator[dict, None, None]:
    awards = _fetch_recent_awards() + _fetch_recent_grants()

    for award in awards:
        award_id = award.get("Award ID", "")
        if not award_id or award_id in _SEEN_IDS:
            continue
        _SEEN_IDS.add(award_id)

        recipient = award.get("Recipient Name", "")
        amount    = award.get("Award Amount") or 0
        agency    = award.get("Awarding Agency Name", "")
        desc      = award.get("Description", "")
        date      = award.get("Action Date", "")

        ticker = _recipient_to_ticker(recipient)

        signal = {
            "source":            "usaspending",
            "ticker":            ticker,
            "award_id":          award_id,
            "recipient":         recipient,
            "award_amount":      amount,
            "award_description": desc,
            "agency":            agency,
            "action_date":       date,
            "is_new_award":      True,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }

        sig_id = save_signal(
            source="usaspending",
            ticker=ticker,
            score=0,
            payload=json.dumps(signal),
        )
        signal["_db_id"] = sig_id
        yield signal


def run_once(on_signal) -> None:
    """Single pass — per GitHub Actions / run_once.py."""
    for sig in poll_awards():
        try:
            on_signal(sig)
        except Exception as exc:
            logger.error("on_signal error (usaspending): %s", exc)


def run_forever(on_signal) -> None:
    logger.info("USAspending monitor started (interval=%ds)", config.USASPENDING_POLL_INTERVAL)
    while True:
        for sig in poll_awards():
            try:
                on_signal(sig)
            except Exception as exc:
                logger.error("on_signal error (usaspending): %s", exc)
        time.sleep(config.USASPENDING_POLL_INTERVAL)
