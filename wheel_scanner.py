"""
wheel_scanner.py — 3-tier options scanner per WHEEL_CANDIDATE signals.

Ispirato a:
  - wheel-scout (MiniGioLabs/wheel-scout): filtri hard/profit/quality
  - stockpile/options-scanner (medloh/stockpile): VRP scoring via IV surface

Usa solo yfinance — nessuna API a pagamento richiesta.
Chiamato da main.py dopo che un segnale ha passato i filtri base WHEEL.

Output principale: la miglior put da vendere (strike, scadenza, premio,
annualized return) + VRP per valutare se il premio è alto rispetto alla
volatilità realizzata.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

import config
from covered_call_optimizer import calculate_iv_rank

logger = logging.getLogger(__name__)

RISK_FREE_RATE = 0.05


def _bs_put_delta(spot: float, strike: float, dte: int, iv: float) -> float | None:
    """Delta Black-Scholes di una put (valore assoluto, 0-1). None se input invalidi."""
    if spot <= 0 or strike <= 0 or dte <= 0 or iv <= 0:
        return None
    T = dte / 365.0
    d1 = (np.log(spot / strike) + (RISK_FREE_RATE + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
    return abs(norm.cdf(d1) - 1)


@dataclass
class PutCandidate:
    strike: float
    expiry: str           # YYYY-MM-DD
    dte: int
    bid: float
    ask: float
    mid: float
    iv: float             # implied volatility (frazione, es: 0.45 = 45%)
    open_interest: int
    volume: int
    spread_pct: float     # (ask-bid)/mid
    annualized_return: float  # (mid/strike)*(365/dte)*100 — LORDO, prima di commissioni
    annualized_return_net: float = 0.0  # netto di 2 commissioni (apertura+chiusura) — usato per filtro/ordinamento
    delta: float | None = None  # |delta| Black-Scholes (probabilita' di assegnazione approx)


@dataclass
class WheelScanResult:
    ticker: str
    best_put: PutCandidate | None = None
    vrp: float | None = None           # ATM_IV / HV_20 (>1 = premium elevato)
    hv_20: float | None = None         # 20-day historical volatility annualizzata
    iv_rank: float | None = None       # IV Rank standard (min-max 52 sett.) — condiviso con covered_call_optimizer.calculate_iv_rank
    atm_iv: float | None = None        # implied vol ATM media
    earnings_in_window: bool = False
    next_earnings: str | None = None   # YYYY-MM-DD
    above_sma50: bool = True
    candidates_count: int = 0
    error: str | None = None
    div_yield: float | None = None     # dividendYield da yfinance (es. 0.076 = 7.6%)
    annual_div: float = 0.0            # dividendRate annuo per azione
    ex_div_date: str | None = None     # prossima ex-dividend date ISO
    div_in_dte_window: bool = False    # ex-div cade dentro il DTE della best_put


# ── Funzione principale ────────────────────────────────────────────────────────

def scan_wheel_candidate(ticker: str) -> WheelScanResult | None:
    """
    Scansiona le put options di un ticker e trova il miglior candidato wheel.

    Tier 1 (hard):    OI, spread bid-ask, DTE nel range
    Tier 2 (profit):  premium minimo, annualized return minimo
    Tier 3 (quality): above 50-SMA, no earnings nella finestra

    VRP = ATM_IV / HV_20 — dalla logica stockpile options-scanner:
      se VRP > 1 il mercato prezza più rischio di quanto lo stock si muova
      → premium sale è favorevole.

    Ritorna None su errore fatale, WheelScanResult con best_put=None se
    nessun candidato supera i filtri.
    """
    try:
        yf_obj = yf.Ticker(ticker)
        info   = yf_obj.info or {}
        spot   = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))

        if not spot or spot <= 0:
            return WheelScanResult(ticker=ticker, error="prezzo non disponibile")

        # ── Dividendo ─────────────────────────────────────────────────────────
        div_yield  = _safe_float(info.get("dividendYield"))
        annual_div = _safe_float(info.get("dividendRate")) or 0.0
        ex_div_ts  = info.get("exDividendDate")
        ex_div_date: str | None = None
        if ex_div_ts:
            try:
                ex_div_date = datetime.fromtimestamp(ex_div_ts).date().isoformat()
            except Exception:
                pass

        # ── Dati storici per HV20 e SMA50 ────────────────────────────────────
        hist    = yf_obj.history(period="70d")
        hv_20   = _compute_hv(hist, window=20)
        sma50   = _safe_float(info.get("fiftyDayAverage"))
        above_sma50 = (spot >= sma50) if sma50 else True

        # ── Earnings check ────────────────────────────────────────────────────
        next_earnings, earnings_in_window = _check_earnings(
            yf_obj, config.WHEEL_DTE_MAX
        )

        # ── Catena opzioni ────────────────────────────────────────────────────
        option_dates = yf_obj.options
        if not option_dates:
            return WheelScanResult(
                ticker=ticker, hv_20=hv_20,
                above_sma50=above_sma50,
                next_earnings=next_earnings,
                earnings_in_window=earnings_in_window,
                error="nessuna opzione disponibile",
            )

        today = date.today()
        candidates: list[PutCandidate] = []
        atm_ivs: list[float] = []

        for exp_str in option_dates:
            try:
                exp_date = date.fromisoformat(exp_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if not (config.WHEEL_DTE_MIN <= dte <= config.WHEEL_DTE_MAX):
                continue

            try:
                chain = yf_obj.option_chain(exp_str)
                puts  = chain.puts
            except Exception as exc:
                logger.debug("wheel_scanner: option_chain %s %s: %s", ticker, exp_str, exc)
                continue

            for _, row in puts.iterrows():
                strike = _safe_float(row.get("strike")) or 0.0
                bid    = _safe_float(row.get("bid")) or 0.0
                ask    = _safe_float(row.get("ask")) or 0.0
                oi     = _safe_int(row.get("openInterest"))
                vol    = _safe_int(row.get("volume"))
                iv     = _safe_float(row.get("impliedVolatility")) or 0.0

                if strike <= 0 or ask <= 0:
                    continue

                mid        = (bid + ask) / 2
                spread_pct = (ask - bid) / mid if mid > 0 else 1.0
                ann_ret    = (mid / strike) * (365 / dte) * 100 if dte > 0 else 0.0
                # Netto di apertura+chiusura (2 ordini, caso peggiore Hogue con
                # early-close) — prima il filtro/ordinamento usava solo il lordo,
                # sovrastimando il rendimento mostrato su Telegram rispetto a
                # quanto backtest_ford.py modella correttamente (trovato in deep
                # audit 29/08/2026, commissione unificata via config).
                net_premium = mid * 100 - 2 * config.WHEEL_COMMISSION_PER_ORDER
                ann_ret_net = (net_premium / 100 / strike) * (365 / dte) * 100 if dte > 0 else 0.0

                # Solo put OTM (CSP = vendere sotto mercato)
                if strike >= spot * 1.01:
                    continue

                # Selezione per delta Black-Scholes invece di banda fissa % dello
                # spot: si adatta a IV/tempo residuo invece di ignorarli. Standard
                # CSP: delta 0.16-0.30 (~16-30% probabilita' di assegnazione).
                delta = _bs_put_delta(spot, strike, dte, iv) if iv > 0 else None
                if delta is None or not (config.WHEEL_PUT_DELTA_MIN <= delta <= config.WHEEL_PUT_DELTA_MAX):
                    continue

                # Tier 1: hard filters (wheel-scout)
                if oi > 0 and oi < config.WHEEL_OI_MIN:
                    continue
                # Passa se soddisfa la % OPPURE lo spread assoluto in $ — su
                # opzioni a premio basso il tick minimo del market maker fa
                # esplodere lo spread % anche con OI molto alto.
                spread_abs = ask - bid
                if spread_pct > config.WHEEL_MAX_SPREAD_PCT and spread_abs > config.WHEEL_MAX_SPREAD_ABS:
                    continue

                # Tier 2: profitability (wheel-scout)
                if mid < config.WHEEL_MIN_PREMIUM:
                    continue
                if ann_ret_net < config.WHEEL_ANN_RETURN_MIN:
                    continue

                # Raccogli IV attorno al ATM (±5% dello spot) per VRP
                if iv > 0 and abs(strike - spot) / spot <= 0.05:
                    atm_ivs.append(iv)

                candidates.append(PutCandidate(
                    strike=strike, expiry=exp_str, dte=dte,
                    bid=bid, ask=ask, mid=mid, iv=iv,
                    open_interest=oi, volume=vol,
                    spread_pct=spread_pct,
                    annualized_return=round(ann_ret, 2),
                    annualized_return_net=round(ann_ret_net, 2),
                    delta=round(delta, 3),
                ))

        # ── VRP (logica stockpile) ────────────────────────────────────────────
        atm_iv = float(np.mean(atm_ivs)) if atm_ivs else None
        vrp    = (atm_iv / hv_20) if (atm_iv and hv_20 and hv_20 > 0) else None

        # IV Rank standard (min-max 52 sett.) — condiviso con
        # covered_call_optimizer.calculate_iv_rank, non una seconda formula.
        iv_rank = calculate_iv_rank(yf_obj, atm_iv or 0.0)

        # Tier 3: qualità (wheel-scout)
        if not above_sma50:
            logger.info("[WHEEL] %s: sotto 50-SMA — candidati scartati", ticker)
            candidates = []

        if earnings_in_window:
            logger.info("[WHEEL] %s: earnings nella finestra DTE — candidati scartati", ticker)
            candidates = []

        if iv_rank is not None and iv_rank < config.WHEEL_MIN_IV_RANK:
            logger.info("[WHEEL] %s: IV Rank %.0f < min %.0f — candidati scartati", ticker, iv_rank, config.WHEEL_MIN_IV_RANK)
            candidates = []

        # Rank per annualized return NETTO (stockpile: ordine per IV excess, qui
        # ann.ret) — prima ordinava per il lordo, potendo preferire un candidato
        # con premio alto ma su un ciclo a molti ordini/roll, netto peggiore.
        candidates.sort(key=lambda c: c.annualized_return_net, reverse=True)
        best = candidates[0] if candidates else None

        logger.info(
            "wheel_scanner %s: %d candidati, VRP=%.2f, best=%s",
            ticker, len(candidates),
            vrp or 0,
            f"${best.strike:.0f} {best.expiry} {best.annualized_return_net:.1f}% netto (lordo {best.annualized_return:.1f}%)" if best else "nessuno",
        )

        div_in_dte_window = False
        if ex_div_date and best:
            try:
                div_in_dte_window = date.fromisoformat(ex_div_date) <= date.fromisoformat(best.expiry)
            except Exception:
                pass

        return WheelScanResult(
            ticker=ticker,
            best_put=best,
            vrp=vrp,
            hv_20=hv_20,
            iv_rank=iv_rank,
            atm_iv=atm_iv,
            earnings_in_window=earnings_in_window,
            next_earnings=next_earnings,
            above_sma50=above_sma50,
            candidates_count=len(candidates),
            div_yield=div_yield,
            annual_div=annual_div,
            ex_div_date=ex_div_date,
            div_in_dte_window=div_in_dte_window,
        )

    except Exception as exc:
        logger.warning("wheel_scanner.scan_wheel_candidate(%s): %s", ticker, exc)
        return WheelScanResult(ticker=ticker, error=str(exc))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_hv(hist: pd.DataFrame, window: int = 20) -> float | None:
    """Historical volatility annualizzata (std log-returns × √252)."""
    try:
        if hist is None or hist.empty:
            return None
        # yfinance può restituire MultiIndex — prendi la colonna Close
        if isinstance(hist.columns, pd.MultiIndex):
            closes = hist["Close"].iloc[:, 0].dropna()
        else:
            closes = hist["Close"].dropna()
        if len(closes) < window + 1:
            return None
        log_ret = np.log(closes / closes.shift(1)).dropna()
        hv = float(log_ret.tail(window).std() * np.sqrt(252))
        return hv if hv > 0 else None
    except Exception as e:
        logger.debug("_compute_hv error: %s", e)
        return None




def _check_earnings(yf_obj, dte_max: int) -> tuple[str | None, bool]:
    """Ritorna (next_earnings_date_iso, in_window) usando yfinance earnings_dates."""
    try:
        ed = yf_obj.earnings_dates
        if ed is None or ed.empty:
            return None, False
        now_utc = pd.Timestamp.now(tz="UTC")
        future  = ed[ed.index > now_utc]
        if future.empty:
            return None, False
        next_e  = future.index[0].date()
        days    = (next_e - date.today()).days
        return next_e.isoformat(), 0 <= days <= dte_max
    except Exception:
        return None, False


def _safe_float(val) -> float | None:
    try:
        v = float(val) if val is not None else None
        return None if (v is not None and v != v) else v  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int:
    try:
        v = float(val) if val is not None else 0.0
        return int(v) if v == v else 0  # NaN → 0
    except (TypeError, ValueError):
        return 0
