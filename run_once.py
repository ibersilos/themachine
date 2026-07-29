"""
run_once.py — esecuzione one-shot per GitHub Actions.

  python run_once.py --mode wheel    # check posizioni wheel + advisory Hogue
  python run_once.py --mode signals  # scan EDGAR/Form4/USAspending + alert
"""
import argparse
import logging
import queue
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run-once")


def run_wheel() -> None:
    import database as db
    import wheel_daemon
    import telegram_bot

    db.init_db()

    # Registra Ford se non presente
    if not db.get_position("F"):
        db.upsert_position("F", cost_basis=13.46, shares=100, entry_date="2026-07-09")
        db.log_capital("buy_shares", -1346.0, "F", "Acquisto 100az Ford @ $13.46")

    db.seed_capital(1500.0)

    logger.info("Eseguo wheel daily check...")
    wheel_daemon._daily_check()
    logger.info("Wheel check completato.")


def run_signals() -> None:
    import config
    import database as db
    import edgar_monitor
    import form4_monitor
    import usaspending
    import serenity_validator
    import fundamentals
    import scoring_engine
    import telegram_bot
    from datetime import datetime, timezone

    db.init_db()
    db.seed_capital(1500.0)

    signal_queue: queue.Queue = queue.Queue()
    alerts_sent = 0

    def on_signal(sig: dict) -> None:
        signal_queue.put(sig)

    def _enrich(sig: dict) -> dict:
        source = sig.get("source", "")
        if source == "form4":
            sig = form4_monitor.enrich_form4_signal(sig)
            if sig.get("transaction_type") and sig["transaction_type"] != "P":
                return {}
        sig = serenity_validator.enrich_signal(sig)
        sig = fundamentals.enrich_signal(sig)
        return sig

    logger.info("Scansione EDGAR 8-K + Form4 (ultimi filing)...")
    edgar_monitor.run_once(on_signal=on_signal)

    logger.info("Scansione USAspending...")
    usaspending.run_once(on_signal=on_signal)

    logger.info("Processing %d segnali...", signal_queue.qsize())
    while not signal_queue.empty():
        sig = signal_queue.get()
        try:
            enriched = _enrich(sig)
            if not enriched:
                continue
            bd = scoring_engine.score_signal(enriched)
            if bd.filtered:
                continue
            if bd.pipeline == "wheel_candidate" and not config.WHEEL_ALERTS_ENABLED:
                continue
            enriched["score"] = bd.total
            enriched["tier"]  = bd.tier()
            telegram_bot.dispatch_signal(bd, enriched)
            alerts_sent += 1
        except Exception as exc:
            logger.error("Errore processing segnale: %s", exc)

    if alerts_sent == 0:
        logger.info("Nessun segnale sopra soglia questa settimana.")
    else:
        logger.info("%d alert inviati su Telegram.", alerts_sent)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["wheel", "signals"], required=True)
    args = parser.parse_args()

    if args.mode == "wheel":
        run_wheel()
    elif args.mode == "signals":
        run_signals()
