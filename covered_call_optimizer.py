"""
covered_call_optimizer.py — Framework Hogue per Covered Calls, Married Put, Collar, Iron Condor.

Regole Hogue implementate:
  1. Chiusura anticipata al 50% del premio (+ regola 21-DTE)
  2. Roll forward solo a credito netto positivo, max 2 roll
  3. Selezione strike per regime di mercato e IV Rank
  4. Blocchi automatici (earnings, IV bassa, calo settimanale)
  5. 15-16 cicli/anno con riapertura immediata dopo chiusura anticipata
  6. Married Put automatica su ogni nuovo pick
  7. Collar automatico su posizioni in profitto >20%
  8. Iron Condor quando IV Rank >80%
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import yfinance as yf
import pandas as pd
import numpy as np

import config
import database as db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Dataclasses di input/output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WheelPosition:
    """Rappresenta una covered call (o CSP) aperta."""
    cycle_id:         int
    ticker:           str
    strike:           float
    expiry:           date
    premium_received: float          # premio incassato all'apertura
    premium_current:  float          # valore attuale (costo di chiusura)
    entry_price:      float          # prezzo di carico delle azioni
    roll_count:       int = 0
    phase:            str = "covered_call"

    @property
    def days_to_expiry(self) -> int:
        return max((self.expiry - date.today()).days, 0)

    @property
    def pct_captured(self) -> float:
        """Percentuale del premio già catturata (1.0 = tutto)."""
        if self.premium_received <= 0:
            return 0.0
        return (self.premium_received - self.premium_current) / self.premium_received

    @property
    def profit_on_stock_pct(self) -> float:
        """Rendimento dello stock rispetto all'entry price."""
        if self.entry_price <= 0:
            return 0.0
        # richiede stock_price corrente; viene iniettato da HogueOptimizer
        return getattr(self, "_stock_price_pct", 0.0)


@dataclass
class HogueAction:
    """Risultato di una valutazione Hogue."""
    action:      str           # 'close' | 'roll' | 'hold' | 'assigned' | 'skip'
    reason:      str
    urgency:     str = "normal"   # 'immediate' | 'normal' | 'info'
    details:     dict = field(default_factory=dict)
    telegram_msg: str = ""


# ─────────────────────────────────────────────────────────────────────────────
#  Utility options chain
# ─────────────────────────────────────────────────────────────────────────────

def _retry_yf(func, retries: int = 3, delay: float = 2.0):
    """Esegue una callable yfinance con retry su eccezione."""
    last_exc = None
    for attempt in range(retries):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            logger.warning("yfinance retry %d/%d: %s", attempt + 1, retries, exc)
            time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"yfinance failed after {retries} retries: {last_exc}") from last_exc


def _get_ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol)


def _best_expiry(ticker_obj: yf.Ticker, target_dte: int = None) -> str | None:
    """
    Trova la data di scadenza delle opzioni più vicina al target DTE.
    Ritorna stringa 'YYYY-MM-DD' o None se non disponibile.
    """
    target_dte = target_dte or config.HOGUE_TARGET_DTE
    try:
        expiries = _retry_yf(lambda: ticker_obj.options)
        if not expiries:
            return None
        today = date.today()
        best, best_diff = None, 9999
        for exp_str in expiries:
            exp_date = date.fromisoformat(exp_str)
            dte = (exp_date - today).days
            if dte < 1:
                continue
            diff = abs(dte - target_dte)
            if diff < best_diff:
                best_diff = diff
                best = exp_str
        return best
    except Exception as exc:
        logger.error("_best_expiry failed: %s", exc)
        return None


def _get_option_chain(ticker_obj: yf.Ticker, expiry: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Ritorna (calls_df, puts_df) per la scadenza richiesta.
    Colonne rilevanti: strike, bid, ask, impliedVolatility, delta (calcolato).
    """
    chain = _retry_yf(lambda: ticker_obj.option_chain(expiry))
    return chain.calls, chain.puts


def _mid_price(row: pd.Series) -> float:
    """Prezzo mid tra bid e ask; fallback su lastPrice."""
    bid = float(row.get("bid", 0) or 0)
    ask = float(row.get("ask", 0) or 0)
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    return float(row.get("lastPrice", 0) or 0)


def _calculate_iv_rank(ticker_obj: yf.Ticker, current_iv: float) -> float:
    """
    Calcola IV Rank (0-100) confrontando IV attuale con il range delle ultime 52 settimane.
    Usa la volatilità storica dei prezzi come proxy se l'IV storica non è disponibile.
    """
    try:
        hist = _retry_yf(lambda: ticker_obj.history(period="1y"))
        if hist.empty:
            return 50.0
        returns = hist["Close"].pct_change().dropna()
        # Volatilità rolling a 20 giorni annualizzata
        rolling_vol = returns.rolling(20).std() * np.sqrt(252)
        vol_52w_low  = float(rolling_vol.min())
        vol_52w_high = float(rolling_vol.max())
        if vol_52w_high <= vol_52w_low:
            return 50.0
        # Usa current_iv se disponibile, altrimenti vol recente
        iv = current_iv if current_iv > 0 else float(rolling_vol.iloc[-1])
        rank = (iv - vol_52w_low) / (vol_52w_high - vol_52w_low) * 100
        return round(max(0.0, min(100.0, rank)), 1)
    except Exception as exc:
        logger.warning("IV Rank calculation failed: %s", exc)
        return 50.0


def _current_price(ticker_obj: yf.Ticker) -> float:
    """Prezzo corrente del titolo."""
    try:
        info = _retry_yf(lambda: ticker_obj.fast_info)
        return float(info.last_price)
    except Exception:
        try:
            hist = ticker_obj.history(period="1d")
            return float(hist["Close"].iloc[-1])
        except Exception:
            return 0.0


def _weekly_return(ticker_obj: yf.Ticker) -> float:
    """Rendimento percentuale degli ultimi 7 giorni di trading."""
    try:
        hist = _retry_yf(lambda: ticker_obj.history(period="10d"))
        if len(hist) < 2:
            return 0.0
        price_now  = float(hist["Close"].iloc[-1])
        price_week = float(hist["Close"].iloc[-min(5, len(hist)-1)])
        return (price_now - price_week) / price_week
    except Exception:
        return 0.0


def _monthly_return(ticker_obj: yf.Ticker) -> float:
    """Rendimento percentuale dell'ultimo mese (≈22 giorni di trading)."""
    try:
        hist = _retry_yf(lambda: ticker_obj.history(period="2mo"))
        if len(hist) < 10:
            return 0.0
        price_now   = float(hist["Close"].iloc[-1])
        price_month = float(hist["Close"].iloc[-min(22, len(hist)-1)])
        return (price_now - price_month) / price_month
    except Exception:
        return 0.0


def _earnings_date(ticker_obj: yf.Ticker) -> date | None:
    """Prossima data di earnings o None se non disponibile."""
    try:
        cal = _retry_yf(lambda: ticker_obj.calendar)
        if cal is None:
            return None
        # yfinance può restituire dict o DataFrame
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                if isinstance(ed, (list, tuple)):
                    ed = ed[0]
                if hasattr(ed, "date"):
                    return ed.date()
                return date.fromisoformat(str(ed)[:10])
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.columns:
            val = cal["Earnings Date"].iloc[0]
            if hasattr(val, "date"):
                return val.date()
    except Exception as exc:
        logger.debug("earnings_date failed: %s", exc)
    return None


def _find_strike_by_delta(
    calls_df: pd.DataFrame,
    stock_price: float,
    target_delta: float,
    otm_pct: float,
) -> tuple[float, float]:
    """
    Trova lo strike OTM che meglio approssima il target_delta.
    Strategia: cerca prima nel range OTM atteso, poi per il delta più vicino.
    Ritorna (strike, mid_price).
    """
    if calls_df.empty:
        return stock_price * (1 + otm_pct), 0.0

    # Filtra solo strike OTM (> stock_price)
    otm = calls_df[calls_df["strike"] > stock_price].copy()
    if otm.empty:
        otm = calls_df.copy()

    # Se disponibile delta in chain, usalo; altrimenti approssima da IV e DTE
    if "delta" in otm.columns:
        otm["_delta"] = otm["delta"].abs()
    else:
        # Delta approssimato: OTM call ha delta ≈ 0.5 * (1 - (strike-price)/price)
        otm["_delta"] = (0.5 - (otm["strike"] - stock_price) / stock_price).clip(0.05, 0.50)

    # Target strike da percentuale OTM
    target_strike = stock_price * (1 + otm_pct)

    # Punteggio combinato: vicinanza al delta target + vicinanza allo strike target
    otm["_score"] = (
        (otm["_delta"] - target_delta).abs() * 2
        + (otm["strike"] - target_strike).abs() / stock_price
    )

    best = otm.loc[otm["_score"].idxmin()]
    return float(best["strike"]), _mid_price(best)


def _fetch_live_premium(
    ticker_obj: yf.Ticker,
    strike: float,
    expiry_str: str,
    option_type: str = "call",
) -> float | None:
    """
    Legge il mid price live dalla catena opzioni per uno strike e una scadenza esatti.

    Args:
        ticker_obj:  istanza yf.Ticker già creata
        strike:      strike della call/put aperta
        expiry_str:  scadenza in formato YYYY-MM-DD (es. '2025-08-15')
        option_type: 'call' (default) o 'put'

    Returns:
        mid price float, oppure None se non trovato o in caso di errore di rete.

    Note:
        yfinance richiede la scadenza esatta nella lista ticker_obj.options;
        se expiry_str non corrisponde a una scadenza disponibile la chiamata
        a option_chain() lancia KeyError — gestito con fallback a None.
    """
    try:
        available = _retry_yf(lambda: ticker_obj.options)
        if expiry_str not in available:
            # Trova la scadenza disponibile più vicina per data
            target = date.fromisoformat(expiry_str)
            closest = min(
                available,
                key=lambda e: abs((date.fromisoformat(e) - target).days),
                default=None,
            )
            if closest is None:
                logger.warning("_fetch_live_premium: nessuna scadenza disponibile per %s", ticker_obj.ticker)
                return None
            logger.debug(
                "_fetch_live_premium: scadenza %s non trovata, uso più vicina %s",
                expiry_str, closest,
            )
            expiry_str = closest

        chain = _retry_yf(lambda: ticker_obj.option_chain(expiry_str))
        df = chain.calls if option_type == "call" else chain.puts
        if df.empty:
            return None

        df = df.copy()
        df["_diff"] = (df["strike"] - strike).abs()
        best = df.loc[df["_diff"].idxmin()]

        # Tolleranza 0.50: avvisa se lo strike trovato differisce sensibilmente
        if float(best["_diff"]) > 0.50:
            logger.warning(
                "_fetch_live_premium %s: strike richiesto %.2f, trovato %.2f (diff %.2f) — "
                "possibile mismatch. Verifica che lo strike sia multiplo standard.",
                ticker_obj.ticker, strike, float(best["strike"]), float(best["_diff"]),
            )

        mid = _mid_price(best)
        if mid <= 0:
            # last resort: usa lastPrice
            mid = float(best.get("lastPrice", 0) or 0)

        return round(mid, 2) if mid > 0 else None

    except Exception as exc:
        logger.warning(
            "_fetch_live_premium(%s, strike=%.2f, expiry=%s): %s",
            getattr(ticker_obj, "ticker", "?"), strike, expiry_str, exc,
        )
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  HogueOptimizer — classe principale
# ─────────────────────────────────────────────────────────────────────────────

class HogueOptimizer:
    """
    Implementa il framework Hogue per ottimizzazione covered calls.
    Tutte le regole sono configurabili via .env (lette da config.py).
    """

    # ── 1. Chiusura anticipata ────────────────────────────────────────────────

    def check_early_close(self, position: WheelPosition) -> HogueAction:
        """
        Verifica se la posizione soddisfa i criteri di chiusura anticipata Hogue.

        Regole:
          - pct_captured >= 50% E DTE <= 21 → chiudi SUBITO (regola 21-DTE)
          - pct_captured >= 50% → chiudi (50% catturato)
          - altrimenti → mantieni
        """
        pct  = position.pct_captured
        dte  = position.days_to_expiry
        threshold = config.HOGUE_EARLY_CLOSE_PCT
        dte_rule  = config.HOGUE_DTE_THRESHOLD

        profit_usd = (position.premium_received - position.premium_current) * 100  # per contratto

        if pct >= threshold and dte <= dte_rule:
            action = HogueAction(
                action="close",
                urgency="immediate",
                reason=f"Regola 21-DTE: {pct*100:.0f}% catturato con {dte} giorni rimasti",
                details={
                    "pct_captured":  round(pct, 3),
                    "dte":           dte,
                    "profit_usd":    round(profit_usd, 2),
                    "reopen_now":    True,
                },
            )
            action.telegram_msg = self._fmt_early_close(position, action, rule="21-DTE")
            return action

        if pct >= threshold:
            action = HogueAction(
                action="close",
                urgency="normal",
                reason=f"{pct*100:.0f}% del premio catturato (soglia {threshold*100:.0f}%)",
                details={
                    "pct_captured": round(pct, 3),
                    "dte":          dte,
                    "profit_usd":   round(profit_usd, 2),
                    "reopen_now":   True,
                },
            )
            action.telegram_msg = self._fmt_early_close(position, action, rule="50%")
            return action

        return HogueAction(
            action="hold",
            urgency="info",
            reason=f"{pct*100:.0f}% catturato — mantieni ({dte} DTE)",
            details={"pct_captured": round(pct, 3), "dte": dte},
        )

    # ── 2. Roll Forward ───────────────────────────────────────────────────────

    def check_roll_opportunity(self, position: WheelPosition) -> HogueAction:
        """
        Valuta se fare roll up-and-out della covered call.

        Regole:
          - stock_price > strike * 0.97 E DTE > 7 → valuta roll
          - credito netto > 0 E roll_count < MAX_ROLLS → suggerisci roll
          - credito netto <= 0 OPPURE roll_count >= MAX_ROLLS → lascia assegnare
        """
        ticker_obj  = _get_ticker(position.ticker)
        stock_price = _current_price(ticker_obj)
        if stock_price <= 0:
            return HogueAction(action="hold", urgency="info", reason="Prezzo non disponibile")

        trigger_price = position.strike * config.HOGUE_ROLL_TRIGGER_PCT

        if stock_price <= trigger_price or position.days_to_expiry <= 7:
            return HogueAction(
                action="hold",
                urgency="info",
                reason=f"Nessun roll necessario — stock ${stock_price:.2f}, strike ${position.strike:.2f}, {position.days_to_expiry} DTE",
            )

        if position.roll_count >= config.HOGUE_MAX_ROLLS:
            action = HogueAction(
                action="assigned",
                urgency="normal",
                reason=f"Raggiunto limite roll ({config.HOGUE_MAX_ROLLS}) — lascia assegnare a ${position.strike:.2f}",
                details={"roll_count": position.roll_count, "stock_price": stock_price},
            )
            action.telegram_msg = self._fmt_assigned(position, stock_price)
            return action

        # Calcola roll: chiudi call attuale + vendi call successiva
        cost_to_close = position.premium_current
        new_expiry = _best_expiry(ticker_obj, config.HOGUE_TARGET_DTE)
        if not new_expiry:
            return HogueAction(action="hold", urgency="info", reason="Nessuna scadenza disponibile per roll")

        try:
            calls, _ = _get_option_chain(ticker_obj, new_expiry)
            # Roll up: strike superiore all'attuale
            otm_calls = calls[calls["strike"] > position.strike]
            if otm_calls.empty:
                otm_calls = calls

            # Prendi il primo strike OTM sopra il prezzo corrente
            roll_candidates = otm_calls[otm_calls["strike"] > stock_price]
            if roll_candidates.empty:
                roll_candidates = otm_calls

            best_roll = roll_candidates.iloc[0]
            new_strike   = float(best_roll["strike"])
            new_premium  = _mid_price(best_roll)
            net_credit   = round(new_premium - cost_to_close, 2)

            if net_credit > 0:
                action = HogueAction(
                    action="roll",
                    urgency="normal",
                    reason=f"Roll up-and-out a ${new_strike:.0f} scad. {new_expiry} — credito netto ${net_credit:.2f}",
                    details={
                        "new_strike":   new_strike,
                        "new_expiry":   new_expiry,
                        "new_premium":  new_premium,
                        "cost_to_close": cost_to_close,
                        "net_credit":   net_credit,
                        "roll_number":  position.roll_count + 1,
                    },
                )
                action.telegram_msg = self._fmt_roll(position, action, stock_price)
                return action
            else:
                action = HogueAction(
                    action="assigned",
                    urgency="normal",
                    reason=f"Roll a debito netto (${abs(net_credit):.2f}) — meglio lasciare assegnare",
                    details={
                        "net_credit":  net_credit,
                        "stock_price": stock_price,
                        "strike":      position.strike,
                    },
                )
                action.telegram_msg = self._fmt_assigned(position, stock_price)
                return action

        except Exception as exc:
            logger.error("check_roll_opportunity error: %s", exc)
            return HogueAction(action="hold", urgency="info", reason=f"Errore calcolo roll: {exc}")

    # ── 3. Selezione Strike ───────────────────────────────────────────────────

    def select_strike(
        self,
        ticker: str,
        market_regime: str = "lateral",
        iv_rank: float | None = None,
    ) -> dict:
        """
        Seleziona lo strike ottimale per una nuova covered call.

        Regimi:
          iv_rank > 80%   → OTM +5%, delta 0.25
          bullish (>5%/m) → OTM +8%, delta 0.15
          lateral (def.)  → OTM +3%, delta 0.30
          bearish         → OTM +1%, delta 0.40
        """
        ticker_obj  = _get_ticker(ticker)
        stock_price = _current_price(ticker_obj)
        if stock_price <= 0:
            return {"error": "Prezzo non disponibile"}

        # Calcola IV Rank se non fornito
        expiry = _best_expiry(ticker_obj)
        if not expiry:
            return {"error": "Nessuna scadenza opzioni disponibile"}

        calls, _ = _get_option_chain(ticker_obj, expiry)
        atm_iv = 0.0
        if not calls.empty:
            atm_row = calls.iloc[(calls["strike"] - stock_price).abs().argsort()[:1]]
            atm_iv  = float(atm_row["impliedVolatility"].iloc[0]) if "impliedVolatility" in atm_row else 0.0

        if iv_rank is None:
            iv_rank = _calculate_iv_rank(ticker_obj, atm_iv)

        # Determina parametri per regime
        if iv_rank > config.HOGUE_HIGH_IV_RANK:
            otm_pct, target_delta, regime_label = 0.05, 0.25, "IV alta"
        elif market_regime == "bullish":
            otm_pct, target_delta, regime_label = 0.08, 0.15, "rialzista"
        elif market_regime == "bearish":
            otm_pct, target_delta, regime_label = 0.01, 0.40, "ribassista"
        else:
            otm_pct, target_delta, regime_label = 0.03, 0.30, "laterale"

        strike, premium = _find_strike_by_delta(calls, stock_price, target_delta, otm_pct)

        dte = (date.fromisoformat(expiry) - date.today()).days
        monthly_return_pct = (premium / stock_price) * (30 / max(dte, 1)) * 100

        return {
            "ticker":           ticker,
            "stock_price":      round(stock_price, 2),
            "strike":           round(strike, 2),
            "expiry":           expiry,
            "dte":              dte,
            "premium":          round(premium, 2),
            "iv_rank":          round(iv_rank, 1),
            "atm_iv":           round(atm_iv * 100, 1),
            "market_regime":    regime_label,
            "target_delta":     target_delta,
            "otm_pct":          round(otm_pct * 100, 1),
            "monthly_return_pct": round(monthly_return_pct, 2),
        }

    # ── 4. Blocchi vendita calls ──────────────────────────────────────────────

    def should_sell_call(
        self,
        ticker: str,
        iv_rank: float | None = None,
        earnings_date_override: date | None = None,
    ) -> tuple[bool, str]:
        """
        Verifica se è sicuro vendere una covered call.
        Ritorna (True, "ok") oppure (False, motivo_blocco).
        """
        ticker_obj = _get_ticker(ticker)

        # Controlla IV Rank
        if iv_rank is None:
            expiry = _best_expiry(ticker_obj)
            if expiry:
                calls, _ = _get_option_chain(ticker_obj, expiry)
                atm_iv = 0.0
                stock_price = _current_price(ticker_obj)
                if not calls.empty and stock_price > 0:
                    atm_row = calls.iloc[(calls["strike"] - stock_price).abs().argsort()[:1]]
                    atm_iv  = float(atm_row["impliedVolatility"].iloc[0]) if "impliedVolatility" in atm_row else 0.0
                iv_rank = _calculate_iv_rank(ticker_obj, atm_iv)
            else:
                iv_rank = 50.0

        if iv_rank < config.HOGUE_MIN_IV_RANK:
            return False, f"IV Rank {iv_rank:.0f}% < soglia {config.HOGUE_MIN_IV_RANK:.0f}% — skip ciclo, premia insufficienti"

        # Controlla calo settimanale
        weekly_ret = _weekly_return(ticker_obj)
        if weekly_ret <= -config.HOGUE_WEEKLY_DROP_BLOCK:
            return False, f"Calo settimanale {weekly_ret*100:.1f}% oltre soglia -{config.HOGUE_WEEKLY_DROP_BLOCK*100:.0f}% — non vendere"

        # Controlla earnings
        earnings = earnings_date_override or _earnings_date(ticker_obj)
        if earnings:
            days_to_earnings = (earnings - date.today()).days
            if 0 <= days_to_earnings <= config.HOGUE_EARNINGS_BUFFER_DAYS:
                return False, f"Earnings fra {days_to_earnings} giorni ({earnings}) — BLOCCATO automaticamente"

        return True, "ok"

    # ── 5. Calcolo rendimento annualizzato ────────────────────────────────────

    def get_annualized_return(
        self,
        ticker: str,
        cycles_completed: int | None = None,
        avg_monthly_return: float | None = None,
    ) -> dict:
        """
        Calcola e proietta il rendimento annualizzato.
        Se non forniti, legge i dati dal DB per questo ticker.
        """
        if cycles_completed is None:
            cycles_completed = db.count_closed_cycles_year(ticker)
        if avg_monthly_return is None:
            avg_pnl = db.avg_pnl_per_cycle(ticker)
            # PnL medio per ciclo come % del capitale (approssimato)
            ticker_obj  = _get_ticker(ticker)
            stock_price = _current_price(ticker_obj)
            shares = 100  # 1 contratto standard
            capital = stock_price * shares if stock_price > 0 else 10_000
            avg_monthly_return = (avg_pnl / capital * 100) if capital > 0 else 0.0

        target_cycles = config.HOGUE_TARGET_CYCLES_YEAR
        projected_annual = avg_monthly_return * target_cycles

        # Rendimento realizzato fino ad oggi
        fraction_of_year = min(cycles_completed / target_cycles, 1.0)
        realized_so_far  = avg_monthly_return * cycles_completed

        return {
            "ticker":               ticker,
            "cycles_completed":     cycles_completed,
            "target_cycles_year":   target_cycles,
            "avg_return_per_cycle": round(avg_monthly_return, 2),
            "realized_ytd_pct":     round(realized_so_far, 2),
            "projected_annual_pct": round(projected_annual, 2),
            "pace":                 "in linea" if fraction_of_year >= (cycles_completed / target_cycles * 0.9) else "indietro",
        }

    # ── 6. Married Put ────────────────────────────────────────────────────────

    def calculate_married_put(
        self,
        ticker: str,
        entry_price: float,
        shares: int = 100,
    ) -> dict:
        """
        Calcola la put protettiva al -10% per un nuovo stock pick (Married Put).
        """
        ticker_obj = _get_ticker(ticker)
        put_strike = round(entry_price * 0.90, 0)  # -10% arrotondato

        expiry = _best_expiry(ticker_obj, 45)  # preferisce 45 DTE per MP
        if not expiry:
            return {"error": "Nessuna scadenza disponibile"}

        _, puts = _get_option_chain(ticker_obj, expiry)
        if puts.empty:
            return {"error": "Chain put non disponibile"}

        # Trova put più vicina allo strike -10%
        puts_filtered = puts[puts["strike"] <= entry_price]
        if puts_filtered.empty:
            puts_filtered = puts

        best_put = puts_filtered.iloc[(puts_filtered["strike"] - put_strike).abs().argsort()[:1]].iloc[0]
        put_premium = _mid_price(best_put)
        actual_strike = float(best_put["strike"])

        total_put_cost  = put_premium * shares
        position_value  = entry_price * shares
        cost_pct        = (total_put_cost / position_value) * 100
        max_downside_pct = ((actual_strike - entry_price) / entry_price) * 100  # negativo

        dte = (date.fromisoformat(expiry) - date.today()).days

        return {
            "ticker":           ticker,
            "entry_price":      round(entry_price, 2),
            "put_strike":       round(actual_strike, 2),
            "put_expiry":       expiry,
            "put_dte":          dte,
            "put_premium":      round(put_premium, 2),
            "total_cost_usd":   round(total_put_cost, 2),
            "cost_pct_trade":   round(cost_pct, 2),
            "max_downside_pct": round(max_downside_pct, 1),
            "protected":        True,
        }

    # ── 7. Collar ─────────────────────────────────────────────────────────────

    def calculate_collar(
        self,
        position: WheelPosition,
        stock_price: float | None = None,
    ) -> dict | None:
        """
        Calcola un Collar per posizioni in profitto >20%.
        Vende call OTM +15%, compra put OTM -10%.
        Ritorna None se il profitto è insufficiente per il trigger.
        """
        ticker_obj  = _get_ticker(position.ticker)
        if stock_price is None:
            stock_price = _current_price(ticker_obj)
        if stock_price <= 0:
            return None

        profit_pct = (stock_price - position.entry_price) / position.entry_price
        if profit_pct < config.HOGUE_COLLAR_TRIGGER_PCT:
            return None  # profitto insufficiente

        expiry = _best_expiry(ticker_obj)
        if not expiry:
            return None

        calls, puts = _get_option_chain(ticker_obj, expiry)
        if calls.empty or puts.empty:
            return None

        # Call OTM +15%
        call_strike_target = stock_price * 1.15
        otm_calls = calls[calls["strike"] > stock_price]
        if otm_calls.empty:
            return None
        best_call = otm_calls.iloc[(otm_calls["strike"] - call_strike_target).abs().argsort()[:1]].iloc[0]
        call_strike  = float(best_call["strike"])
        call_premium = _mid_price(best_call)

        # Put OTM -10%
        put_strike_target = stock_price * 0.90
        otm_puts = puts[puts["strike"] < stock_price]
        if otm_puts.empty:
            return None
        best_put = otm_puts.iloc[(otm_puts["strike"] - put_strike_target).abs().argsort()[:1]].iloc[0]
        put_strike  = float(best_put["strike"])
        put_premium = _mid_price(best_put)

        net_cost   = round(put_premium - call_premium, 2)  # positivo = costo, negativo = credito
        is_free     = abs(net_cost) < config.HOGUE_FREE_COLLAR_THRESHOLD
        dte = (date.fromisoformat(expiry) - date.today()).days

        result = {
            "ticker":         position.ticker,
            "stock_price":    round(stock_price, 2),
            "profit_pct":     round(profit_pct * 100, 1),
            "expiry":         expiry,
            "dte":            dte,
            "call_strike":    round(call_strike, 2),
            "call_premium":   round(call_premium, 2),
            "put_strike":     round(put_strike, 2),
            "put_premium":    round(put_premium, 2),
            "net_cost":       net_cost,
            "is_nearly_free": is_free,
            "max_profit_pct": round((call_strike - stock_price) / stock_price * 100, 1),
            "protected_to":   round(put_strike, 2),
        }
        result["telegram_msg"] = self._fmt_collar(position, result)
        return result

    # ── 8. Iron Condor ────────────────────────────────────────────────────────

    def calculate_iron_condor(
        self,
        ticker: str,
        iv_rank: float | None = None,
    ) -> dict | None:
        """
        Calcola un Iron Condor quando IV Rank >80%.
        Struttura: sell OTM put + buy further OTM put + sell OTM call + buy further OTM call.
        """
        ticker_obj  = _get_ticker(ticker)
        stock_price = _current_price(ticker_obj)
        if stock_price <= 0:
            return None

        if iv_rank is None:
            expiry_tmp = _best_expiry(ticker_obj)
            iv_rank = 50.0
            if expiry_tmp:
                calls, _ = _get_option_chain(ticker_obj, expiry_tmp)
                if not calls.empty:
                    atm_row = calls.iloc[(calls["strike"] - stock_price).abs().argsort()[:1]]
                    atm_iv  = float(atm_row["impliedVolatility"].iloc[0]) if "impliedVolatility" in atm_row else 0.0
                    iv_rank = _calculate_iv_rank(ticker_obj, atm_iv)

        if iv_rank < config.HOGUE_HIGH_IV_RANK:
            return None  # Iron Condor non appropriato

        expiry = _best_expiry(ticker_obj)
        if not expiry:
            return None

        calls, puts = _get_option_chain(ticker_obj, expiry)
        if calls.empty or puts.empty:
            return None

        dte = (date.fromisoformat(expiry) - date.today()).days

        # Struttura: delta ~0.16 per le short leg (1 SD)
        short_put_target  = stock_price * 0.90   # ~10% OTM
        short_call_target = stock_price * 1.10   # ~10% OTM
        wing_width        = stock_price * 0.05   # ali da 5%

        def _find_put(target: float) -> tuple[float, float]:
            filtered = puts[puts["strike"] <= stock_price]
            if filtered.empty:
                return target, 0.0
            best = filtered.iloc[(filtered["strike"] - target).abs().argsort()[:1]].iloc[0]
            return float(best["strike"]), _mid_price(best)

        def _find_call(target: float) -> tuple[float, float]:
            filtered = calls[calls["strike"] >= stock_price]
            if filtered.empty:
                return target, 0.0
            best = filtered.iloc[(filtered["strike"] - target).abs().argsort()[:1]].iloc[0]
            return float(best["strike"]), _mid_price(best)

        short_put_strike,  short_put_prem  = _find_put(short_put_target)
        long_put_strike,   long_put_prem   = _find_put(short_put_target - wing_width)
        short_call_strike, short_call_prem = _find_call(short_call_target)
        long_call_strike,  long_call_prem  = _find_call(short_call_target + wing_width)

        net_premium  = round((short_put_prem + short_call_prem - long_put_prem - long_call_prem) * 100, 2)
        max_loss     = round((short_put_strike - long_put_strike) * 100 - net_premium, 2)
        profit_range = f"${long_put_strike:.0f} – ${long_call_strike:.0f}"

        # Probabilità di profitto approssimata (range / 2*sigma)
        sigma_1m = stock_price * 0.15  # approx 1 sigma mensile
        prob_profit = min(max(round((1 - (stock_price * 0.10 / sigma_1m)) * 100, 0), 50), 75)

        result = {
            "ticker":            ticker,
            "stock_price":       round(stock_price, 2),
            "iv_rank":           round(iv_rank, 1),
            "expiry":            expiry,
            "dte":               dte,
            "long_put_strike":   round(long_put_strike, 2),
            "short_put_strike":  round(short_put_strike, 2),
            "short_call_strike": round(short_call_strike, 2),
            "long_call_strike":  round(long_call_strike, 2),
            "net_premium_usd":   net_premium,
            "max_loss_usd":      max_loss,
            "profit_range":      profit_range,
            "prob_profit_pct":   prob_profit,
        }
        result["telegram_msg"] = self._fmt_iron_condor(result)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    #  Formatting Telegram (formato Hogue)
    # ─────────────────────────────────────────────────────────────────────────

    def format_wheel_alert(
        self,
        position: WheelPosition,
        action: HogueAction,
        next_cycle: dict | None = None,
    ) -> str:
        """
        Genera il messaggio Telegram completo per un Wheel Update.
        """
        ticker = position.ticker
        annual = self.get_annualized_return(ticker)

        pct_cap  = position.pct_captured * 100
        dte      = position.days_to_expiry
        exp_str  = position.expiry.strftime("%d/%m/%Y")

        lines = [
            f"⚙️ *WHEEL UPDATE — {ticker}*\n",
            f"📊 *Posizione:* CC ${position.strike:.0f} scad. {exp_str}",
            f"Premio incassato: `${position.premium_received:.2f}` | Valore attuale: `${position.premium_current:.2f}`",
            f"Catturato: `{pct_cap:.0f}%` | DTE: `{dte}`\n",
            f"🔔 *Azione:* {action.reason}\n",
        ]

        if next_cycle:
            earnings_icon = "⚠️" if not next_cycle.get("earnings_ok", True) else "✅"
            lines += [
                "📈 *Prossimo ciclo:*",
                f"Strike: `${next_cycle.get('strike', 0):.0f}` scad. `{next_cycle.get('expiry', 'N/A')}`",
                f"Premio stimato: `${next_cycle.get('premium', 0):.2f}` | Rendimento: `{next_cycle.get('monthly_return_pct', 0):.1f}%`/mese",
                f"IV Rank: `{next_cycle.get('iv_rank', 0):.0f}%` | Earnings: {next_cycle.get('earnings_date', 'N/A')} {earnings_icon}",
            ]

        lines += [
            f"\n📅 Cicli completati {annual['cycles_completed']}/{annual['target_cycles_year']} | Annualizzato: `{annual['projected_annual_pct']:.1f}%`",
        ]

        return "\n".join(lines)

    def format_pick_alert(
        self,
        ticker: str,
        score: int,
        signal: dict,
        married_put: dict,
        next_cc: dict,
    ) -> str:
        """
        Genera il messaggio Telegram per un nuovo Pick con Married Put integrata.
        """
        name       = signal.get("short_name") or signal.get("recipient") or ticker
        price      = signal.get("current_price") or married_put.get("entry_price", 0)
        motivation = signal.get("flags", [])
        motivation_str = " · ".join(motivation[:3]) if motivation else signal.get("source", "segnale")

        stop_loss_price = round(price * (1 - config.STOP_LOSS_PCT), 2)

        # Informazioni earnings per la CC
        ticker_obj = _get_ticker(ticker)
        earnings = _earnings_date(ticker_obj)
        earnings_str = earnings.strftime("%d/%m/%Y") if earnings else "N/D"

        lines = [
            f"🎯 *PICK ALERT — Score {score}/100*\n",
            f"📋 {name} (`{ticker}`) | `${price:.2f}`",
            f"💰 Segnale: _{motivation_str}_\n",
        ]

        if not married_put.get("error"):
            lines += [
                "🛡️ *PROTEZIONE SUGGERITA (Married Put):*",
                f"Put strike: `${married_put['put_strike']:.0f}` scad. `{married_put['put_expiry']}` ({married_put['put_dte']} DTE)",
                f"Costo: `${married_put['total_cost_usd']:.2f}` (`{married_put['cost_pct_trade']:.1f}%` del trade)",
                f"Downside massimo: `{married_put['max_downside_pct']:.1f}%`\n",
            ]

        wheel_ok = not next_cc.get("error")
        iv_rank  = next_cc.get("iv_rank", 0)
        cc_str   = "sì ✅" if wheel_ok else "IV insufficiente ⚠️"
        lines += [
            f"📈 *WHEEL POSSIBILE:* {cc_str}",
        ]
        if wheel_ok:
            lines.append(
                f"IV Rank: `{iv_rank:.0f}%` | Strike CC: `${next_cc['strike']:.0f}` | Premio: `${next_cc['premium']:.2f}`/mese"
            )

        lines.append(f"\n🛑 Stop loss automatico: `${stop_loss_price:.2f}` (-{config.STOP_LOSS_PCT*100:.0f}%)")

        return "\n".join(lines)

    # ── Formatters interni ────────────────────────────────────────────────────

    def _fmt_early_close(self, pos: WheelPosition, action: HogueAction, rule: str) -> str:
        urgency_icon = "🚀" if action.urgency == "immediate" else "✅"
        return (
            f"{urgency_icon} *CHIUDI — {pos.ticker}* (regola {rule})\n"
            f"CC `${pos.strike:.0f}` scad. `{pos.expiry.strftime('%d/%m/%Y')}`\n"
            f"Premio: `${pos.premium_received:.2f}` → attuale `${pos.premium_current:.2f}`\n"
            f"Catturato: `{pos.pct_captured*100:.0f}%` | DTE: `{pos.days_to_expiry}`\n"
            f"Profitto: `${action.details.get('profit_usd', 0):.2f}` per contratto\n\n"
            f"_Riapri nuovo ciclo immediatamente dopo chiusura._"
        )

    def _fmt_roll(self, pos: WheelPosition, action: HogueAction, stock_price: float) -> str:
        d = action.details
        return (
            f"🔄 *ROLL — {pos.ticker}* (roll #{d['roll_number']}/{config.HOGUE_MAX_ROLLS})\n"
            f"Stock: `${stock_price:.2f}` | Strike attuale: `${pos.strike:.0f}`\n\n"
            f"Chiudi CC `${pos.strike:.0f}` @ `${d['cost_to_close']:.2f}`\n"
            f"Vendi CC `${d['new_strike']:.0f}` scad. `{d['new_expiry']}` @ `${d['new_premium']:.2f}`\n"
            f"*Credito netto: `${d['net_credit']:.2f}`* ✅"
        )

    def _fmt_assigned(self, pos: WheelPosition, stock_price: float) -> str:
        gain_loss = (pos.strike - pos.entry_price + pos.premium_received) * 100
        return (
            f"📦 *ASSEGNAZIONE PREVISTA — {pos.ticker}*\n"
            f"Stock: `${stock_price:.2f}` > Strike: `${pos.strike:.0f}`\n"
            f"Azioni vendute a `${pos.strike:.0f}` + premio `${pos.premium_received:.2f}` incassato\n"
            f"PnL stimato: `${gain_loss:+.2f}` per contratto\n\n"
            f"_Valuta riacquisto azioni per nuovo ciclo CSP._"
        )

    def _fmt_collar(self, pos: WheelPosition, d: dict) -> str:
        free_str = " 🎁 *PROTEZIONE QUASI GRATUITA!*" if d["is_nearly_free"] else ""
        cost_str = f"credito netto `${abs(d['net_cost']):.2f}`" if d["net_cost"] < 0 else f"costo netto `${d['net_cost']:.2f}`"
        return (
            f"🛡️ *COLLAR SUGGERITO — {pos.ticker}*{free_str}\n"
            f"Stock: `${d['stock_price']:.2f}` (+{d['profit_pct']:.0f}% dal costo)\n\n"
            f"Vendi Call `${d['call_strike']:.0f}` @ `${d['call_premium']:.2f}`\n"
            f"Compra Put `${d['put_strike']:.0f}` @ `${d['put_premium']:.2f}`\n"
            f"Scadenza: `{d['expiry']}` ({d['dte']} DTE)\n\n"
            f"{cost_str} | Max upside: `+{d['max_profit_pct']:.1f}%` | Protetto fino a: `${d['protected_to']:.0f}`"
        )

    def _fmt_iron_condor(self, d: dict) -> str:
        return (
            f"🦅 *IRON CONDOR — {d['ticker']}* (IV Rank {d['iv_rank']:.0f}%)\n\n"
            f"Struttura (scad. `{d['expiry']}`, {d['dte']} DTE):\n"
            f"  Long Put  `${d['long_put_strike']:.0f}` | Short Put `${d['short_put_strike']:.0f}`\n"
            f"  Short Call `${d['short_call_strike']:.0f}` | Long Call `${d['long_call_strike']:.0f}`\n\n"
            f"Premio netto: `${d['net_premium_usd']:.2f}` | Perdita max: `${d['max_loss_usd']:.2f}`\n"
            f"Range profitto: `{d['profit_range']}`\n"
            f"Probabilità profitto: ~`{d['prob_profit_pct']:.0f}%`"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Funzione di utilità pubblica per integrazione con main.py
# ─────────────────────────────────────────────────────────────────────────────

def run_hogue_check(ticker: str, cycle_id: int | None = None) -> None:
    """
    Entry point per il loop principale di the-machine.

    Flusso:
      0. Carica il ciclo aperto dal DB
      1. Aggiorna premium_current da yfinance in real-time (mid price live)
      2. Aggiorna il valore nel DB
      3. Esegue check_early_close  → alert Telegram se azione richiesta
      4. Esegue check_roll_opportunity → alert Telegram se roll/assegnazione
      5. Esegue calculate_collar  → alert Telegram se profitto >20%
      6. Nessuna azione: log di stato, nessun alert
    """
    from telegram_bot import send_alert

    opt        = HogueOptimizer()
    conn       = db._conn()
    ticker_obj = _get_ticker(ticker)

    # ── Step 0a: recupera ciclo aperto dal DB ────────────────────────────────
    if cycle_id is None:
        row_cycle = conn.execute(
            "SELECT * FROM wheel_cycles "
            "WHERE ticker=? AND phase != 'closed' "
            "ORDER BY opened_at DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row_cycle:
            logger.info("run_hogue_check: nessun ciclo aperto per %s", ticker)
            return
        cycle_id = row_cycle["id"]
    else:
        row_cycle = conn.execute(
            "SELECT * FROM wheel_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        if not row_cycle:
            logger.error("run_hogue_check: cycle_id %d non trovato nel DB", cycle_id)
            return

    # ── Step 0b: leggi entry_price dalla tabella positions (se disponibile) ──
    # La tabella `positions` registra il prezzo di carico reale delle azioni.
    # Se non presente (es. posizione non ancora registrata), usa lo strike come
    # proxy conservativo (worst case: entry al prezzo strike).
    pos_row = conn.execute(
        "SELECT entry_price FROM positions WHERE ticker=?", (ticker,)
    ).fetchone()
    entry_price: float = (
        float(pos_row["entry_price"])
        if pos_row and pos_row["entry_price"]
        else float(row_cycle["strike"])
    )
    if not pos_row:
        logger.debug(
            "run_hogue_check: %s non in tabella positions, uso strike %.2f come entry_price",
            ticker, entry_price,
        )

    # ── Step 0c: costruisci WheelPosition con dati DB ────────────────────────
    position = WheelPosition(
        cycle_id         = cycle_id,
        ticker           = ticker,
        strike           = float(row_cycle["strike"]),
        expiry           = date.fromisoformat(row_cycle["expiry"]),
        premium_received = float(row_cycle["premium_received"]),
        premium_current  = float(row_cycle["premium_current"]),  # valore DB; aggiornato al passo 1
        entry_price      = entry_price,
        roll_count       = int(row_cycle["roll_count"]),
        phase            = row_cycle["phase"],
    )

    # ── Step 1: aggiorna premium_current da yfinance in real-time ────────────
    # Legge la catena opzioni per il ticker, trova la call con strike e
    # scadenza corrispondenti, calcola il mid price (bid+ask)/2.
    expiry_str   = row_cycle["expiry"]   # formato YYYY-MM-DD
    live_premium = _fetch_live_premium(ticker_obj, position.strike, expiry_str, "call")

    if live_premium is not None:
        logger.info(
            "run_hogue_check %s: premium live $%.2f (DB aveva $%.2f)",
            ticker, live_premium, position.premium_current,
        )
        # ── Step 2: aggiorna il DB con il nuovo valore ──────────────────────
        position.premium_current = live_premium
        db.update_wheel_premium(cycle_id, live_premium)
    else:
        logger.warning(
            "run_hogue_check %s: impossibile aggiornare premium live — "
            "uso valore DB $%.2f (mercato chiuso o opzione scaduta?)",
            ticker, position.premium_current,
        )

    # Prezzo corrente dello stock (usato dal collar check)
    stock_price = _current_price(ticker_obj)

    # ── Step 3: check chiusura anticipata ────────────────────────────────────
    close_action = opt.check_early_close(position)
    if close_action.action == "close":
        # Costruisci anche il messaggio Wheel Update completo
        # (include prossimo ciclo se IV lo permette)
        next_cycle_data: dict | None = None
        can_sell, _ = opt.should_sell_call(ticker)
        if can_sell:
            next_cycle_data = opt.select_strike(ticker)

        full_msg = opt.format_wheel_alert(position, close_action, next_cycle_data)
        send_alert(full_msg)
        logger.info("run_hogue_check %s: alert CHIUDI inviato (%.0f%% catturato, %d DTE)",
                    ticker, position.pct_captured * 100, position.days_to_expiry)
        return

    # ── Step 4: check roll ────────────────────────────────────────────────────
    roll_action = opt.check_roll_opportunity(position)
    if roll_action.action in ("roll", "assigned"):
        full_msg = opt.format_wheel_alert(position, roll_action)
        send_alert(full_msg)
        if roll_action.action == "roll":
            new_count = db.increment_roll_count(cycle_id)
            logger.info("run_hogue_check %s: alert ROLL inviato (roll #%d)", ticker, new_count)
        else:
            logger.info("run_hogue_check %s: alert ASSEGNAZIONE inviato", ticker)
        return

    # ── Step 5: check collar ──────────────────────────────────────────────────
    collar = opt.calculate_collar(position, stock_price)
    if collar:
        send_alert(collar["telegram_msg"])
        logger.info(
            "run_hogue_check %s: alert COLLAR inviato (profitto +%.1f%%)",
            ticker, collar["profit_pct"],
        )
        return

    # ── Step 6: nessuna azione richiesta ─────────────────────────────────────
    logger.info(
        "run_hogue_check %s: nessuna azione — %.0f%% catturato, %d DTE, stock $%.2f",
        ticker, position.pct_captured * 100, position.days_to_expiry, stock_price,
    )
