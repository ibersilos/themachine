"""
Telegram bot – alert dispatch + kill-switch enforcement.

Commands available:
  /status  – current risk state, pause status, monthly PnL
  /signals – last 10 scored signals above threshold
  /risk    – current drawdown and position risk summary

/pause is intentionally NOT implemented (kill switch is automatic only).
"""
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters
from telegram.constants import ParseMode

import config
import database as db
from scoring_engine import ScoreBreakdown

logger = logging.getLogger(__name__)

_TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


# ── Kill-switch enforcement ───────────────────────────────────────────────────

def check_stop_loss(ticker: str, entry_price: float, current_price: float) -> bool:
    """
    Returns True and fires an alert if the position has hit the stop loss.
    This does NOT execute any trade – it only alerts.
    """
    if entry_price <= 0:
        return False

    pct_change = (current_price - entry_price) / entry_price

    if pct_change <= -config.STOP_LOSS_PCT:
        loss_pct = abs(pct_change) * 100
        msg = (
            f"🚨 *STOP LOSS TRIGGERED*\n\n"
            f"Ticker: `{ticker}`\n"
            f"Entry: `${entry_price:.2f}`\n"
            f"Current: `${current_price:.2f}`\n"
            f"Loss: `{loss_pct:.1f}%` (threshold {config.STOP_LOSS_PCT*100:.0f}%)\n\n"
            f"⚠️ _Manual review required. No position auto-closed._"
        )
        send_alert(msg)
        return True
    return False


def check_monthly_drawdown(delta_pct: float) -> None:
    """
    Updates monthly PnL and auto-pauses the bot for DRAWDOWN_PAUSE_DAYS
    if MAX_MONTHLY_DRAWDOWN is breached.
    """
    db.update_monthly_pnl(delta_pct)
    state = db.get_risk_state()
    monthly_pnl = state["monthly_pnl_pct"]

    if monthly_pnl <= -config.MAX_MONTHLY_DRAWDOWN:
        pause_until = datetime.utcnow() + timedelta(days=config.DRAWDOWN_PAUSE_DAYS)
        db.set_pause(pause_until)
        msg = (
            f"🔴 *AUTO-PAUSE ACTIVATED*\n\n"
            f"Monthly drawdown: `{abs(monthly_pnl)*100:.1f}%`\n"
            f"Threshold: `{config.MAX_MONTHLY_DRAWDOWN*100:.0f}%`\n"
            f"Paused until: `{pause_until.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
            f"_All alerts suppressed for {config.DRAWDOWN_PAUSE_DAYS} days._"
        )
        send_alert(msg)
        logger.warning("AUTO-PAUSE activated until %s", pause_until)


# ── Alert formatting ──────────────────────────────────────────────────────────

def _fmt_cap(cap) -> str:
    if cap is None:
        return "N/A"
    if cap >= 1_000_000_000:
        return f"${cap/1e9:.1f}B"
    return f"${cap/1e6:.0f}M"


def _fmt_vol(vol) -> str:
    if vol is None:
        return "N/A"
    if vol >= 1_000_000:
        return f"{vol/1e6:.1f}M"
    return f"{vol/1_000:.0f}K"


def _52w_bar(price, low, high, width=10) -> str:
    if not price or not low or not high or high <= low:
        return ""
    pct = (price - low) / (high - low)
    filled = round(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"`{bar}` {pct*100:.0f}%"


def _verdict_line(bd: ScoreBreakdown) -> str:
    """Riga di verdetto finale — riassume in linguaggio semplice il tier gia' calcolato."""
    labels = {
        "STRONG BUY": "🔥 *STRONG BUY* — segnale forte, priorita' alta",
        "BUY ALERT":  "📡 *BUY ALERT* — sopra soglia, vale un'occhiata",
        "WATCH":      "👁 *WATCH* — sotto soglia operativa, solo osservazione",
    }
    return labels.get(bd.tier(), bd.tier())


def _format_pick_alert(bd: ScoreBreakdown, signal: dict) -> str:
    """Layout stock scanner per pipeline STOCK_PICKING."""
    ticker  = bd.ticker or "N/A"
    url     = signal.get("filing_url") or signal.get("award_url") or ""
    source_labels = {"form4": "Form 4 Insider", "usaspending": "USAspending"}
    source  = source_labels.get(signal.get("source", ""), signal.get("source", ""))

    price   = signal.get("current_price")
    cap     = signal.get("market_cap")
    vol     = signal.get("avg_volume")
    low52   = signal.get("52w_low")
    high52  = signal.get("52w_high")
    bar     = _52w_bar(price, low52, high52)

    lines = [
        f"🎯 *[PICK] {bd.tier()} — {ticker}*",
        f"Score: `{bd.total}/100`  |  {source}",
        "─────────────────────────",
        f"💰 Prezzo:   `{'$%.2f' % price if price else 'N/A'}`",
        f"📊 Volume:   `{_fmt_vol(vol)}/day`",
        f"🏦 Cap:      `{_fmt_cap(cap)}`",
    ]
    if bar:
        lines.append(f"📈 52w:      {bar}")
        if low52 and high52:
            lines.append(f"            `${low52:.2f}` ── `${high52:.2f}`")
    lines.append("─────────────────────────")
    if bd.flags:
        for f in bd.flags:
            lines.append(f"  • {f}")
    lines.append("─────────────────────────")
    lines.append(_verdict_line(bd))
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\n🕐 `{ts}`")
    if url:
        lines.append(f"[View filing]({url})")
    return "\n".join(lines)


def _format_wheel_alert(bd: ScoreBreakdown, signal: dict) -> str:
    """Layout options desk per pipeline WHEEL_CANDIDATES."""
    ticker  = bd.ticker or "N/A"
    url     = signal.get("filing_url") or signal.get("award_url") or ""
    source_labels = {"edgar_8k": "SEC 8-K"}
    source  = source_labels.get(signal.get("source", ""), signal.get("source", ""))

    cap    = signal.get("market_cap")
    price  = signal.get("current_price")
    sector = signal.get("sector") or ""
    vrp    = signal.get("vrp")
    hv_20  = signal.get("hv_20")
    atm_iv = signal.get("atm_iv")
    ws     = signal.get("wheel_scan")   # WheelScanResult

    # VRP line (logica stockpile: IV/HV20)
    if vrp is not None and atm_iv is not None and hv_20 is not None:
        vrp_str = f"{vrp:.2f}x  (IV {atm_iv*100:.0f}% vs HV20 {hv_20*100:.0f}%)"
    elif vrp is not None:
        vrp_str = f"{vrp:.2f}x"
    else:
        vrp_str = "N/A  (calcolo post-scan)"

    lines = [
        f"⚙️ *[WHEEL] {bd.tier()} — {ticker}*",
        f"Score: `{bd.total}/100`  |  {source}",
        "─────────────────────────",
        f"📊 VRP:      `{vrp_str}`",
        f"💵 Prezzo:   `{'$%.2f' % price if price else 'N/A'}`",
        f"🏦 Cap:      `{_fmt_cap(cap)}`",
    ]
    if sector:
        lines.append(f"🏭 Settore:  `{sector}`")

    # Miglior put candidata (wheel_scanner)
    if ws and ws.best_put:
        bp = ws.best_put
        lines.append("─────────────────────────")
        lines.append(f"🎯 *Miglior Put da vendere:*")
        lines.append(f"   Strike:  `${bp.strike:.0f}`  |  Scad: `{bp.expiry}` ({bp.dte}gg)")
        lines.append(f"   Premio:  `${bp.mid:.2f}`  |  Ann.Ret: `{bp.annualized_return:.1f}%`")
        lines.append(f"   OI:      `{bp.open_interest:,}`  |  Spread: `{bp.spread_pct*100:.1f}%`")

    # Earnings status
    if ws:
        earn_str = f"⚠️ {ws.next_earnings} — NELLA FINESTRA" if ws.earnings_in_window \
                   else (f"✅ {ws.next_earnings}" if ws.next_earnings else "✅ Nessuno in finestra")
        lines.append(f"📅 Earnings: `{earn_str}`")
        if not ws.above_sma50:
            lines.append("⚠️ `Sotto 50-SMA — trend ribassista`")

    lines.append("─────────────────────────")
    if bd.flags:
        for f in bd.flags:
            lines.append(f"  • {f}")
    lines.append("─────────────────────────")
    lines.append(_verdict_line(bd))
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\n🕐 `{ts}`")
    if url:
        lines.append(f"[View filing]({url})")
    return "\n".join(lines)


def _format_signal_alert(bd: ScoreBreakdown, signal: dict) -> str:
    if bd.pipeline == "stock_picking":
        return _format_pick_alert(bd, signal)
    if bd.pipeline == "wheel_candidate":
        return _format_wheel_alert(bd, signal)
    # fallback generico
    ticker = bd.ticker or "N/A"
    url    = signal.get("filing_url") or signal.get("award_url") or ""
    lines  = [
        f"{bd.emoji()} *{bd.tier()} — {ticker}*",
        f"Score: `{bd.total}/100`",
        "",
    ]
    for f in bd.flags:
        lines.append(f"  • {f}")
    lines.append("")
    lines.append(_verdict_line(bd))
    if url:
        lines.append(f"\n[View filing]({url})")
    lines.append(f"\n🕐 `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`")
    return "\n".join(lines)


# ── Public send functions ─────────────────────────────────────────────────────

def send_alert(text: str) -> None:
    """
    Fire-and-forget alert — httpx sync, funziona da qualsiasi thread/contesto.
    Se il Markdown e' malformato (es. underscore/asterisco spaiati) Telegram
    risponde 400 senza sollevare eccezione lato client — prima veniva ignorato
    silenziosamente. Ora logga l'errore e ritenta in plain text, cosi' il
    messaggio arriva comunque invece di sparire senza traccia.
    """
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(f"{_TG_API}/sendMessage", json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
            if resp.status_code != 200:
                logger.error("Telegram send failed (%d): %s — ritento in plain text", resp.status_code, resp.text)
                client.post(f"{_TG_API}/sendMessage", json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": True,
                })
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)


def dispatch_signal(bd: ScoreBreakdown, signal: dict) -> None:
    """
    Send a scored signal alert if it clears the threshold and bot is not paused.
    This is the main entry point called by main.py after scoring.
    """
    if db.is_paused():
        logger.info("Bot paused – suppressing alert for %s", bd.ticker)
        return

    if bd.total < config.MIN_ALERT_SCORE:
        return

    if db.was_recently_alerted(signal.get("source", ""), bd.ticker):
        logger.info("Alert soppresso (gia' inviato di recente): %s %s", signal.get("source"), bd.ticker)
        return

    msg = _format_signal_alert(bd, signal)

    # Confronto wheel vs trading direzionale — solo sui tier azionabili
    # (BUY ALERT/STRONG BUY), per non pagare uno scan opzioni live su ogni
    # WATCH scartabile. "Come se i soldi fossero nostri": confronto basato
    # su dati storici reali (backtest_engine), non previsioni.
    if bd.tier() != "WATCH" and bd.ticker:
        try:
            import strategy_advisor
            cmp = strategy_advisor.compare(bd.ticker, signal.get("source", ""), bd.total)
            msg += f"\n\n---\n*Wheel vs Trading:*\n{cmp.format()}"
        except Exception as exc:
            logger.warning("strategy_advisor.compare fallito per %s: %s", bd.ticker, exc)

    send_alert(msg)

    sig_id = signal.get("_db_id")
    if sig_id:
        db.mark_alerted(sig_id)
        with db.tx() as conn:
            conn.execute(
                "UPDATE signals SET score=?, pipeline=? WHERE id=?",
                (bd.total, bd.pipeline, sig_id),
            )


# ── Bot command handlers ──────────────────────────────────────────────────────

async def _cmd_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /wheel — mostra posizioni aperte + azione Hogue consigliata per ciascuna.
    Esegue un check immediato (come il daily check) e risponde in chat.
    """
    import wheel_daemon
    open_cycles = db.get_open_cycles()
    positions   = db.get_all_positions()

    if not open_cycles and not positions:
        await update.message.reply_text(
            "Nessuna posizione tracciata.\n"
            "Usa /open per registrare un ciclo o aggiungi azioni con /addpos.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Header posizioni azionarie
    lines = ["📊 *WHEEL DASHBOARD*\n"]
    if positions:
        lines.append("*Azioni in portafoglio:*")
        for p in positions:
            ticker = p["ticker"]
            try:
                from covered_call_optimizer import _get_ticker, _current_price
                t = _get_ticker(ticker)
                spot = _current_price(t)
                cost = float(p["entry_price"])
                shares = float(p["shares"])
                gain = (spot - cost) * shares if spot else 0
                gain_pct = (spot - cost) / cost * 100 if spot and cost else 0
                lines.append(
                    f"  `{ticker}` — {shares:.0f}az @ `${cost:.2f}` | "
                    f"ora `${spot:.2f}` ({gain_pct:+.1f}%, `${gain:+.0f}`)"
                )
            except Exception:
                lines.append(f"  `{ticker}` — {float(p['shares']):.0f}az @ `${float(p['entry_price']):.2f}`")

    # Cicli aperti
    if open_cycles:
        lines.append("\n*Cicli aperti:*")
        for c in open_cycles:
            prem_r = float(c["premium_received"])
            prem_c = float(c["premium_current"])
            pct    = int((prem_r - prem_c) / prem_r * 100) if prem_r > 0 else 0
            dte    = (db.date.fromisoformat(c["expiry"]) - db.date.today()).days if hasattr(db, 'date') else "?"
            try:
                from datetime import date as _date
                dte = (_date.fromisoformat(c["expiry"]) - _date.today()).days
            except Exception:
                dte = "?"
            lines.append(
                f"  `{c['ticker']}` {c['phase'].replace('_', ' ').upper()} `${float(c['strike']):.1f}` "
                f"scad `{c['expiry']}` ({dte} DTE) — {pct}% catturato"
            )
    else:
        lines.append("\n_Nessun ciclo aperto. Usa /open per registrarne uno._")

    lines.append("\n_Eseguo check Hogue..._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # Esegui check e invia advisory separati
    for cycle in open_cycles:
        try:
            wheel_daemon._check_cycle(cycle)
        except Exception as exc:
            logger.error("_cmd_wheel check_cycle %s: %s", cycle["ticker"], exc)


async def _cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /open <TICKER> <CC|CSP> <STRIKE> <SCADENZA> <PREMIO>
    Es: /open F CC 15.0 2026-08-21 0.39

    Registra un nuovo ciclo wheel aperto nel DB.
    """
    args = context.args
    if not args or len(args) < 5:
        await update.message.reply_text(
            "Uso: `/open TICKER CC|CSP STRIKE SCADENZA PREMIO`\n"
            "Es: `/open F CC 15.0 2026-08-21 0.39`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        ticker  = args[0].upper()
        phase   = args[1].lower()
        if phase not in ("cc", "csp"):
            raise ValueError("Fase deve essere CC o CSP")
        phase_db = "covered_call" if phase == "cc" else "csp"
        strike   = float(args[2])
        expiry   = args[3]
        premium  = float(args[4])
        from datetime import date as _date
        _date.fromisoformat(expiry)  # valida formato
    except (ValueError, IndexError) as exc:
        await update.message.reply_text(f"Errore nei parametri: {exc}", parse_mode=ParseMode.MARKDOWN)
        return

    cycle_id = db.open_wheel_cycle(
        ticker=ticker, strike=strike, expiry=expiry,
        premium_received=premium, phase=phase_db,
    )

    pnl_per_contract = premium * 100
    from datetime import date as _date
    dte = (_date.fromisoformat(expiry) - _date.today()).days
    ann_ret = (premium / strike) * (365 / max(dte, 1)) * 100

    # Registra incasso premio sul capitale (il premio arriva subito in cassa)
    new_bal = db.log_capital(
        "premium_in", pnl_per_contract, ticker,
        f"{phase.upper()} ${strike:.2f} scad {expiry} — premio incassato",
    )

    await update.message.reply_text(
        f"✅ *Ciclo registrato* (ID {cycle_id})\n\n"
        f"`{ticker}` {phase.upper()} strike `${strike:.2f}` scad `{expiry}` ({dte} DTE)\n"
        f"Premio: `${premium:.2f}` → `${pnl_per_contract:.2f}` per contratto\n"
        f"Ann. return stimato: `{ann_ret:.1f}%`\n"
        f"Capitale cash: `${new_bal:.2f}`\n\n"
        f"_Riceverai advisory automatici ogni mattina._\n"
        f"Chiudi con `/close {ticker}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /close <TICKER> [PREMIO_CHIUSURA]
    Es: /close F 0.19
    Se non specifichi il premio di chiusura, usa il valore live da yfinance.
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: `/close TICKER [PREMIO_CHIUSURA]`\n"
            "Es: `/close F 0.19` (o `/close F` per usare prezzo live)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ticker   = args[0].upper()
    cycle    = db.get_open_cycle(ticker)
    if not cycle:
        await update.message.reply_text(
            f"Nessun ciclo aperto trovato per `{ticker}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Premio di chiusura: da argomento o da yfinance
    if len(args) >= 2:
        try:
            close_prem = float(args[1])
        except ValueError:
            await update.message.reply_text("Premio di chiusura non valido.", parse_mode=ParseMode.MARKDOWN)
            return
    else:
        from covered_call_optimizer import _get_ticker, _fetch_live_premium
        t   = _get_ticker(ticker)
        opt = "call" if cycle["phase"] == "covered_call" else "put"
        close_prem = _fetch_live_premium(t, float(cycle["strike"]), cycle["expiry"], opt)
        if close_prem is None:
            close_prem = 0.0
            await update.message.reply_text(
                f"Impossibile leggere il premio live — uso $0.00 (scaduta senza valore).",
                parse_mode=ParseMode.MARKDOWN,
            )

    prem_recv  = float(cycle["premium_received"])
    pnl        = (prem_recv - close_prem) * 100
    pct_cap    = int((prem_recv - close_prem) / prem_recv * 100) if prem_recv > 0 else 100
    phase_lbl  = "CC" if cycle["phase"] == "covered_call" else "CSP"

    notes = f"Chiuso manualmente via Telegram. Premio chiusura: ${close_prem:.2f}"
    db.close_wheel_cycle(cycle["id"], pnl, notes)

    # Se il costo di chiusura > 0, esci la differenza dal capitale
    # (il premio era già entrato a /open; qui correggiamo per il buyback)
    if close_prem > 0:
        buyback_cost = close_prem * 100
        new_bal = db.log_capital(
            "premium_out", -buyback_cost, ticker,
            f"{phase_lbl} buyback ${float(cycle['strike']):.2f} — costo riacquisto",
        )
    else:
        cap = db.get_capital()
        new_bal = cap["balance"]

    # Determina prossima fase
    next_phase = "CSP" if cycle["phase"] == "covered_call" else "CC"

    await update.message.reply_text(
        f"✅ *Ciclo chiuso — {ticker}*\n\n"
        f"`{phase_lbl}` strike `${float(cycle['strike']):.2f}` scad `{cycle['expiry']}`\n"
        f"Premio aperto: `${prem_recv:.2f}` | chiuso: `${close_prem:.2f}`\n"
        f"Catturato: `{pct_cap}%` | PnL: `${pnl:+.2f}`\n"
        f"Capitale cash: `${new_bal:.2f}`\n\n"
        f"_Prossimo ciclo: apri una {next_phase} con /open {ticker} {next_phase} ..._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_suggest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /suggest <TICKER> [CC|CSP]
    Es: /suggest F CC   — analisi real-time + migliore CC da vendere adesso
        /suggest F CSP  — migliore CSP da vendere
        /suggest F      — deduce la fase dal ciclo aperto o dalle posizioni
    """
    import wheel_daemon
    args = context.args
    if not args:
        await update.message.reply_text(
            "Uso: `/suggest TICKER [CC|CSP]`\nEs: `/suggest F CC`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ticker = args[0].upper()

    # Deduce fase se non specificata
    if len(args) >= 2:
        phase = args[1].lower()
        if phase not in ("cc", "csp"):
            phase = "cc"
    else:
        cycle = db.get_open_cycle(ticker)
        pos   = db.get_position(ticker)
        if cycle:
            phase = "csp" if cycle["phase"] == "covered_call" else "cc"
        elif pos and float(pos["shares"]) > 0:
            phase = "cc"
        else:
            phase = "cc"

    await update.message.reply_text(
        f"🔍 Analizzo {ticker} ({phase.upper()})...",
        parse_mode=ParseMode.MARKDOWN,
    )

    result = wheel_daemon.suggest_next_cycle(ticker, phase)
    msg    = wheel_daemon._fmt_suggest(result)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def _cmd_addpos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /addpos <TICKER> <AZIONI> <COSTO_MEDIO> [DATA]
    Es: /addpos F 100 13.50 2024-06-01
    Registra una posizione azionaria nel portafoglio.
    """
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Uso: `/addpos TICKER AZIONI COSTO_MEDIO [DATA]`\n"
            "Es: `/addpos F 100 13.50 2024-06-01`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        ticker    = args[0].upper()
        shares    = float(args[1])
        cost      = float(args[2])
        entry_dt  = args[3] if len(args) >= 4 else None
    except (ValueError, IndexError) as exc:
        await update.message.reply_text(f"Errore parametri: {exc}", parse_mode=ParseMode.MARKDOWN)
        return

    db.upsert_position(ticker, cost, shares, entry_dt)

    total_cost = cost * shares
    await update.message.reply_text(
        f"✅ *Posizione aggiunta — {ticker}*\n\n"
        f"Azioni: `{shares:.0f}` | Costo medio: `${cost:.2f}`\n"
        f"Capitale investito: `${total_cost:.2f}`\n\n"
        f"Usa `/suggest {ticker} CC` per vedere la miglior covered call da vendere.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /income [MESE] [ANNO]
    Es: /income        — mese corrente
        /income 6      — giugno anno corrente
        /income 6 2026 — giugno 2026
    Report income: premi realizzati + non realizzati + dividendi attesi.
    """
    import wheel_daemon
    args = context.args
    from datetime import datetime as _dt
    now = _dt.utcnow()
    month = int(args[0]) if args else now.month
    year  = int(args[1]) if len(args) >= 2 else now.year

    positions = db.get_all_positions()
    msg = wheel_daemon._fmt_weekly_report(positions, year, month)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def _cmd_capital(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /capital — stato del capitale: cash, investito in azioni, totale, crescita.
    Opzionale: /capital log  — mostra ultimi movimenti.
    """
    import yfinance as yf
    cap  = db.get_capital()
    seed = cap["seed"]
    cash = cap["balance"]

    # Calcola valore azioni in portafoglio
    positions = db.get_all_positions()
    invested  = 0.0
    pos_lines = []
    for p in positions:
        try:
            price = yf.Ticker(p["ticker"]).fast_info.get("last_price") or float(p["entry_price"])
        except Exception:
            price = float(p["entry_price"])
        val = price * float(p["shares"])
        invested += val
        cost_val  = float(p["entry_price"]) * float(p["shares"])
        gain_pct  = (val - cost_val) / cost_val * 100 if cost_val else 0
        pos_lines.append(
            f"  `{p['ticker']}` {float(p['shares']):.0f}az "
            f"@ `${price:.2f}` = `${val:.2f}` ({gain_pct:+.1f}%)"
        )

    total    = cash + invested
    growth   = total - seed
    growth_p = growth / seed * 100 if seed else 0

    lines = [
        "💼 *CAPITALE THE MACHINE*\n",
        f"Seed iniziale:   `${seed:.2f}`",
        f"Cash disponibile: `${cash:.2f}`",
        f"Azioni in ptf:   `${invested:.2f}`",
        "─────────────────────────",
        f"Totale portafoglio: `${total:.2f}`",
        f"Crescita:           `${growth:+.2f}` ({growth_p:+.1f}%)",
    ]

    if pos_lines:
        lines.append("\n*Posizioni:*")
        lines.extend(pos_lines)

    # Mostra log se richiesto
    args = context.args
    if args and args[0].lower() == "log":
        lines.append("\n*Ultimi movimenti:*")
        for entry in cap["log"][:10]:
            sign = "+" if entry["amount"] >= 0 else ""
            tick = f" [{entry['ticker']}]" if entry.get("ticker") else ""
            lines.append(
                f"  `{entry['created_at'][:10]}` {entry['event']}{tick}: "
                f"`{sign}${entry['amount']:.2f}` → `${entry['balance_after']:.2f}`"
            )

    lines.append(f"\n🕐 `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _fetch_live_ibkr_snapshot() -> tuple[dict[str, float], float] | None:
    """
    Prova a leggere posizioni reali da IBKR Client Portal Gateway locale.
    Ritorna (posizioni {ticker: valore_mercato}, cash) o None se il Gateway
    non è raggiungibile/autenticato — il chiamante deve avere un fallback.
    """
    from ibkr_connector import CPClient
    try:
        cp = CPClient()
        if not cp.auth_status():
            return None
        summary = cp.get_account_summary()
        positions = cp.get_all_positions()
        if not summary:
            return None
        stocks = {p.ticker: p.market_val for p in positions if p.sec_type == "STK"}
        return stocks, summary.total_cash
    except Exception as exc:
        logger.debug("_fetch_live_ibkr_snapshot: %s", exc)
        return None


async def _cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /portfolio — check istantaneo posizioni+P&L. Prova IBKR live (Client
    Portal Gateway locale), altrimenti fallback su DB+yfinance (come /capital).
    """
    live = _fetch_live_ibkr_snapshot()

    if live:
        stocks, cash = live
        total = sum(stocks.values()) + cash
        lines = ["💼 *PORTAFOGLIO — dati IBKR live*\n"]
        for ticker, val in sorted(stocks.items(), key=lambda x: -x[1]):
            pct = val / total * 100 if total else 0
            lines.append(f"  `{ticker}` `${val:,.2f}` ({pct:.1f}% del totale)")
        lines.append(f"\nCash: `${cash:,.2f}`")
        lines.append(f"*Totale: `${total:,.2f}`*")
    else:
        lines = ["⚠️ IBKR Gateway non raggiungibile — dati da DB (possono essere disallineati dal reale)\n"]
        cap = db.get_capital()
        positions = db.get_all_positions()
        import yfinance as yf
        for p in positions:
            try:
                price = yf.Ticker(p["ticker"]).fast_info.get("last_price") or float(p["entry_price"])
            except Exception:
                price = float(p["entry_price"])
            val = price * float(p["shares"])
            lines.append(f"  `{p['ticker']}` `${val:,.2f}`")
        lines.append(f"\nCash (DB): `${cap['balance']:,.2f}`")

    lines.append(f"\n🕐 `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _cmd_concentration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /concentration — % del bucket per ogni sottostante vs tetto
    config.WHEEL_MAX_CONCENTRATION_PCT. Richiede IBKR live (senza dati
    reali il check non ha senso — niente fallback DB qui).
    """
    live = _fetch_live_ibkr_snapshot()
    if not live:
        await update.message.reply_text(
            "⚠️ IBKR Gateway non raggiungibile — il check di concentrazione richiede dati reali, niente fallback.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    stocks, cash = live
    total = sum(stocks.values()) + cash
    lines = [f"📊 *CONCENTRAZIONE BUCKET* (tetto {config.WHEEL_MAX_CONCENTRATION_PCT:.0f}%)\n"]
    for ticker, val in sorted(stocks.items(), key=lambda x: -x[1]):
        pct = val / total * 100 if total else 0
        flag = "🔴" if pct > config.WHEEL_MAX_CONCENTRATION_PCT else "🟢"
        lines.append(f"  {flag} `{ticker}` {pct:.1f}%")
    lines.append(f"\nCash: {cash/total*100 if total else 0:.1f}%")
    lines.append(f"\n🕐 `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _cmd_dividend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /dividend <TICKER> <IMPORTO_PER_AZIONE>
    Es: /dividend F 0.15   — registra dividendo Ford da $0.15/az
    Aggiunge automaticamente (shares × importo) al capitale cash.
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Uso: `/dividend TICKER IMPORTO_PER_AZIONE`\n"
            "Es: `/dividend F 0.15`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        ticker = args[0].upper()
        amount_per_share = float(args[1])
    except ValueError:
        await update.message.reply_text("Importo non valido.", parse_mode=ParseMode.MARKDOWN)
        return

    pos = db.get_position(ticker)
    shares = float(pos["shares"]) if pos else 1.0
    total_div = amount_per_share * shares

    new_bal = db.log_capital(
        "dividend", total_div, ticker,
        f"Dividendo ${amount_per_share:.4f}/az × {shares:.0f}az",
    )

    await update.message.reply_text(
        f"✅ *Dividendo registrato — {ticker}*\n\n"
        f"`${amount_per_share:.4f}` × `{shares:.0f}az` = `${total_div:.2f}`\n"
        f"Capitale cash: `${new_bal:.2f}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = db.get_risk_state()
    paused = db.is_paused()
    monthly_pnl = state["monthly_pnl_pct"] * 100

    pause_info = ""
    if paused:
        pause_info = f"\n🔴 *PAUSED* until `{state['paused_until']}`"

    msg = (
        f"📊 *System Status*{pause_info}\n\n"
        f"Monthly PnL: `{monthly_pnl:+.2f}%`\n"
        f"Drawdown limit: `{config.MAX_MONTHLY_DRAWDOWN*100:.0f}%`\n"
        f"Stop loss: `{config.STOP_LOSS_PCT*100:.0f}%`\n"
        f"Alert threshold: `{config.MIN_ALERT_SCORE}/100`\n"
        f"Time: `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def _cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db._conn()
    rows = conn.execute(
        "SELECT ticker, source, score, created_at FROM signals "
        "WHERE score >= ? ORDER BY created_at DESC LIMIT 10",
        (config.MIN_ALERT_SCORE,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("No scored signals above threshold yet.")
        return

    lines = ["📡 *Recent signals:*\n"]
    for r in rows:
        lines.append(
            f"• `{r['ticker'] or 'N/A'}` | {r['source']} | score `{r['score']}` | {r['created_at'][:16]}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def _cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db._conn()
    positions = conn.execute("SELECT * FROM positions").fetchall()
    state = db.get_risk_state()

    lines = ["⚖️ *Risk Summary*\n"]
    lines.append(f"Monthly PnL: `{state['monthly_pnl_pct']*100:+.2f}%`")
    lines.append(f"Pause status: {'🔴 PAUSED' if db.is_paused() else '🟢 ACTIVE'}\n")

    if positions:
        lines.append("*Open positions:*")
        for p in positions:
            lines.append(
                f"  `{p['ticker']}` — entry `${p['entry_price']:.2f}` "
                f"on {p['entry_date']}"
            )
    else:
        lines.append("_No tracked positions._")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Bot runner ────────────────────────────────────────────────────────────────

def run_bot_in_thread() -> None:
    """Start the Telegram bot in a background daemon thread."""
    import asyncio

    async def _main():
        app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )
        # Autorizzazione: solo la chat configurata (TELEGRAM_CHAT_ID) puo'
        # usare qualsiasi comando — senza questo filtro chiunque scopra lo
        # username del bot puo' leggere saldo/posizioni o iniettare dati
        # falsi nel ledger (/open, /close con importi/date arbitrarie).
        # Trovato da security review, 29/08/2026.
        _only_owner = filters.Chat(chat_id=int(config.TELEGRAM_CHAT_ID))

        app.add_handler(CommandHandler("status",  _cmd_status, filters=_only_owner))
        app.add_handler(CommandHandler("signals", _cmd_signals, filters=_only_owner))
        app.add_handler(CommandHandler("risk",    _cmd_risk, filters=_only_owner))
        # Wheel advisory commands
        app.add_handler(CommandHandler("wheel",    _cmd_wheel, filters=_only_owner))
        app.add_handler(CommandHandler("open",     _cmd_open, filters=_only_owner))
        app.add_handler(CommandHandler("close",    _cmd_close, filters=_only_owner))
        app.add_handler(CommandHandler("suggest",  _cmd_suggest, filters=_only_owner))
        app.add_handler(CommandHandler("addpos",   _cmd_addpos, filters=_only_owner))
        app.add_handler(CommandHandler("income",   _cmd_income, filters=_only_owner))
        app.add_handler(CommandHandler("capital",  _cmd_capital, filters=_only_owner))
        app.add_handler(CommandHandler("portfolio",     _cmd_portfolio, filters=_only_owner))
        app.add_handler(CommandHandler("concentration", _cmd_concentration, filters=_only_owner))
        app.add_handler(CommandHandler("dividend", _cmd_dividend, filters=_only_owner))

        logger.info("Telegram bot polling started")
        async with app:
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            # tieni vivo il thread finché il processo gira
            await asyncio.Event().wait()

    def _run():
        try:
            asyncio.run(_main())
        except Exception as exc:
            logger.error("Telegram bot crashed: %s", exc, exc_info=True)

    t = threading.Thread(target=_run, name="telegram-bot", daemon=True)
    t.start()
    return t
