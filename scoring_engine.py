"""
Scoring engine – converts raw signal dicts into 0-100 scores.

Due pipeline distinte:
  STOCK_PICKING   → form4 + usaspending  → tag [PICK]  🎯
  WHEEL_CANDIDATE → edgar_8k             → tag [WHEEL] ⚙️

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
}

# ── Weights per il peso primario (una sola sorgente per segnale) ─────────────
# fundamentals + seeking_alpha sono supplementari, sommati sopra il primario.
# NON sono normalizzati a 1.0 e NON sono validati contro alcun backtest —
# sono costanti tarate a mano. Prima di ritoccarle di nuovo "a sensazione",
# costruire prima il motore di backtest (prezzo a T0/T0+N per ogni alert)
# per avere un modo di verificare se un cambiamento migliora o peggiora la
# qualita' dei segnali, invece di continuare a tarare al buio.
WEIGHTS = {
    "edgar_8k":      0.30,
    "form4":         0.45,
    "usaspending":   0.40,
    "fundamentals":  0.35,
    "seeking_alpha": 0.20,
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

    if not ticker:
        # Nessun ticker risolto (spesso un emittente non quotato — fondo
        # privato, BDC non-traded, ecc.): non è acquistabile sul mercato,
        # quindi non è un pick, a prescindere dal punteggio.
        logger.info("[PICK] scartata: nessun ticker risolto (titolo non quotato?)")
        return True

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

    if not ticker:
        logger.info("[WHEEL] scartata: nessun ticker risolto (titolo non quotato?)")
        return True

    # Market cap minimo $1B (liquidità opzioni garantita)
    cap = signal.get("market_cap")
    if cap is not None and cap < config.WHEEL_CAP_MIN:
        logger.info("[WHEEL] %s scartata: cap $%.0fM < min $%.0fB",
                    t, cap / 1e6, config.WHEEL_CAP_MIN / 1e9)
        return True

    # VRP (IV/HV20) — logica stockpile: se disponibile e troppo basso, scarta
    vrp = signal.get("vrp")
    if vrp is not None and vrp < config.WHEEL_VRP_MIN:
        logger.info("[WHEEL] %s scartata: VRP %.2f < min %.1f (premium non elevato)",
                    t, vrp, config.WHEEL_VRP_MIN)
        return True

    # Wheel scan: se non ci sono candidati put validi, scarta
    ws = signal.get("wheel_scan")
    if ws is not None and ws.candidates_count == 0:
        reason = "earnings in finestra" if ws.earnings_in_window else \
                 "sotto 50-SMA" if not ws.above_sma50 else \
                 "nessuna put supera i filtri"
        logger.info("[WHEEL] %s scartata: %s", t, reason)
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

        # CEO pesa piu' di CFO/COO/Chairman, che pesano piu' di President/Director
        # generico — letteratura insider trading: il CEO e' il ruolo con il
        # contenuto informativo piu' alto in assoluto, non equivalente al resto
        # del "high conviction" bucket. Proposto 27/08/2026, non ancora
        # backtestato con dati sufficienti (vedi log regole in the-machine-analyst).
        tier = signal.get("conviction_tier")
        tier_bonus = {"ceo": 25, "senior": 20, "generic": 12}.get(tier, 0)
        if tier_bonus:
            score += tier_bonus
            title = signal.get("insider_title", "")
            flags.append(f"Conviction {tier} +{tier_bonus} ({title})")

        cluster = signal.get("insider_cluster_count", 0) or 0
        if cluster >= 2:
            score += 15
            flags.append(f"Cluster: {cluster} insider negli ultimi 7gg")
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


def _score_seeking_alpha(signal: dict) -> tuple[int, list[str]]:
    """Sostituisce Serenity — notizie/earnings reali da feed Seeking Alpha per ticker."""
    score = 0
    flags = []

    days_since = signal.get("sa_days_since_latest")
    if days_since is not None and days_since <= 3:
        score += 20
        flags.append(f"SA: notizia recente ({days_since}gg fa)")

    surprise = signal.get("sa_eps_surprise")
    if surprise == "beat":
        score += 40
        flags.append("SA: EPS beat recente")
    elif surprise == "miss":
        score = max(score - 30, 0)
        flags.append("SA: EPS miss recente")

    item_count = signal.get("sa_item_count", 0) or 0
    if item_count >= 5:
        score += 15
        flags.append(f"SA: copertura attiva ({item_count} articoli)")

    if signal.get("sa_div_cut"):
        score = max(score - 30, 0)
        flags.append("SA: possibile taglio dividendo")
    elif signal.get("sa_div_mentioned"):
        score += 10
        flags.append("SA: articolo dividendo recente")

    return min(score, 100), flags


def _score_fundamentals(signal: dict) -> tuple[int, list[str]]:
    score = 0
    flags = []

    pe = signal.get("pe_ratio")
    if pe and 5 < pe < 20:
        score += 20
        flags.append(f"PE={pe:.1f} (attractive)")
    elif pe and pe < 5:
        score += 10
        flags.append(f"PE={pe:.1f} (very low)")

    rev_growth = signal.get("revenue_growth")
    if rev_growth and rev_growth > 0.20:
        score += 20
        flags.append(f"Revenue growth {rev_growth*100:.0f}%")
    elif rev_growth and rev_growth > 0.05:
        score += 10
        flags.append(f"Revenue growth {rev_growth*100:.0f}%")

    debt_eq = signal.get("debt_to_equity")
    if debt_eq is not None and debt_eq < 0.5:
        score += 15
        flags.append("Low D/E ratio")
    elif debt_eq is not None and debt_eq > 2.0:
        score -= 10
        flags.append("High D/E ratio")

    roe = signal.get("return_on_equity")
    if roe and roe > 0.15:
        score += 15
        flags.append(f"ROE {roe*100:.0f}%")

    mom_1m = signal.get("momentum_1m")
    mom_3m = signal.get("momentum_3m")
    if mom_1m is not None and mom_3m is not None:
        if mom_1m > 0 and mom_3m > 0:
            score += 15
            flags.append(f"Momentum positivo (1m {mom_1m*100:+.0f}%, 3m {mom_3m*100:+.0f}%)")
        elif mom_1m < -0.15:
            score = max(score - 15, 0)
            flags.append(f"Momentum negativo (1m {mom_1m*100:+.0f}%)")

    div_yield = signal.get("div_yield") or 0.0
    if div_yield >= 0.05:
        score += 20
        flags.append(f"Div yield {div_yield*100:.1f}% (high)")
    elif div_yield >= 0.02:
        score += 10
        flags.append(f"Div yield {div_yield*100:.1f}%")

    return max(min(score, 100), 0), flags


_SCORERS = {
    "edgar_8k":      _score_edgar_8k,
    "form4":         _score_form4,
    "usaspending":   _score_usaspending,
    "seeking_alpha": _score_seeking_alpha,
    "fundamentals":  _score_fundamentals,
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

    for supplementary in ("seeking_alpha", "fundamentals"):
        if supplementary == source:
            continue
        if supplementary == "seeking_alpha" and not signal.get("sa_item_count"):
            continue  # nessun dato Seeking Alpha — skip senza penalità
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
