"""
run_once.py — esecuzione one-shot per GitHub Actions.

  python run_once.py --mode wheel     # check posizioni wheel + advisory Hogue
  python run_once.py --mode signals   # scan EDGAR/Form4/USAspending + alert
  python run_once.py --mode backtest  # ritorno realizzato T0..T0+N per segnali alertati
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

    # Pulizia una tantum: il DB persistito via cache GitHub Actions e' separato
    # da quello locale, quindi non ha mai visto il /close reale del 10/08 —
    # continua a mandare advisory per un ciclo gia' chiuso su IBKR. Chiude
    # qui il record fantasma se ancora presente (self-heal, non ripetibile
    # per errore: agisce solo sull'esatta firma del ciclo noto).
    _ghost = db.get_open_cycle("F")
    if _ghost and float(_ghost["strike"]) == 15.5 and _ghost["expiry"] == "2026-08-21":
        db.close_wheel_cycle(
            _ghost["id"], pnl=17.54,
            notes="Chiuso realmente il 10/08/2026 (buy-to-close @ $0.03) — "
                  "self-heal cache GitHub Actions mai sincronizzata col DB locale",
        )
        logger.info("Ciclo fantasma F (strike 15.5) chiuso per allineare la cache GH Actions")

    # Prossimo ciclo da aprire manualmente via /open dopo l'ex-dividend dell'11/08
    # finche' non verra' registrato allo stesso modo.

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
    import seeking_alpha_feed
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

        if source == "edgar_8k":
            # Pre-check economico (1 campo, no history/option_chain) prima
            # dell'arricchimento pieno: la maggior parte dei segnali 8-K
            # viene comunque scartata dal filtro cap WHEEL_CAP_MIN — non ha
            # senso pagare 3-4 round-trip di rete per poi buttarli via.
            cap = fundamentals.quick_market_cap(sig.get("ticker"))
            if cap is not None and cap < config.WHEEL_CAP_MIN:
                return {}

        sig = fundamentals.enrich_signal(sig)
        sig = seeking_alpha_feed.enrich_signal(sig)
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


def run_backtest() -> None:
    import database as db
    import backtest_engine
    import telegram_bot

    db.init_db()

    logger.info("Eseguo backtest pass...")
    n = backtest_engine.run_pass()
    logger.info("%d segnali aggiornati.", n)

    report = backtest_engine.format_report()
    logger.info("Report backtest:\n%s", report)
    telegram_bot.send_alert(f"📈 *Backtest settimanale* ({n} segnali aggiornati)\n```\n{report}\n```")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["wheel", "signals", "backtest"], required=True)
    args = parser.parse_args()

    if args.mode == "wheel":
        run_wheel()
    elif args.mode == "signals":
        run_signals()
    elif args.mode == "backtest":
        run_backtest()
