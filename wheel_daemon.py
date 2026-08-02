"""
wheel_daemon.py — Loop giornaliero di monitoraggio Wheel Strategy (Livello 1 Advisory).

Funzionalità:
  - Controlla ogni mattina tutti i cicli wheel aperti nel DB
  - Applica regole Hogue (50% early close, 21-DTE, roll, collar)
  - Avvisa per dividend risk prima di ogni nuovo ciclo
  - Report domenicale automatico (income + performance)
  - Tutto advisory: nessun ordine eseguito automaticamente

Avvio: chiamato da main.py → wheel_daemon.start()
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import yfinance as yf
import numpy as np
import pandas as pd

import config
import database as db
from covered_call_optimizer import (
    HogueOptimizer, WheelPosition,
    _current_price, _get_ticker, _fetch_live_premium,
    _best_expiry, _get_option_chain, _mid_price,
    _calculate_iv_rank, _earnings_date,
)

logger = logging.getLogger(__name__)

_opt = HogueOptimizer()

# Intervallo di check: ogni ora, ma esegue la logica solo una volta al giorno
_CHECK_INTERVAL_SEC = 3600
_last_check_date: Optional[date] = None
_last_weekly_report: Optional[date] = None


# ── Dividend helpers ──────────────────────────────────────────────────────────

def _next_ex_div(ticker_obj: yf.Ticker) -> Optional[date]:
    """Restituisce la prossima ex-dividend date da yfinance fast_info."""
    try:
        info = ticker_obj.info
        ts = info.get("exDividendDate")
        if ts:
            return datetime.fromtimestamp(ts).date()
    except Exception:
        pass
    try:
        divs = ticker_obj.dividends
        if divs is not None and not divs.empty:
            future = divs[divs.index > pd.Timestamp.now(tz="UTC")]
            if not future.empty:
                return future.index[0].date()
    except Exception:
        pass
    return None


def _annual_dividend(ticker_obj: yf.Ticker) -> float:
    """Dividendo annuo per azione."""
    try:
        return float(ticker_obj.info.get("dividendRate") or 0)
    except Exception:
        return 0.0


# ── Suggest helper ────────────────────────────────────────────────────────────

def suggest_next_cycle(ticker: str, phase: str = "cc") -> dict:
    """
    Analizza la catena opzioni e restituisce la migliore CC o CSP da aprire.
    phase: 'cc' (hai azioni) o 'csp' (sei in cash).
    """
    ticker_obj = _get_ticker(ticker)
    spot = _current_price(ticker_obj)
    if not spot:
        return {"error": "Prezzo non disponibile"}

    expiry = _best_expiry(ticker_obj, config.HOGUE_TARGET_DTE)
    if not expiry:
        return {"error": "Nessuna scadenza opzioni disponibile"}

    calls, puts = _get_option_chain(ticker_obj, expiry)
    dte = (date.fromisoformat(expiry) - date.today()).days

    # Earnings check
    earnings = _earnings_date(ticker_obj)
    earnings_in_window = False
    if earnings:
        days_to_earn = (earnings - date.today()).days
        earnings_in_window = 0 <= days_to_earn <= config.HOGUE_EARNINGS_BUFFER_DAYS

    # Dividend check
    ex_div = _next_ex_div(ticker_obj)
    div_in_window = False
    div_risk = False
    if ex_div:
        days_to_div = (ex_div - date.today()).days
        div_in_window = 0 <= days_to_div <= dte
        div_risk = div_in_window and phase == "cc"

    # IV rank
    atm_iv = 0.0
    df = calls if phase == "cc" else puts
    if not df.empty:
        atm_row = df.iloc[(df["strike"] - spot).abs().argsort()[:1]]
        atm_iv = float(atm_row["impliedVolatility"].iloc[0]) if "impliedVolatility" in atm_row else 0.0
    iv_rank = _calculate_iv_rank(ticker_obj, atm_iv)

    # Selezione strike
    if phase == "cc":
        otm_pct = 0.03
        candidates = calls[calls["strike"] > spot].copy()
        candidates = candidates[candidates["strike"] <= spot * 1.12]
    else:
        otm_pct = 0.03
        candidates = puts[puts["strike"] < spot].copy()
        candidates = candidates[candidates["strike"] >= spot * 0.88]

    if candidates.empty:
        return {"error": "Nessuno strike idoneo trovato"}

    candidates = candidates.copy()
    candidates["mid"] = candidates.apply(_mid_price, axis=1)
    candidates["spread_pct"] = (candidates["ask"] - candidates["bid"]) / candidates["mid"].clip(0.01)
    candidates["oi"] = candidates["openInterest"].fillna(0).astype(int)
    candidates["ann_ret"] = candidates["mid"] / spot * (365 / max(dte, 1)) * 100

    # Filtri base
    candidates = candidates[candidates["mid"] >= 0.10]
    candidates = candidates[candidates["spread_pct"] <= 0.20]
    candidates = candidates[candidates["oi"] >= 100]

    if candidates.empty:
        return {"error": "Nessun candidato dopo filtri (OI/spread)"}

    # Migliore: più vicino al target OTM con OI alto
    target_strike = spot * (1 + otm_pct) if phase == "cc" else spot * (1 - otm_pct)
    candidates["_score"] = (candidates["strike"] - target_strike).abs() / spot
    best = candidates.sort_values("_score").iloc[0]

    ann_ret = float(best["ann_ret"])
    mid     = float(best["mid"])
    strike  = float(best["strike"])
    oi      = int(best["oi"])

    return {
        "ticker":          ticker,
        "phase":           phase.upper(),
        "spot":            round(spot, 2),
        "strike":          round(strike, 2),
        "expiry":          expiry,
        "dte":             dte,
        "mid":             round(mid, 2),
        "ann_ret":         round(ann_ret, 1),
        "iv_rank":         round(iv_rank, 1),
        "atm_iv_pct":      round(atm_iv * 100, 1),
        "oi":              oi,
        "earnings":        earnings.isoformat() if earnings else None,
        "earnings_block":  earnings_in_window,
        "ex_div":          ex_div.isoformat() if ex_div else None,
        "div_in_window":   div_in_window,
        "div_risk":        div_risk,
        "annual_div":      _annual_dividend(ticker_obj),
    }


# ── Formato messaggi advisory ─────────────────────────────────────────────────

def _fmt_advisory(cycle_row, action_str: str, detail: str, urgency: str = "normal") -> str:
    icon = {"immediate": "🚀", "normal": "📋", "info": "ℹ️"}.get(urgency, "📋")
    ticker  = cycle_row["ticker"]
    phase   = cycle_row["phase"].replace("_", " ").upper()
    strike  = float(cycle_row["strike"])
    expiry  = cycle_row["expiry"]
    prem_r  = float(cycle_row["premium_received"])
    prem_c  = float(cycle_row["premium_current"])
    pct_cap = int((prem_r - prem_c) / prem_r * 100) if prem_r > 0 else 0
    dte     = (date.fromisoformat(expiry) - date.today()).days

    return (
        f"{icon} *WHEEL ADVISORY — {ticker}*\n"
        f"Posizione: `{phase}` strike `${strike:.1f}` scad `{expiry}` ({dte} DTE)\n"
        f"Premio: `${prem_r:.2f}` aperto → `${prem_c:.2f}` ora ({pct_cap}% catturato)\n\n"
        f"*Azione consigliata:* {action_str}\n"
        f"_{detail}_"
    )


def _fmt_suggest(s: dict) -> str:
    if s.get("error"):
        return f"⚠️ Analisi {s.get('ticker','')}: {s['error']}"

    warn = ""
    if s["earnings_block"]:
        warn += f"\n⛔ *BLOCCO EARNINGS* — {s['earnings']} tra pochi giorni. Non aprire."
    if s["div_risk"]:
        warn += f"\n⚠️ *DIV RISK* — ex-div {s['ex_div']} dentro la scadenza. Rischio early assignment."
    if s["div_in_window"] and not s["div_risk"]:
        warn += f"\n💰 Dividendo ex-div {s['ex_div']} dentro il ciclo — ottimo, lo incassi prima."

    phase_label = "Covered Call" if s["phase"] == "CC" else "Cash-Secured Put"
    ordine = "VENDI" if True else ""

    return (
        f"📊 *SUGGEST — {s['ticker']} ({s['phase']})*\n"
        f"Spot: `${s['spot']:.2f}` | IV Rank: `{s['iv_rank']:.0f}%` | ATM IV: `{s['atm_iv_pct']:.0f}%`\n\n"
        f"*{ordine} {phase_label}:*\n"
        f"  Strike: `${s['strike']:.2f}` | Scad: `{s['expiry']}` ({s['dte']} DTE)\n"
        f"  Premio mid: `${s['mid']:.2f}` → `${s['mid']*100:.0f}` per contratto\n"
        f"  Ann. return: `{s['ann_ret']:.1f}%` | OI: `{s['oi']:,}`\n"
        + (f"  Dividendo annuo: `${s['annual_div']:.2f}/az` (+`${s['annual_div']*100:.0f}`/anno)\n" if s['annual_div'] else "")
        + f"{warn}"
    )


def _fmt_weekly_report(positions: list, year: int, month: int) -> str:
    report = db.get_income_report(year, month)
    lines = [
        f"📅 *WHEEL REPORT — {year}/{month:02d}*\n",
        f"Cicli chiusi nel mese: `{report['n_closed']}`",
        f"Premi realizzati:      `${report['total_realized']:.2f}`",
        f"Premi non realizzati:  `${report['total_unrealized']:.2f}` (posizioni aperte)",
        f"YTD realizzato:        `${report['ytd_realized']:.2f}`\n",
    ]

    if report["closed_cycles"]:
        lines.append("*Cicli chiusi:*")
        for c in report["closed_cycles"]:
            lines.append(f"  `{c['ticker']}` — `${float(c['pnl_realized']):.2f}` ({c['closed_at'][:10]})")

    if report["open_cycles"]:
        lines.append("\n*Posizioni aperte:*")
        for c in report["open_cycles"]:
            pnl_open = (float(c["premium_received"]) - float(c["premium_current"])) * 100
            lines.append(
                f"  `{c['ticker']}` {c['phase'].replace('_', ' ').upper()} — "
                f"catturato `${pnl_open:.2f}` finora"
            )

    # Dividendi attesi
    div_lines = []
    for pos in positions:
        try:
            t = _get_ticker(pos["ticker"])
            ex_div = _next_ex_div(t)
            ann_div = _annual_dividend(t)
            if ann_div > 0:
                quarterly = ann_div / 4 * float(pos["shares"])
                div_lines.append(
                    f"  `{pos['ticker']}` — `${quarterly:.2f}` (ex-div: {ex_div or 'N/D'})"
                )
        except Exception:
            pass

    if div_lines:
        lines.append("\n*Dividendi attesi:*")
        lines.extend(div_lines)

    return "\n".join(lines)


# ── Check ciclo singolo ───────────────────────────────────────────────────────

def _check_cycle(cycle_row) -> None:
    from telegram_bot import send_alert

    ticker     = cycle_row["ticker"]
    cycle_id   = cycle_row["id"]
    strike     = float(cycle_row["strike"])
    expiry_str = cycle_row["expiry"]
    prem_recv  = float(cycle_row["premium_received"])
    phase      = cycle_row["phase"]  # covered_call | csp

    ticker_obj = _get_ticker(ticker)
    expiry_d   = date.fromisoformat(expiry_str)
    dte        = (expiry_d - date.today()).days

    if dte < 0:
        logger.info("Ciclo %s %s: scaduto — skippo (chiudi manualmente con /close %s)", ticker, expiry_str, ticker)
        send_alert(
            f"⏰ *CICLO SCADUTO — {ticker}*\n"
            f"Il ciclo `{phase}` strike `${strike:.1f}` scad `{expiry_str}` è scaduto.\n"
            f"Usa `/close {ticker}` per registrare il risultato e aprire il prossimo."
        )
        return

    # Aggiorna premium live
    opt_type  = "call" if phase == "covered_call" else "put"
    live_prem = _fetch_live_premium(ticker_obj, strike, expiry_str, opt_type)
    if live_prem is not None:
        db.update_wheel_premium(cycle_id, live_prem)
        prem_now = live_prem
    else:
        prem_now = float(cycle_row["premium_current"])

    pct_cap = (prem_recv - prem_now) / prem_recv if prem_recv > 0 else 0

    # Ricrea WheelPosition per HogueOptimizer
    pos_row = db.get_position(ticker)
    entry_price = float(pos_row["entry_price"]) if pos_row else strike

    position = WheelPosition(
        cycle_id=cycle_id, ticker=ticker, strike=strike,
        expiry=expiry_d, premium_received=prem_recv,
        premium_current=prem_now, entry_price=entry_price,
        roll_count=int(cycle_row["roll_count"]), phase=phase,
    )

    stock_price = _current_price(ticker_obj)

    # ── Ex-dividend risk check ──────────────────────────────────────────────
    # _check_dividend_warnings() salta i ticker con un ciclo aperto assumendo
    # che sia gestito qui — per le CSP il dividendo non e' un rischio di
    # assegnazione anticipata (chi eserciterebbe una put per pagare di piu'?),
    # quindi il check si applica solo alle covered call.
    if phase == "covered_call":
        ex_div = _next_ex_div(ticker_obj)
        if ex_div:
            days_to_div = (ex_div - date.today()).days
            if 0 <= days_to_div <= config.HOGUE_DIV_WARNING_DAYS and ex_div <= expiry_d:
                ann_div = _annual_dividend(ticker_obj)
                div_per_share = ann_div / 4  # approssimazione trimestrale
                intrinsic = max(stock_price - strike, 0) if stock_price else 0.0
                extrinsic = prem_now - intrinsic
                at_risk = intrinsic > 0 and extrinsic < div_per_share

                icon = "🚨" if at_risk else "💰"
                risk_line = (
                    f"*A RISCHIO ASSEGNAZIONE* — valore estrinseco (`${extrinsic:.2f}`) sotto il "
                    f"dividendo stimato (`${div_per_share:.2f}`). Valuta di ricomprare la call "
                    f"entro la sera prima dell'ex-div per non perdere le azioni."
                    if at_risk else
                    f"Estrinseco `${extrinsic:.2f}` sopra il dividendo stimato `${div_per_share:.2f}` — "
                    f"assegnazione anticipata improbabile per ora, ricontrolla avvicinandoti alla data."
                )
                send_alert(
                    f"{icon} *EX-DIVIDEND RISK — {ticker}*\n"
                    f"Ex-div: `{ex_div}` (tra {days_to_div} giorni)\n"
                    f"Stock: `${stock_price:.2f}` vs strike `${strike:.1f}` | Call: `${prem_now:.2f}`\n\n"
                    f"_{risk_line}_"
                )

    # ── Hogue checks ─────────────────────────────────────────────────────────
    close_action = _opt.check_early_close(position)
    if close_action.action == "close":
        profit_usd = (prem_recv - prem_now) * 100
        urgency = "immediate" if dte <= config.HOGUE_DTE_THRESHOLD else "normal"
        rule = "21-DTE" if dte <= config.HOGUE_DTE_THRESHOLD else "50%"
        msg = _fmt_advisory(
            cycle_row,
            action_str=f"CHIUDI la {phase.replace('_', ' ').upper()} — regola {rule}",
            detail=(
                f"Ricompra la {opt_type.upper()} {ticker} ${strike:.1f} scad {expiry_str} "
                f"a circa ${prem_now:.2f}. Profitto: ${profit_usd:.2f}. "
                f"Poi usa /open per il prossimo ciclo."
            ),
            urgency=urgency,
        )
        send_alert(msg)
        logger.info("Advisory CLOSE inviato: %s %s", ticker, expiry_str)
        return

    roll_action = _opt.check_roll_opportunity(position)
    if roll_action.action == "roll":
        d = roll_action.details
        msg = _fmt_advisory(
            cycle_row,
            action_str=f"ROLL UP-AND-OUT — strike ${d['new_strike']:.0f} scad {d['new_expiry']}",
            detail=(
                f"1) Ricompra Call ${strike:.1f} a ${d['cost_to_close']:.2f}\n"
                f"2) Vendi Call ${d['new_strike']:.0f} scad {d['new_expiry']} a ${d['new_premium']:.2f}\n"
                f"Credito netto: ${d['net_credit']:.2f} (roll #{d['roll_number']}/{config.HOGUE_MAX_ROLLS})"
            ),
            urgency="normal",
        )
        send_alert(msg)
        return

    if roll_action.action == "assigned":
        msg = _fmt_advisory(
            cycle_row,
            action_str="LASCIA ASSEGNARE — max roll raggiunto o roll a debito",
            detail=(
                f"Stock ${stock_price:.2f} > strike ${strike:.1f}. "
                f"Azioni vendute a ${strike:.1f} + premio ${prem_recv:.2f} incassato. "
                f"Poi usa /open {ticker} CSP per il prossimo ciclo."
            ),
            urgency="normal",
        )
        send_alert(msg)
        return

    # Collar check
    collar = _opt.calculate_collar(position, stock_price)
    if collar:
        send_alert(collar["telegram_msg"])
        return

    # Nessuna azione — solo log, no alert
    logger.info(
        "Cycle %s %s: %.0f%% catturato, %d DTE — hold",
        ticker, expiry_str, pct_cap * 100, dte,
    )


# ── Check dividendi su posizioni senza ciclo aperto ──────────────────────────

def _check_dividend_warnings(positions: list, open_cycles: list) -> None:
    from telegram_bot import send_alert

    open_tickers = {c["ticker"] for c in open_cycles}
    for pos in positions:
        ticker = pos["ticker"]
        if ticker in open_tickers:
            continue  # già gestito nel ciclo
        try:
            t = _get_ticker(ticker)
            ex_div = _next_ex_div(t)
            if ex_div:
                days = (ex_div - date.today()).days
                if 0 <= days <= 14:
                    ann_div = _annual_dividend(t)
                    shares  = float(pos["shares"])
                    quarterly = ann_div / 4 * shares
                    send_alert(
                        f"💰 *DIVIDENDO IN ARRIVO — {ticker}*\n"
                        f"Ex-dividend: `{ex_div}` (tra {days} giorni)\n"
                        f"Importo stimato: `${quarterly:.2f}` ({shares:.0f} azioni)\n"
                        f"Nessun ciclo aperto — perfetto, incasserai il dividendo."
                    )
        except Exception as exc:
            logger.debug("Dividend check %s: %s", ticker, exc)


# ── Daily check ───────────────────────────────────────────────────────────────

def _daily_check() -> None:
    from telegram_bot import send_alert

    logger.info("wheel_daemon: daily check avviato")
    open_cycles = db.get_open_cycles()
    positions   = db.get_all_positions()

    if not open_cycles and not positions:
        logger.info("wheel_daemon: nessun ciclo aperto e nessuna posizione tracciata")
        return

    for cycle in open_cycles:
        try:
            _check_cycle(cycle)
        except Exception as exc:
            logger.error("_check_cycle %s: %s", cycle["ticker"], exc)

    _check_dividend_warnings(positions, open_cycles)


def _weekly_report() -> None:
    from telegram_bot import send_alert

    positions = db.get_all_positions()
    now = datetime.utcnow()
    msg = _fmt_weekly_report(positions, now.year, now.month)
    send_alert(msg)
    logger.info("wheel_daemon: weekly report inviato")


# ── Loop principale ───────────────────────────────────────────────────────────

def _loop() -> None:
    global _last_check_date, _last_weekly_report

    logger.info("wheel_daemon: loop avviato")
    while True:
        try:
            now  = datetime.utcnow()
            today = now.date()

            # Daily check: una volta al giorno tra le 9:30 e le 10:30 UTC
            # (mercato US apre alle 14:30 CET = 13:30 UTC → check mattutino anticipato)
            in_window = 7 <= now.hour <= 8  # 9-10 CET
            if in_window and _last_check_date != today:
                _last_check_date = today
                _daily_check()

            # Weekly report: domenica mattina
            if today.weekday() == 6 and _last_weekly_report != today:
                _last_weekly_report = today
                _weekly_report()

        except Exception as exc:
            logger.error("wheel_daemon loop error: %s", exc)

        time.sleep(_CHECK_INTERVAL_SEC)


def start() -> threading.Thread:
    """Avvia il wheel daemon in background. Chiamato da main.py."""
    t = threading.Thread(target=_loop, name="wheel-daemon", daemon=True)
    t.start()
    logger.info("wheel_daemon: thread avviato")
    return t


def run_check_now() -> None:
    """Esegue il daily check immediatamente (per test o chiamata manuale da /wheel)."""
    _daily_check()
