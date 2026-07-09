"""
Scoring engine – converts raw signal dicts into 0-100 scores.

Due pipeline distinte:
  STOCK_PICKING   → form4 + usaspending  → tag [PICK]  🎯
  WHEEL_CANDIDATE → edgar_8k + serenity  → tag [WHEEL] ⚙️

Ogni pipeline ha filtri dedicati; segnali fuori range vengono scartati
silenziosamente (filtered=True) senza alert Telegram.
"""
import logging
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

# Mapping source → pipeline
_PIPELINE_MAP: dict[str, str] = {
    "form4":       "stock_picking",
    "usaspending": "stock_picking",
    "edgar_8k":    "wheel_candidate",
    "serenity":    "wheel_candidate",
}

# ── Weights (must sum to 1.0) ─────────────────────────────────────────────────
WEIGHTS = {
    "edgar_8k":    0.30,
    "form4":       0.25,
    "usaspending": 0.20,
    "serenity":    0.15,
    "fundamentals": 0.10,
}


@dataclass
class ScoreBreakdown:
    ticker: str | None
    pipeline: str = "unknown"      # stock_picking | wheel_candidate
    total: int = 0
    components: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    filtered: bool = False         # True → segnale scartato — nessun alert

    def tier(self) -> str:
        if self.total >= config.STRONG_BUY_THRESHOLD:
            return "STRONG BUY"
        if self.total >= config.MIN_ALERT_SCORE:
            return "BUY ALERT"
        return "WATCH"

    def emoji(self) -> str:
        t = self.tier()
        return {"STRONG BUY": "🔥", "BUY ALERT": "📡", "WATCH": "👁"}.get(t, "")

    def pipeline_tag(self) -> str:
        if self.pipeline == "stock_picking":
            return "[PICK] 🎯"
        if self.pipeline == "wheel_candidate":
            return "[WHEEL] ⚙️"
        return ""


# ── Pipeline filters ──────────────────────────────────────────────────────────

def _filter_stock_picking(signal: dict, ticker: str | None) -> bool:
    """Ritorna True (= scartare) se il segnale non soddisfa i criteri PICK."""
    t = ticker or "—"

    cap = signal.get("market_cap")
    if cap is not None:
        if cap < config.PICK_CAP_MIN:
            logger.info("[PICK] %s scartata: cap $%.0fM < min $%.0fM (micro cap)",
                        t, cap / 1e6, config.PICK_CAP_MIN / 1e6)
            return True
        if cap > config.PICK_CAP_MAX:
            logger.info("[PICK] %s scartata: cap $%.0fB > max $%.0fM",
                        t, cap / 1e9, config.PICK_CAP_MAX / 1e6)
            return True

    price = signal.get("current_price")
    if price is not None:
        if price < config.PICK_PRICE_MIN or price > config.PICK_PRICE_MAX:
            logger.info("[PICK] %s scartata: prezzo $%.2f fuori range $%.0f-$%.0f",
                        t, price, config.PICK_PRICE_MIN, config.PICK_PRICE_MAX)
            return True

    vol = signal.get("avg_volume")
    if vol is not None and vol < config.PICK_VOL_MIN:
        logger.info("[PICK] %s scartata: volume %.0f/day < min %.0f",
                    t, vol, config.PICK_VOL_MIN)
        return True

    return False


def _filter_wheel_candidate(signal: dict, ticker: str | None) -> bool:
    """Ritorna True (= scartare) se il segnale non soddisfa i criteri WHEEL."""
    t = ticker or "—"

    cap = signal.get("market_cap")
    if cap is not None and cap < config.WHEEL_CAP_MIN:
        logger.info("[WHEEL] %s scartata: cap $%.0fM < min $%.0fB",
                    t, cap / 1e6, config.WHEEL_CAP_MIN / 1e9)
        return True

    oi = signal.get("open_interest")
    if oi is not None and oi < config.WHEEL_OI_MIN:
        logger.info("[WHEEL] %s scartata: OI %.0f < min %.0f",
                    t, oi, config.WHEEL_OI_MIN)
        return True

    iv_rank = signal.get("iv_rank")
    if iv_rank is not None and iv_rank < config.WHEEL_IV_RANK_MIN:
        logger.info("[WHEEL] %s scartata: IV rank %.1f%% < min %.0f%%",
                    t, iv_rank, config.WHEEL_IV_RANK_MIN)
        return True

    return False


# ── Per-source scorers ────────────────────────────────────────────────────────

def _score_edgar_8k(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []

    hv = signal.get("high_value_items", [])
    count = len(hv)

    if count >= 3:
        score += 70
        flags.append(f"8-K: {count} high-value items")
    elif count >= 2:
        score += 50
        flags.append(f"8-K: {count} high-value items")
    elif count == 1:
        score += 35
        flags.append(f"8-K item {hv[0]}")
    elif signal.get("_has_high_value"):
        score += 20
        flags.append("8-K: high-value item detected")

    if "2.02" in hv:
        score = min(score + 20, 100)
        flags.append("Earnings report (2.02)")

    if "1.03" in hv:
        score = max(score - 40, 0)
        flags.append("⚠️ Bankruptcy/receivership (1.03)")

    return min(score, 100), flags


def _score_form4(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []
    title = signal.get("filing_title", "").lower()

    tx_type = signal.get("transaction_type", "")
    amount  = signal.get("transaction_value_usd", 0) or 0

    is_purchase = (tx_type == "P") or ("purchase" in title and "sale" not in title)
    is_sale     = (tx_type == "S") or "sale" in title

    if is_purchase:
        score += 50
        flags.append("Insider purchase")
        if amount >= 1_000_000:
            score += 30
            flags.append(f"Large purchase ${amount:,.0f}")
        elif amount >= 100_000:
            score += 15
            flags.append(f"Purchase ${amount:,.0f}")
    elif is_sale:
        score = max(score - 10, 0)
        flags.append("Insider sale (bearish)")

    return min(score, 100), flags


def _score_usaspending(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []

    amount = signal.get("award_amount", 0) or 0

    if amount >= 1_000_000_000:
        score = 90
        flags.append("$1B+ contract award")
    elif amount >= 500_000_000:
        score = 75
        flags.append("$500M+ contract")
    elif amount >= 100_000_000:
        score = 55
        flags.append("$100M+ contract")
    elif amount >= 10_000_000:
        score = 35
        flags.append("$10M+ contract")
    else:
        score = 15
        flags.append("Contract <$10M")

    if signal.get("is_new_award"):
        score = min(score + 10, 100)
        flags.append("New award (not modification)")

    return min(score, 100), flags


def _score_serenity(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []

    confidence = signal.get("serenity_confidence", 0)
    score = int(confidence * 100)

    if signal.get("serenity_recent_mention"):
        score = min(score + 20, 100)
        flags.append("Recent Serenity mention")

    sentiment = signal.get("serenity_sentiment", "neutral")
    if sentiment == "bullish":
        score = min(score + 10, 100)
        flags.append("Serenity bullish sentiment")
    elif sentiment == "bearish":
        score = max(score - 30, 0)
        flags.append("Serenity bearish")

    return min(score, 100), flags


def _score_fundamentals(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []

    pe = signal.get("pe_ratio")
    if pe and 5 < pe < 20:
        score += 30
        flags.append(f"PE={pe:.1f} (attractive)")
    elif pe and pe < 5:
        score += 15
        flags.append(f"PE={pe:.1f} (very low)")

    rev_growth = signal.get("revenue_growth")
    if rev_growth and rev_growth > 0.20:
        score += 30
        flags.append(f"Revenue growth {rev_growth*100:.0f}%")
    elif rev_growth and rev_growth > 0.05:
        score += 15
        flags.append(f"Revenue growth {rev_growth*100:.0f}%")

    debt_eq = signal.get("debt_to_equity")
    if debt_eq is not None and debt_eq < 0.5:
        score += 20
        flags.append("Low D/E ratio")
    elif debt_eq is not None and debt_eq > 2.0:
        score -= 10
        flags.append("High D/E ratio")

    return max(min(score, 100), 0), flags


_SCORERS = {
    "edgar_8k":     _score_edgar_8k,
    "form4":        _score_form4,
    "usaspending":  _score_usaspending,
    "serenity":     _score_serenity,
    "fundamentals": _score_fundamentals,
}


# ── Main entry point ──────────────────────────────────────────────────────────

def score_signal(signal: dict) -> ScoreBreakdown:
    """
    Compute a composite score for a raw signal dict.
    Rileva automaticamente la pipeline dalla sorgente e applica i filtri
    corrispondenti. Se il segnale non supera i filtri, ritorna
    ScoreBreakdown con filtered=True — il chiamante deve scartare.
    """
    source = signal.get("source", "unknown")
    ticker = signal.get("ticker")
    pipeline = _PIPELINE_MAP.get(source, "unknown")

    bd = ScoreBreakdown(ticker=ticker, pipeline=pipeline)

    # ── Filtri pipeline ───────────────────────────────────────────────────────
    if pipeline == "stock_picking":
        if _filter_stock_picking(signal, ticker):
            bd.filtered = True
            return bd
    elif pipeline == "wheel_candidate":
        if _filter_wheel_candidate(signal, ticker):
            bd.filtered = True
            return bd

    # ── Scoring ───────────────────────────────────────────────────────────────
    scorer = _SCORERS.get(source)
    if scorer:
        raw, flags = scorer(signal)
        weight = WEIGHTS.get(source, 0.2)
        bd.components[source] = raw
        bd.flags.extend(flags)
        bd.total += int(raw * weight)
    else:
        logger.warning("Unknown signal source: %s", source)

    for supplementary in ("serenity", "fundamentals"):
        if supplementary == source:
            continue
        s_scorer = _SCORERS[supplementary]
        raw, flags = s_scorer(signal)
        if raw > 0:
            w = WEIGHTS[supplementary]
            bd.components[supplementary] = raw
            bd.flags.extend(flags)
            bd.total += int(raw * w)

    bd.total = min(bd.total, 100)
    logger.debug("Score %s [%s/%s]: %d – %s", ticker, source, pipeline, bd.total, bd.flags)
    return bd
