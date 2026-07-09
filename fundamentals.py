"""
Fundamental data enrichment via yfinance.

Fetches PE ratio, revenue growth, debt/equity and other metrics
for a given ticker. Results are cached in-process for FUNDAMENTALS_CACHE_TTL
seconds to avoid hammering Yahoo Finance.
"""
import logging
import time
from typing import Optional

import yfinance as yf

import config

logger = logging.getLogger(__name__)

# In-process cache: {ticker: (fetched_at, data_dict)}
_cache: dict[str, tuple[float, dict]] = {}


def _is_fresh(ticker: str) -> bool:
    if ticker not in _cache:
        return False
    fetched_at, _ = _cache[ticker]
    return (time.time() - fetched_at) < config.FUNDAMENTALS_CACHE_TTL


def _fetch_total_oi(yf_ticker) -> float | None:
    """Somma dell'open interest della prima scadenza disponibile (proxy liquidità opzioni)."""
    try:
        dates = yf_ticker.options
        if not dates:
            return None
        chain = yf_ticker.option_chain(dates[0])
        oi = (
            (chain.calls["openInterest"].sum() if "openInterest" in chain.calls.columns else 0)
            + (chain.puts["openInterest"].sum() if "openInterest" in chain.puts.columns else 0)
        )
        return float(oi) if oi else None
    except Exception:
        return None


def get_fundamentals(ticker: str) -> dict:
    """
    Return a dict of fundamental fields for `ticker`.
    Keys match what scoring_engine._score_fundamentals() expects:
      pe_ratio, revenue_growth, debt_to_equity,
      market_cap, profit_margins, quick_ratio, beta
    Returns empty dict on failure.
    """
    if not ticker:
        return {}

    if _is_fresh(ticker):
        return _cache[ticker][1]

    try:
        yf_obj = yf.Ticker(ticker)
        info = yf_obj.info
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return {}

    data = {
        "pe_ratio":        _safe_float(info.get("trailingPE") or info.get("forwardPE")),
        "revenue_growth":  _safe_float(info.get("revenueGrowth")),
        "debt_to_equity":  _safe_float(info.get("debtToEquity")),
        "market_cap":      _safe_float(info.get("marketCap")),
        "profit_margins":  _safe_float(info.get("profitMargins")),
        "quick_ratio":     _safe_float(info.get("quickRatio")),
        "beta":            _safe_float(info.get("beta")),
        "short_name":      info.get("shortName") or info.get("longName") or ticker,
        "sector":          info.get("sector", ""),
        "industry":        info.get("industry", ""),
        "current_price":   _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
        "52w_high":        _safe_float(info.get("fiftyTwoWeekHigh")),
        "52w_low":         _safe_float(info.get("fiftyTwoWeekLow")),
        "avg_volume":      _safe_float(info.get("averageVolume") or info.get("averageDailyVolume10Day")),
        "open_interest":   _fetch_total_oi(yf_obj),
        "iv_rank":         None,   # non disponibile da yfinance — richiede IBKR
    }

    _cache[ticker] = (time.time(), data)
    logger.debug("Fundamentals cached for %s: pe=%.1f rev_growth=%.2f vol=%.0f",
                 ticker,
                 data["pe_ratio"] or 0,
                 data["revenue_growth"] or 0,
                 data["avg_volume"] or 0)
    return data


def _safe_float(val) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def enrich_signal(signal: dict) -> dict:
    """Attach fundamentals data to a signal dict in place."""
    ticker = signal.get("ticker")
    if not ticker:
        return signal
    fundamentals = get_fundamentals(ticker)
    signal.update(fundamentals)
    return signal


def current_price(ticker: str) -> float | None:
    """Quick price lookup – used by the kill-switch stop loss check."""
    data = get_fundamentals(ticker)
    if data.get("current_price"):
        return data["current_price"]
    # Force fresh fetch if cached price is stale
    try:
        fast = yf.Ticker(ticker).fast_info
        return float(fast.last_price)
    except Exception:
        return None
