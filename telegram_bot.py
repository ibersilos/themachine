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
from telegram.ext import Application, CommandHandler, ContextTypes
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
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\n🕐 `{ts}`")
    if url:
        lines.append(f"[View filing]({url})")
    return "\n".join(lines)


def _format_wheel_alert(bd: ScoreBreakdown, signal: dict) -> str:
    """Layout options desk per pipeline WHEEL_CANDIDATES."""
    ticker  = bd.ticker or "N/A"
    url     = signal.get("filing_url") or signal.get("award_url") or ""
    source_labels = {"edgar_8k": "SEC 8-K", "serenity": "Serenity"}
    source  = source_labels.get(signal.get("source", ""), signal.get("source", ""))

    iv_rank = signal.get("iv_rank")
    oi      = signal.get("open_interest")
    cap     = signal.get("market_cap")
    price   = signal.get("current_price")
    sector  = signal.get("sector") or ""

    iv_str  = f"{iv_rank:.0f}%  ← vendi premium" if iv_rank else "N/A  (IBKR offline)"
    oi_str  = f"{oi:,.0f}" if oi else "N/A"

    lines = [
        f"⚙️ *[WHEEL] {bd.tier()} — {ticker}*",
        f"Score: `{bd.total}/100`  |  {source}",
        "─────────────────────────",
        f"🎰 IV Rank:  `{iv_str}`",
        f"📋 OI tot:   `{oi_str}`",
        f"💵 Prezzo:   `{'$%.2f' % price if price else 'N/A'}`",
        f"🏦 Cap:      `{_fmt_cap(cap)}`",
    ]
    if sector:
        lines.append(f"🏭 Settore:  `{sector}`")
    lines.append("─────────────────────────")
    if bd.flags:
        for f in bd.flags:
            lines.append(f"  • {f}")
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
    if url:
        lines.append(f"\n[View filing]({url})")
    lines.append(f"\n🕐 `{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}`")
    return "\n".join(lines)


# ── Public send functions ─────────────────────────────────────────────────────

def send_alert(text: str) -> None:
    """Fire-and-forget alert — httpx sync, funziona da qualsiasi thread/contesto."""
    try:
        with httpx.Client(timeout=10) as client:
            client.post(f"{_TG_API}/sendMessage", json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
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

    msg = _format_signal_alert(bd, signal)
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
        app.add_handler(CommandHandler("status",  _cmd_status))
        app.add_handler(CommandHandler("signals", _cmd_signals))
        app.add_handler(CommandHandler("risk",    _cmd_risk))

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
