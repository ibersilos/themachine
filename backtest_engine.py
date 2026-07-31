"""
backtest_engine.py — misura il ritorno realizzato dei segnali alertati.

Per ogni segnale con ticker e alerted=1, cattura il prezzo di chiusura al
giorno del segnale (T0) e ai giorni di trading T0+5/10/20/60, per verificare
se lo score prodotto dalla pipeline correla con un ritorno reale o e' rumore.

Uso:
  python backtest_engine.py                 # esegue una passata + stampa report
  python backtest_engine.py --report-only    # solo report sui dati gia' presenti
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import yfinance as yf

import database as db

logger = logging.getLogger(__name__)

HORIZONS = (5, 10, 20, 60)   # giorni di trading da T0
MIN_AGE_DAYS = 6             # aspetta almeno T0+5gg di calendario prima di provare
MAX_AGE_DAYS = 120           # oltre non ha senso ritentare


def _fetch_history(ticker: str, t0: datetime):
    """Scarica un'unica finestra di storico che copre da poco prima di T0 a T0+~95gg."""
    start = (t0 - timedelta(days=3)).strftime("%Y-%m-%d")
    end = min(datetime.now(timezone.utc), t0 + timedelta(days=95)).strftime("%Y-%m-%d")
    try:
        hist = yf.Ticker(ticker).history(start=start, end=end)
    except Exception as exc:
        logger.warning("yfinance history fallita per %s: %s", ticker, exc)
        return None
    if hist.empty:
        return None
    return hist


def _closes_from(hist, t0: datetime):
    """Ritorna (t0_price, {horizon: price}) usando offset per indice di trading day."""
    closes = hist["Close"]
    anchor_pos = None
    for i, ts in enumerate(closes.index):
        ts_utc = ts.to_pydatetime()
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        if ts_utc >= t0:
            anchor_pos = i
            break
    if anchor_pos is None:
        return None, {}

    t0_price = float(closes.iloc[anchor_pos])
    out = {}
    for h in HORIZONS:
        pos = anchor_pos + h
        if pos < len(closes):
            out[h] = float(closes.iloc[pos])
    return t0_price, out


def run_pass(limit: int = 200) -> int:
    """Elabora fino a `limit` segnali in attesa di backtest. Ritorna quanti aggiornati."""
    signals = db.get_signals_for_backtest(MIN_AGE_DAYS, MAX_AGE_DAYS, limit)
    updated = 0
    hist_cache: dict[str, object] = {}

    for sig in signals:
        ticker = sig["ticker"]
        try:
            t0 = datetime.strptime(sig["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        cache_key = f"{ticker}:{t0.date()}"
        if cache_key not in hist_cache:
            hist_cache[cache_key] = _fetch_history(ticker, t0)
            time.sleep(0.3)  # gentle su yfinance
        hist = hist_cache[cache_key]
        if hist is None:
            continue

        t0_price, horizon_prices = _closes_from(hist, t0)
        if t0_price is None:
            continue

        prices = {"t0": t0_price, **horizon_prices}
        returns = {h: (p - t0_price) / t0_price for h, p in horizon_prices.items()}

        db.upsert_backtest_result(
            signal_id=sig["id"], ticker=ticker, score=sig["score"],
            source=sig["source"], pipeline=sig["pipeline"],
            t0_date=t0.strftime("%Y-%m-%d"), prices=prices, returns=returns,
        )
        updated += 1

    logger.info("Backtest pass: %d/%d segnali aggiornati", updated, len(signals))
    return updated


def _pct(v) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "  n/a"


def format_report() -> str:
    rows = db.get_backtest_report()
    if not rows:
        return "Nessun dato di backtest ancora disponibile."

    lines = [f"{'source':11} {'bucket':7} {'n':>4} {'avg5d':>8} {'avg10d':>8} {'avg20d':>8} {'avg60d':>8} {'win20d':>8}"]
    for r in rows:
        lines.append(
            f"{r['source']:11} {r['bucket']:7} {r['n']:>4} "
            f"{_pct(r['avg_5d']):>8} {_pct(r['avg_10d']):>8} {_pct(r['avg_20d']):>8} {_pct(r['avg_60d']):>8} "
            f"{_pct(r['win_rate_20d']):>8}"
        )
    return "\n".join(lines)


def print_report() -> None:
    print(format_report())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--report-only", action="store_true", help="Salta il fetch prezzi, mostra solo il report esistente")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db.init_db()

    if not args.report_only:
        run_pass()
    print_report()
