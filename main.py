"""
the-machine – entry point.

Pipeline:
  edgar_monitor  ──┐
  usaspending    ──┼──► signal_queue ──► enrich ──► score ──► dispatch (Telegram)
  (form4 via edgar)┘                                           └──► drive_export (daily)
"""
import logging
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import config
import database as db
import edgar_monitor
import usaspending
import form4_monitor
import serenity_validator
import fundamentals
import scoring_engine
import telegram_bot
import drive_export
import api_server
import ibkr_connector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("the-machine")

_signal_queue: queue.Queue = queue.Queue()
_daily_signals: list[dict] = []          # accumulate for Drive export
_last_export_date: str = ""


def _enrich(sig: dict) -> dict:
    """Attach Serenity + Fundamentals data to any signal that has a ticker."""
    source = sig.get("source", "")

    # Form-4 deep parse
    if source == "form4":
        sig = form4_monitor.enrich_form4_signal(sig)
        # Skip non-purchase Form-4s before scoring
        tx = sig.get("transaction_type")
        if tx and tx != "P":
            return {}   # empty dict signals "skip this"

    # Serenity + Fundamentals always
    sig = serenity_validator.enrich_signal(sig)
    sig = fundamentals.enrich_signal(sig)

    return sig


def _maybe_export_to_drive() -> None:
    """Once per UTC day, push accumulated signals to Google Drive."""
    global _last_export_date, _daily_signals
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _last_export_date and _daily_signals:
        url = drive_export.export_daily_signals(_daily_signals)
        if url:
            telegram_bot.send_alert(
                f"📊 *Daily export ready*\n[View on Drive]({url})"
            )
        _daily_signals = []
        _last_export_date = today


def _stop_loss_watcher() -> None:
    """Background thread: checks open positions against live prices every 5 min."""
    while True:
        try:
            conn = db._conn()
            positions = conn.execute("SELECT ticker, entry_price FROM positions").fetchall()
            for pos in positions:
                ticker = pos["ticker"]
                price  = fundamentals.current_price(ticker)
                if price:
                    telegram_bot.check_stop_loss(ticker, pos["entry_price"], price)
        except Exception as exc:
            logger.error("Stop-loss watcher error: %s", exc)
        time.sleep(300)


def _scoring_worker() -> None:
    """Consume raw signals from queue, enrich → score → dispatch."""
    while True:
        try:
            sig = _signal_queue.get(timeout=5)
        except queue.Empty:
            _maybe_export_to_drive()
            continue

        try:
            enriched = _enrich(sig)
            if not enriched:
                continue

            bd = scoring_engine.score_signal(enriched)
            if bd.filtered:
                continue  # market cap fuori range — nessun alert
            enriched["score"] = bd.total
            enriched["tier"]  = bd.tier()
            enriched["flags"] = bd.flags

            telegram_bot.dispatch_signal(bd, enriched)
            api_server.notify_signal()

            if bd.total >= 40:  # accumulate anything notable for daily export
                _daily_signals.append(enriched)

        except Exception as exc:
            logger.error("Pipeline error: %s", exc)
        finally:
            _signal_queue.task_done()


def _on_signal(sig: dict) -> None:
    _signal_queue.put(sig)


def _shutdown(signum, frame):
    logger.info("Shutdown – flushing exports")
    if _daily_signals:
        drive_export.export_daily_signals(_daily_signals)
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    db.init_db()
    logger.info("DB ready at %s", db.config.DB_PATH)

    api_server.start_api_thread(
        host=config.API_HOST,
        port=config.API_PORT,
    )

    telegram_bot.run_bot_in_thread()
    telegram_bot.send_alert(
        "🟢 *the-machine online*\n"
        "Sources: SEC 8-K · Form 4 · USAspending · Serenity · yfinance"
    )

    threading.Thread(target=_scoring_worker, name="scorer", daemon=True).start()
    threading.Thread(target=_stop_loss_watcher, name="stop-loss", daemon=True).start()

    # IBKR connector — opzionale, non blocca se TWS/Gateway è offline
    def _on_ibkr_sync(result: ibkr_connector.SyncResult) -> None:
        api_server.set_ibkr_status(connected=True, positions=result.synced)
        if result.errors:
            logger.warning("IBKR sync errors: %s", result.errors)

    ibkr_connector.start_ibkr_thread(_on_ibkr_sync)

    # USAspending in its own thread (slower poll)
    threading.Thread(
        target=usaspending.run_forever,
        args=(_on_signal,),
        name="usaspending",
        daemon=True,
    ).start()

    # EDGAR (8-K + Form-4) – blocks main thread
    edgar_monitor.run_forever(on_signal=_on_signal)


if __name__ == "__main__":
    main()
