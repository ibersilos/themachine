"""
the-machine – entry point.
Starts EDGAR monitor + Telegram bot in parallel threads.
"""
import logging
import queue
import signal
import sys
import threading

import database as db
import edgar_monitor
import scoring_engine
import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("the-machine")

_signal_queue: queue.Queue = queue.Queue()


def _on_edgar_signal(sig: dict) -> None:
    _signal_queue.put(sig)


def _scoring_worker() -> None:
    """Consume raw signals, score them, dispatch alerts."""
    while True:
        try:
            sig = _signal_queue.get(timeout=5)
        except queue.Empty:
            continue
        try:
            bd = scoring_engine.score_signal(sig)
            telegram_bot.dispatch_signal(bd, sig)
        except Exception as exc:
            logger.error("Scoring/dispatch error: %s", exc)
        finally:
            _signal_queue.task_done()


def _shutdown(signum, frame):
    logger.info("Shutdown signal received – exiting.")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    db.init_db()
    logger.info("Database initialised at %s", db.config.DB_PATH)

    telegram_bot.run_bot_in_thread()
    telegram_bot.send_alert("🟢 *the-machine started* – monitoring SEC EDGAR feeds.")

    scorer_thread = threading.Thread(
        target=_scoring_worker, name="scorer", daemon=True
    )
    scorer_thread.start()

    # Blocking: runs EDGAR polling forever
    edgar_monitor.run_forever(on_signal=_on_edgar_signal)


if __name__ == "__main__":
    main()
