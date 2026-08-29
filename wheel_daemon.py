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
from scipy.stats import norm

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


_SUGGEST_RISK_FREE = 0.05


def _bs_delta(spot: float, strike: float, dte: int, iv: float, right: str) -> float | None:
    """Delta Black-Scholes (valore assoluto, 0-1). None se input invalidi.
    Stessa formula di wheel_scanner._bs_put_delta, generalizzata a call/put —
    prima suggest_next_cycle selezionava per % OTM fissa (0.03), contraddicendo
    la regola di casa validata da backtest il 27/08 ("selezione per delta
    sempre, mai % fissa"). Trovato in deep audit 29/08/2026."""
    if spot <= 0 or strike <= 0 or dte <= 0 or iv <= 0:
        return None
    T = dte / 365.0
    d1 = (np.log(spot / strike) + (_SUGGEST_RISK_FREE + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
    return float(norm.cdf(d1)) if right == "call" else float(abs(norm.cdf(d1) - 1))


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

    # Selezione strike per delta Black-Scholes (mai % OTM fissa — regola di
    # casa validata dal backtest 27/08, disallineamento corretto 29/08/2026).
    right = "call" if phase == "cc" else "put"
    if phase == "cc":
        candidates = calls[calls["strike"] > spot].copy()
    else:
        candidates = puts[puts["strike"] < spot].copy()

    if candidates.empty:
        return {"error": "Nessuno strike idoneo trovato"}

    candidates = candidates.copy()
    candidates["mid"] = candidates.apply(_mid_price, axis=1)
    candidates["spread_pct"] = (candidates["ask"] - candidates["bid"]) / candidates["mid"].clip(0.01)
    candidates["spread_abs"] = candidates["ask"] - candidates["bid"]
    candidates["oi"] = candidates["openInterest"].fillna(0).astype(int)
    candidates["ann_ret"] = candidates["mid"] / spot * (365 / max(dte, 1)) * 100
    candidates["delta"] = candidates.apply(
        lambda r: _bs_delta(spot, r["strike"], dte, float(r.get("impliedVolatility") or 0.0), right),
        axis=1,
    )

    # Filtri — stessi di wheel_scanner.py, letti da config invece che
    # ridichiarati hardcoded (spread_pct<=0.20 qui vs config 0.10 era
    # un'incoerenza diretta, trovata in deep audit 29/08/2026).
    candidates = candidates[candidates["mid"] >= config.WHEEL_MIN_PREMIUM]
    candidates = candidates[
        (candidates["spread_pct"] <= config.WHEEL_MAX_SPREAD_PCT)
        | (candidates["spread_abs"] <= config.WHEEL_MAX_SPREAD_ABS)
    ]
    candidates = candidates[candidates["oi"] >= config.WHEEL_OI_MIN]
    candidates = candidates[candidates["delta"].notna()]
    candidates = candidates[
        (candidates["delta"] >= config.WHEEL_PUT_DELTA_MIN) & (candidates["delta"] <= config.WHEEL_PUT_DELTA_MAX)
    ]

    if candidates.empty:
        return {"error": "Nessun candidato dopo filtri (delta/OI/spread)"}

    # Migliore: delta più vicino al bordo superiore della banda (stesso
    # target di backtest_ford.py TARGET_DELTA=0.30) con OI alto
    target_delta = config.WHEEL_PUT_DELTA_MAX
    candidates["_score"] = (candidates["delta"] - target_delta).abs()
    best = candidates.sort_values("_score").iloc[0]

    ann_ret     = float(best["ann_ret"])
    mid         = float(best["mid"])
    strike      = float(best["strike"])
    oi          = int(best["oi"])
    best_delta  = float(best["delta"])
    net_premium = mid * 100 - 2 * config.WHEEL_COMMISSION_PER_ORDER
    ann_ret_net = (net_premium / 100 / strike) * (365 / max(dte, 1)) * 100

    return {
        "ticker":          ticker,
        "phase":           phase.upper(),
        "spot":            round(spot, 2),
        "strike":          round(strike, 2),
        "expiry":          expiry,
        "dte":             dte,
        "mid":             round(mid, 2),
        "delta":           round(best_delta, 3),
        "ann_ret":         round(ann_ret, 1),
        "ann_ret_net":     round(ann_ret_net, 1),
        "iv_rank":         round(iv_rank, 1) if iv_rank is not None else None,
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
    iv_rank_str = f"{s['iv_rank']:.0f}%" if s['iv_rank'] is not None else "n/d"

    return (
        f"📊 *SUGGEST — {s['ticker']} ({s['phase']})*\n"
        f"Spot: `${s['spot']:.2f}` | IV Rank: `{iv_rank_str}` | ATM IV: `{s['atm_iv_pct']:.0f}%`\n\n"
        f"*{ordine} {phase_label}:*\n"
        f"  Strike: `${s['strike']:.2f}` (delta `{s['delta']:.2f}`) | Scad: `{s['expiry']}` ({s['dte']} DTE)\n"
        f"  Premio mid: `${s['mid']:.2f}` → `${s['mid']*100:.0f}` per contratto\n"
        f"  Ann. return netto: `{s['ann_ret_net']:.1f}%` (lordo {s['ann_ret']:.1f}%) | OI: `{s['oi']:,}`\n"
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

    # check_roll_opportunity() (CC) e check_roll_opportunity_put() (CSP) sono
    # ora due funzioni simmetriche e distinte — prima girava solo la logica CC
    # anche sulle CSP con la direzione invertita (poteva sopprimere un roll
    # difensivo necessario, o proporre "vendi una call" su una short-put).
    # Roll down-and-out per le CSP implementato in deep audit 29/08/2026,
    # backlog #11.
    if phase == "covered_call":
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
    else:  # csp
        roll_action = _opt.check_roll_opportunity_put(position)
        if roll_action.action == "roll":
            d = roll_action.details
            msg = _fmt_advisory(
                cycle_row,
                action_str=f"ROLL DOWN-AND-OUT — strike ${d['new_strike']:.0f} scad {d['new_expiry']}",
                detail=(
                    f"1) Ricompra CSP ${strike:.1f} a ${d['cost_to_close']:.2f}\n"
                    f"2) Vendi CSP ${d['new_strike']:.0f} scad {d['new_expiry']} a ${d['new_premium']:.2f}\n"
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
                    f"Stock ${stock_price:.2f} < strike ${strike:.1f}. "
                    f"Azioni comprate a ${strike:.1f} + premio ${prem_recv:.2f} incassato. "
                    f"Poi usa /open {ticker} CC per il prossimo ciclo."
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


# ── Rottura tesi (sostituisce lo stop-loss a prezzo) ─────────────────────────

def _check_thesis_break(positions: list) -> None:
    """
    Controllo su rottura di tesi, non su prezzo. La strategia compra titoli
    che si e' disposti a tenere per il dividendo (config.WHEEL_TIER1_UNIVERSE
    curata apposta) — uno stop-loss a prezzo venderebbe proprio quando si
    vorrebbe tenere/mediare. Il vero segnale di uscita e' "il motivo per cui
    lo tengo non vale piu'": dividendo tagliato/sospeso.

    Il check "nessun dividendo rilevato" gira solo sui ticker Tier-1 (scelti
    apposta per il dividendo) — su posizioni stock-picking pure (es. ALTO,
    SRI, mai state dividend play) darebbe falsi positivi permanenti.
    """
    from telegram_bot import send_alert
    import seeking_alpha_feed

    for pos in positions:
        ticker = pos["ticker"]
        try:
            news = seeking_alpha_feed.get_recent_news(ticker)
            if news.get("sa_div_cut"):
                send_alert(
                    f"🚨 *POSSIBILE TAGLIO DIVIDENDO — {ticker}*\n"
                    f"Notizia: _{news.get('sa_latest_headline') or 'n/d'}_\n"
                    f"La tesi per cui tieni questo titolo (dividendo) potrebbe essersi rotta — "
                    f"verifica, non è un alert di prezzo."
                )
                db.log_decision(
                    "thesis_break", f"Possibile taglio dividendo — {news.get('sa_latest_headline') or 'n/d'}",
                    ticker=ticker, source="_check_thesis_break",
                )
                continue

            if ticker in config.WHEEL_TIER1_UNIVERSE:
                t = _get_ticker(ticker)
                ann_div = _annual_dividend(t)
                if not ann_div:
                    send_alert(
                        f"⚠️ *DIVIDENDO NON RILEVATO — {ticker}*\n"
                        f"Nessun dividend rate annuo riportato — verifica se è stato "
                        f"sospeso (titolo Tier-1, scelto apposta per il dividendo)."
                    )
                    db.log_decision(
                        "thesis_break", "Dividend rate annuo non rilevato su titolo Tier-1",
                        ticker=ticker, source="_check_thesis_break",
                    )
        except Exception as exc:
            logger.debug("Thesis-break check %s: %s", ticker, exc)


# ── Scan proattivo universo Tier-1 ────────────────────────────────────────────

def _daily_universe_scan() -> None:
    """
    Rispetto a _check_cycle() (reattivo, sui cicli gia' aperti) e al confronto
    in telegram_bot.dispatch_signal() (reattivo, sui nuovi segnali in arrivo),
    questo scan e' proattivo: ogni mattina ripassa l'intero universo Tier-1
    (config.WHEEL_TIER1_UNIVERSE) a prescindere da segnali, e propone il
    miglior candidato non ancora aperto — "come se i soldi fossero nostri":
    lettura dei dati, non previsione di mercato.
    """
    from telegram_bot import send_alert
    import wheel_scanner as ws

    open_tickers = {c["ticker"] for c in db.get_open_cycles()}
    candidates = []

    for ticker in config.WHEEL_TIER1_UNIVERSE:
        if ticker in open_tickers:
            continue
        try:
            r = ws.scan_wheel_candidate(ticker)
            if r and r.best_put:
                candidates.append((ticker, r.best_put, r.div_yield))
        except Exception as exc:
            logger.debug("Universe scan %s: %s", ticker, exc)

    if not candidates:
        logger.info("wheel_daemon: universe scan — nessun candidato Tier-1 valido oggi")
        return

    candidates.sort(key=lambda c: c[1].annualized_return_net, reverse=True)
    best_ticker, best_put, div_yield = candidates[0]

    # Dedup: manda l'alert solo se il candidato migliore e' cambiato in modo
    # rilevante rispetto all'ultimo digest inviato — altrimenti e' rumore
    # ripetuto ogni mattina che si finisce per ignorare dopo una settimana.
    last = db.get_last_universe_scan()
    ANN_RET_CHANGE_THRESHOLD = 2.0  # punti percentuali
    changed = (
        last is None
        or last["ticker"] != best_ticker
        or last["strike"] != best_put.strike
        or last["expiry"] != best_put.expiry
        or abs((last["ann_return"] or 0) - best_put.annualized_return_net) >= ANN_RET_CHANGE_THRESHOLD
    )
    if not changed:
        logger.info("wheel_daemon: universe scan invariato (%s %.1f%%) — alert soppresso", best_ticker, best_put.annualized_return_net)
        return

    lines = [f"🔍 *SCAN UNIVERSO TIER-1 — {best_ticker} in testa*\n"]
    lines.append(
        f"Strike `${best_put.strike}` scad `{best_put.expiry}` (delta {best_put.delta}) — "
        f"`{best_put.annualized_return_net:.1f}%/anno` annualizzato"
        + (f", dividendo `{div_yield:.1f}%`" if div_yield else "")
    )
    if len(candidates) > 1:
        others = ", ".join(f"{t} {p.annualized_return_net:.1f}%" for t, p, _ in candidates[1:4])
        lines.append(f"Altri candidati: {others}")
    lines.append(f"\nUsa `/open {best_ticker} CSP {best_put.strike} {best_put.expiry} {best_put.mid:.2f}` per registrarlo dopo l'esecuzione manuale su IBKR.")

    send_alert("\n".join(lines))
    db.set_last_universe_scan(best_ticker, best_put.strike, best_put.expiry, best_put.annualized_return_net)
    db.log_decision(
        "universe_scan_top",
        f"Strike ${best_put.strike} scad {best_put.expiry}, delta {best_put.delta}, "
        f"ann.ret {best_put.annualized_return_net:.1f}%, OI {best_put.open_interest} — "
        f"migliore tra {len(candidates)} candidati Tier-1 validi oggi",
        ticker=best_ticker, source="_daily_universe_scan",
    )
    logger.info("wheel_daemon: universe scan inviato — top %s (%.1f%%)", best_ticker, best_put.annualized_return_net)


# ── Sync capitale con broker ──────────────────────────────────────────────────

def _sync_capital_ledger() -> None:
    """
    Riallinea capital_log al cash reale IBKR — db.sync_capital_from_broker()
    esisteva da stamattina ma non era mai stata agganciata a nulla (trovato
    da code review, 29/08/2026). Senza questo, il ledger torna a derivare in
    silenzio ad ogni trade eseguito manualmente e non registrato via /open,
    /close — esattamente il problema gia' risolto una volta oggi a mano.
    """
    try:
        from ibkr_connector import CPClient
        cp = CPClient()
        if not cp.auth_status():
            logger.debug("_sync_capital_ledger: Gateway non raggiungibile, skip")
            return
        summary = cp.get_account_summary()
        if not summary:
            return
        db.sync_capital_from_broker(summary.total_cash, "Sync automatico giornaliero")
        logger.info("wheel_daemon: capital ledger riallineato a $%.2f", summary.total_cash)
    except Exception as exc:
        logger.debug("_sync_capital_ledger: %s", exc)


# ── Daily check ───────────────────────────────────────────────────────────────

def _daily_check() -> None:
    from telegram_bot import send_alert

    logger.info("wheel_daemon: daily check avviato")
    open_cycles = db.get_open_cycles()
    positions   = db.get_all_positions()

    try:
        _sync_capital_ledger()
    except Exception as exc:
        logger.error("_sync_capital_ledger: %s", exc)

    try:
        from telegram_bot import check_monthly_drawdown
        check_monthly_drawdown()
    except Exception as exc:
        logger.error("check_monthly_drawdown: %s", exc)

    # Kill-switch attivo: niente nuovi suggerimenti di ciclo — le posizioni
    # gia' aperte restano comunque monitorate/difese sotto (early close/roll/
    # assegnazione), il kill-switch blocca solo l'apertura di nuovo rischio.
    # check_monthly_drawdown()/_daily_universe_scan() erano scritte ma mai
    # collegate — trovato in deep audit 29/08/2026, backlog #5.
    if db.is_paused():
        logger.info("wheel_daemon: bot in pausa (drawdown mensile) — scan universo saltato")
    else:
        try:
            _daily_universe_scan()
        except Exception as exc:
            logger.error("_daily_universe_scan: %s", exc)

    if not open_cycles and not positions:
        logger.info("wheel_daemon: nessun ciclo aperto e nessuna posizione tracciata")
        return

    for cycle in open_cycles:
        try:
            _check_cycle(cycle)
        except Exception as exc:
            logger.error("_check_cycle %s: %s", cycle["ticker"], exc)

    _check_dividend_warnings(positions, open_cycles)
    _check_thesis_break(positions)


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
