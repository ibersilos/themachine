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
        info = yf.Ticker(ticker).info
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
    }

    _cache[ticker] = (time.time(), data)
    logger.debug("Fundamentals cached for %s: pe=%.1f rev_growth=%.2f",
                 ticker,
                 data["pe_ratio"] or 0,
                 data["revenue_growth"] or 0)
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
