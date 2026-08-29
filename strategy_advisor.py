"""
strategy_advisor.py — confronta wheel vs trading direzionale su un segnale e
consiglia quale rende di piu', dato il capitale disponibile.

Non e' un quarto pipeline — e' un layer di confronto sopra le due strategie
di "possesso azionario" gia' esistenti:

  WHEEL     → vendi premio (CSP/CC), non serve comprare azioni prima
  TRADING   → compra azioni, vendi a target di prezzo/tempo (non tenere
              per dividendo — quello resta lo stock-picking Tier-1/dividendo
              gia' gestito da wheel_daemon._check_thesis_break)

L'edge "trading" e' stimato dal ritorno storico realizzato dei segnali della
stessa fonte/score (backtest_engine.py) — non una previsione, un dato
osservato su questo sistema. Sotto MIN_SAMPLE la stima non e' affidabile e
viene segnalata come tale, non nascosta.

Livello 1 advisory: produce solo un confronto testuale, nessun ordine.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import config
import database as db
import wheel_scanner as ws

BXM_TICKER = "^BXM"  # CBOE S&P 500 BuyWrite Index — benchmark covered-call sistematico

logger = logging.getLogger(__name__)

MIN_SAMPLE_FOR_TRADING = 20   # sotto questo n, l'edge storico non e' affidabile
TRADING_HORIZON_DAYS   = 20   # orizzonte usato per il ritorno storico (return_20d)


@dataclass
class StrategyComparison:
    ticker: str
    wheel_ann_pct: float | None = None
    wheel_capital: float | None = None
    wheel_note: str = ""
    trading_period_pct: float | None = None   # ritorno atteso sull'orizzonte reale (non annualizzato)
    trading_n: int = 0
    trading_win_rate: float | None = None
    trading_note: str = ""
    recommendation: str = "n/d"

    def format(self) -> str:
        lines = []
        if self.wheel_ann_pct is not None:
            lines.append(
                f"🎡 WHEEL: {self.wheel_ann_pct:.1f}%/anno annualizzato "
                f"(collaterale ${self.wheel_capital:,.0f}) — {self.wheel_note}"
            )
        else:
            lines.append(f"🎡 WHEEL: non disponibile — {self.wheel_note}")

        if self.trading_period_pct is not None:
            conf = "dato solido" if self.trading_n >= MIN_SAMPLE_FOR_TRADING else "campione piccolo, poco affidabile"
            lines.append(
                f"📈 TRADING: {self.trading_period_pct:+.1f}% atteso su ~{TRADING_HORIZON_DAYS} giorni "
                f"(storico su {self.trading_n} segnali, win rate {self.trading_win_rate*100:.0f}% — {conf}). "
                f"NON annualizzato: presuppone di ritrovare un segnale altrettanto buono ogni volta, "
                f"cosa che non e' garantita — l'annualizzato del wheel e il periodale del trading "
                f"non sono direttamente comparabili 1:1."
            )
        else:
            lines.append(f"📈 TRADING: non disponibile — {self.trading_note}")

        lines.append(f"\n➡️ Consigliato: *{self.recommendation}*")
        return "\n".join(lines)


def compare(ticker: str, source: str, score: int) -> StrategyComparison:
    """Confronta wheel vs trading direzionale per `ticker`, dato il segnale
    che lo ha fatto emergere (source/score usati per stimare l'edge trading)."""
    cmp = StrategyComparison(ticker=ticker)

    # ── Wheel ──────────────────────────────────────────────────────────────
    try:
        wr = ws.scan_wheel_candidate(ticker)
        if wr and wr.best_put:
            cmp.wheel_ann_pct = wr.best_put.annualized_return_net  # netto di commissione — confronto equo con l'edge trading (deep audit 29/08/2026)
            cmp.wheel_capital = wr.best_put.strike * 100
            cmp.wheel_note = f"strike ${wr.best_put.strike} scad {wr.best_put.expiry}, delta {wr.best_put.delta}"
        elif wr and wr.error:
            cmp.wheel_note = wr.error
        elif wr and not wr.above_sma50:
            cmp.wheel_note = "sotto 50-SMA — qualita' insufficiente ora"
        else:
            cmp.wheel_note = "nessun candidato valido nel chain opzioni"
    except Exception as exc:
        cmp.wheel_note = f"errore scan: {exc}"
        logger.warning("strategy_advisor wheel scan %s: %s", ticker, exc)

    # ── Trading direzionale ───────────────────────────────────────────────
    try:
        stats = db.get_backtest_stats_for(source, min_score=score - (score % 10))
        cmp.trading_n = stats["n"]
        if stats["avg_20d"] is not None and stats["n"] > 0:
            cmp.trading_period_pct = stats["avg_20d"] * 100  # NON annualizzato di proposito
            cmp.trading_win_rate = stats["win_rate_20d"]
        else:
            cmp.trading_note = "nessun dato storico ancora per questo bucket — servono altri cicli di backtest_engine"
    except Exception as exc:
        cmp.trading_note = f"errore query backtest: {exc}"
        logger.warning("strategy_advisor trading stats %s/%s: %s", source, score, exc)

    # ── Raccomandazione ────────────────────────────────────────────────────
    # Confronto sullo STESSO orizzonte (~TRADING_HORIZON_DAYS gg), non su
    # annualizzati: l'annualizzato del wheel è legittimo (puoi rivendere
    # premio ad ogni scadenza, è meccanico), quello del trading no (presuppone
    # di ritrovare un segnale buono quanto questo ogni volta — non garantito).
    # Comparare i due annualizzati alla cieca sovrastimerebbe sempre il trading.
    wheel_period_pct = (cmp.wheel_ann_pct * TRADING_HORIZON_DAYS / 365) if cmp.wheel_ann_pct is not None else None
    trading_reliable = cmp.trading_period_pct is not None and cmp.trading_n >= MIN_SAMPLE_FOR_TRADING

    if wheel_period_pct is None and not trading_reliable:
        cmp.recommendation = "nessuna delle due — dati insufficienti, tieni d'occhio"
    elif wheel_period_pct is None:
        cmp.recommendation = "TRADING (wheel non disponibile su questo titolo)"
    elif not trading_reliable:
        cmp.recommendation = "WHEEL (trading non ha ancora campione sufficiente per fidarsi)"
    elif wheel_period_pct >= cmp.trading_period_pct:
        cmp.recommendation = f"WHEEL (+{wheel_period_pct - cmp.trading_period_pct:.1f}pt su ~{TRADING_HORIZON_DAYS}gg, ma il wheel è ripetibile ogni scadenza — vantaggio composto nel tempo)"
    else:
        cmp.recommendation = f"TRADING (+{cmp.trading_period_pct - wheel_period_pct:.1f}pt su ~{TRADING_HORIZON_DAYS}gg — ma non garantito ripetibile, verifica prima di fidarti)"

    db.log_decision("strategy_compare", cmp.recommendation, ticker=ticker, source="strategy_advisor.compare")
    return cmp


@dataclass
class ConcentrationCheck:
    ticker: str
    current_value: float
    proposed_value: float
    bucket_total: float
    pct_after: float
    exceeds_cap: bool
    note: str = ""


def check_concentration(ticker: str, proposed_new_value: float,
                         positions: dict[str, float], cash: float) -> ConcentrationCheck:
    """
    Verifica se aprire/aumentare una posizione su `ticker` per
    `proposed_new_value` sfonda il tetto di concentrazione del bucket.

    `positions`: {ticker: valore_mercato} di TUTTE le posizioni attuali del
    bucket (incluso eventualmente ticker stesso se gia' aperta — viene
    sommato al nuovo valore, non sostituito). `cash`: cash libero nel bucket.

    Va chiamata con dati reali (IBKR live), non con stime — il tetto serve a
    prevenire un errore di concentrazione reale, non un esercizio teorico.
    """
    current_value = positions.get(ticker, 0.0)
    bucket_total = sum(positions.values()) + cash
    new_total_for_ticker = current_value + proposed_new_value
    # Il bucket totale dopo l'operazione include il nuovo valore aggiunto
    # (cash che si trasforma in posizione non cambia il totale, ma se e'
    # capitale fresco che entra ora, va sommato).
    bucket_after = bucket_total if proposed_new_value <= cash else bucket_total + (proposed_new_value - cash)
    pct_after = (new_total_for_ticker / bucket_after * 100) if bucket_after > 0 else 100.0

    exceeds = pct_after > config.WHEEL_MAX_CONCENTRATION_PCT
    note = (
        f"{ticker} arriverebbe al {pct_after:.1f}% del bucket "
        f"({'sopra' if exceeds else 'sotto'} il tetto {config.WHEEL_MAX_CONCENTRATION_PCT:.0f}%)"
    )
    return ConcentrationCheck(
        ticker=ticker, current_value=current_value, proposed_value=proposed_new_value,
        bucket_total=bucket_after, pct_after=round(pct_after, 1), exceeds_cap=exceeds, note=note,
    )


def bxm_benchmark_return(start_date: str, end_date: str | None = None) -> float | None:
    """
    Ritorno % dell'indice CBOE S&P 500 BuyWrite (^BXM) tra start_date e
    end_date (default: oggi). Benchmark corretto per una covered-call
    sistematica — a differenza del buy&hold puro, sconta gia' lo scambio
    upside-per-premio che la strategia fa strutturalmente. Da usare per
    contestualizzare il rendimento del bucket, non per giudicarlo "buono/
    cattivo" da solo (BXM storico 1986-2026: ~8.6%/anno — un bucket su
    titoli singoli ad alta IV come F/PBR non e' direttamente comparabile,
    la diversificazione e il livello di IV sono diversi).
    """
    import yfinance as yf
    try:
        hist = yf.Ticker(BXM_TICKER).history(start=start_date, end=end_date)
        if hist.empty or len(hist) < 2:
            return None
        closes = hist["Close"].dropna()
        return round(float((closes.iloc[-1] / closes.iloc[0] - 1) * 100), 2)
    except Exception as exc:
        logger.warning("bxm_benchmark_return: %s", exc)
        return None
