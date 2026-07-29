"""
SEC Form 4 (Statement of Changes in Beneficial Ownership) deep parser.

Goes beyond the RSS feed: fetches the actual XML filing to extract
transaction type, shares, price, and insider role.
Only Purchase (P) transactions generate positive signals.
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

_EDGAR_BASE = "https://www.sec.gov"
_HEADERS = {
    "User-Agent": config.EDGAR_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# Insider roles that carry the strongest conviction signal
_HIGH_CONVICTION_ROLES = {
    "Chief Executive Officer", "CEO",
    "Chief Financial Officer", "CFO",
    "Chief Operating Officer", "COO",
    "President", "Chairman",
    "Director",
}

_SEEN_ACCESSIONS: set[str] = set()


def _get(url: str, timeout: int = 15) -> requests.Response:
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    time.sleep(0.15)
    return resp


def _parse_form4_xml(xml_text: str) -> dict:
    """
    Extract key fields from a Form 4 XML document.
    Returns a dict with transaction details.
    """
    soup = BeautifulSoup(xml_text, "xml")
    result: dict = {
        "transaction_type":      None,
        "transaction_shares":    None,
        "price_per_share":       None,
        "transaction_value_usd": None,
        "insider_name":          None,
        "insider_title":         None,
        "is_high_conviction":    False,
        "ownership_post":        None,
        "transaction_date":      None,
        "issuer_ticker":         None,
        "issuer_cik":            None,
        "issuer_name":           None,
    }

    # Issuer identity — authoritative source for ticker resolution.
    # The RSS entry URL's CIK can belong to ANY party on a jointly-filed
    # Form 4 (issuer or any reporting owner), so it must not be trusted
    # for ticker lookup. The <issuer> block inside the XML itself is
    # the same regardless of which party's URL was used to fetch it.
    issuer = soup.find("issuer")
    if issuer:
        cik_tag = issuer.find("issuerCik")
        symbol_tag = issuer.find("issuerTradingSymbol")
        name_tag = issuer.find("issuerName")
        result["issuer_cik"] = cik_tag.get_text(strip=True) if cik_tag else None
        symbol = symbol_tag.get_text(strip=True) if symbol_tag else None
        result["issuer_ticker"] = symbol.upper() if symbol else None
        result["issuer_name"] = name_tag.get_text(strip=True) if name_tag else None

    # Insider identity
    rp = soup.find("reportingOwner")
    if rp:
        name = rp.find("rptOwnerName")
        result["insider_name"] = name.get_text(strip=True) if name else None

        title_tag = soup.find("officerTitle")
        if title_tag:
            title = title_tag.get_text(strip=True)
            result["insider_title"] = title
            result["is_high_conviction"] = any(
                role.lower() in title.lower() for role in _HIGH_CONVICTION_ROLES
            )

    # Non-derivative transactions (stock purchases/sales)
    for txn in soup.find_all("nonDerivativeTransaction"):
        code_tag = txn.find("transactionCode")
        if not code_tag:
            continue
        code = code_tag.get_text(strip=True)

        shares_tag = txn.find("transactionShares")
        price_tag  = txn.find("transactionPricePerShare") or txn.find("pricePerShare")
        date_tag   = txn.find("transactionDate")
        post_tag   = txn.find("sharesOwnedFollowingTransaction")

        shares = float(shares_tag.find("value").get_text()) if shares_tag and shares_tag.find("value") else None
        price  = float(price_tag.find("value").get_text())  if price_tag  and price_tag.find("value")  else None
        date   = date_tag.find("value").get_text(strip=True) if date_tag and date_tag.find("value") else None
        post   = float(post_tag.find("value").get_text())   if post_tag   and post_tag.find("value")  else None

        # Take the first non-derivative transaction (usually the primary one)
        result["transaction_type"]      = code
        result["transaction_shares"]    = shares
        result["price_per_share"]       = price
        result["transaction_date"]      = date
        result["ownership_post"]        = post

        if shares and price:
            result["transaction_value_usd"] = round(shares * price, 2)

        break  # first nonDerivativeTransaction is the primary one

    return result


def _resolve_filing_xml(index_url: str) -> str | None:
    """Find the .xml Form 4 document from the filing index page."""
    try:
        soup = BeautifulSoup(_get(index_url).text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.endswith(".xml") and "xsl" not in href.lower():
                return _EDGAR_BASE + href
    except Exception as exc:
        logger.debug("Could not resolve Form 4 XML: %s", exc)
    return None


def _ticker_from_issuer_cik(cik: str) -> str | None:
    """Look up ticker for an issuer CIK via EDGAR company facts API."""
    if not cik:
        return None
    try:
        url = f"{_EDGAR_BASE}/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=1&search_text=&output=atom"
        xml = _get(url).text
        soup = BeautifulSoup(xml, "xml")
        ticker_tag = soup.find("ticker-symbol") or soup.find("tickerSymbol")
        if ticker_tag:
            return ticker_tag.get_text(strip=True).upper()
    except Exception:
        pass
    return None


def _insider_cluster_count(ticker: str, days: int = 7) -> int:
    """
    Conta quanti altri segnali form4 sullo stesso ticker sono arrivati
    negli ultimi N giorni — piu' insider che comprano in cluster e' un
    segnale piu' forte di un acquisto isolato.
    """
    if not ticker:
        return 0
    try:
        from database import _conn
        conn = _conn()
        row = conn.execute(
            "SELECT COUNT(DISTINCT id) FROM signals "
            "WHERE source='form4' AND ticker=? AND created_at >= datetime('now', ?)",
            (ticker, f"-{days} days"),
        ).fetchone()
        return row[0] if row else 0
    except Exception as exc:
        logger.debug("_insider_cluster_count(%s): %s", ticker, exc)
        return 0


def enrich_form4_signal(raw_signal: dict) -> dict:
    """
    Takes a raw Form-4 signal from edgar_monitor and enriches it
    with parsed transaction data from the actual XML filing.
    Returns the enriched signal (mutated in place + returned).

    Always writes the enriched payload back to the DB row (if the raw
    signal carries a _db_id) regardless of purchase/sale outcome, so the
    persisted record always reflects what scoring actually used — the
    prior behaviour left the DB row frozen at its pre-enrichment stub,
    making backtesting impossible even for signals that did score.
    """
    url = raw_signal.get("filing_url")
    if not url:
        return raw_signal

    accession = raw_signal.get("id") or url
    if accession in _SEEN_ACCESSIONS:
        return raw_signal
    _SEEN_ACCESSIONS.add(accession)

    xml_url = _resolve_filing_xml(url)
    if not xml_url:
        return raw_signal

    try:
        xml_text = _get(xml_url).text
        parsed = _parse_form4_xml(xml_text)
        raw_signal.update(parsed)

        # Issuer ticker from the XML itself is authoritative — prefer it
        # over whatever the RSS-entry-URL-based guess produced upstream,
        # since that guess can resolve to any party on a joint filing.
        if parsed.get("issuer_ticker"):
            raw_signal["ticker"] = parsed["issuer_ticker"]

        if raw_signal.get("ticker") and parsed.get("transaction_type") == "P":
            raw_signal["insider_cluster_count"] = _insider_cluster_count(raw_signal["ticker"])

        sig_id = raw_signal.get("_db_id")
        if sig_id:
            from database import tx as db_tx
            with db_tx() as conn:
                conn.execute(
                    "UPDATE signals SET payload=?, ticker=? WHERE id=?",
                    (json.dumps({k: v for k, v in raw_signal.items() if not k.startswith("_")}),
                     raw_signal.get("ticker"), sig_id),
                )
    except Exception as exc:
        logger.warning("Form 4 XML parse failed for %s: %s", url, exc)

    return raw_signal


def poll_form4_deep(raw_signals: list[dict]) -> Generator[dict, None, None]:
    """
    Enrich a list of raw Form-4 signals from edgar_monitor with
    full transaction detail. Only yields Purchase (P) transactions.
    """
    for sig in raw_signals:
        enriched = enrich_form4_signal(sig)
        tx_type = enriched.get("transaction_type")

        # Skip everything that's not a purchase
        if tx_type and tx_type != "P":
            logger.debug(
                "Form-4 skipped (type=%s, ticker=%s)",
                tx_type, enriched.get("ticker"),
            )
            continue

        # Update persisted score=0 signal with enriched payload
        sig_id = enriched.get("_db_id")
        if sig_id:
            from database import tx as db_tx
            with db_tx() as conn:
                conn.execute(
                    "UPDATE signals SET payload=?, ticker=? WHERE id=?",
                    (json.dumps(enriched), enriched.get("ticker"), sig_id),
                )

        yield enriched
