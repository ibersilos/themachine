"""
api_server.py — REST API per il dashboard live di The Machine.

Endpoint:
  GET /api/status          → stato sistema (pausa, drawdown, poll age)
  GET /api/signals         → ultimi N segnali dal DB
  GET /api/positions       → posizioni aperte con stato Hogue
  GET /api/hogue/<ticker>  → run_hogue_check() on demand
  GET /api/risk            → risk state corrente
  GET /api/wheel           → wheel cycles anno corrente

Avvio standalone:
  python api_server.py

Avviato da main.py come daemon thread:
  from api_server import start_api_thread
  start_api_thread()

Dipendenza: pip install fastapi uvicorn
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import database as db

logger = logging.getLogger(__name__)

app = FastAPI(title="The Machine API", version="1.0.0", docs_url="/api/docs")

# CORS — consente al dashboard HTML di fare fetch() da qualsiasi origine locale
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Timestamp dell'ultimo segnale ricevuto (aggiornato da main.py via notify_signal)
_last_signal_ts: float = time.time()
_signal_count_today: int = 0

# Stato IBKR (aggiornato da ibkr_connector via set_ibkr_status)
_ibkr_connected: bool = False
_ibkr_positions: list = []
_ibkr_last_sync: float = 0.0


def notify_signal() -> None:
    """Chiamata da main.py ogni volta che arriva un segnale."""
    global _last_signal_ts, _signal_count_today
    _last_signal_ts = time.time()
    _signal_count_today += 1


def set_ibkr_status(connected: bool, positions: list | None = None) -> None:
    """Aggiornata da ibkr_connector a ogni sync."""
    global _ibkr_connected, _ibkr_positions, _ibkr_last_sync
    _ibkr_connected = connected
    if positions is not None:
        _ibkr_positions = positions
    _ibkr_last_sync = time.time()


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """sqlite3.Row → plain dict."""
    return dict(row) if row else {}


def _rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _poll_age_str(ts: float) -> str:
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


# ── /api/status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status() -> dict:
    """Stato globale del sistema: pausa, drawdown, age ultimo poll."""
    try:
        risk = _row_to_dict(db.get_risk_state())
        paused = bool(risk.get("paused_until") and
                      datetime.fromisoformat(risk["paused_until"]) > datetime.now(timezone.utc))
        return {
            "ok": True,
            "active": not paused,
            "paused": paused,
            "paused_until": risk.get("paused_until"),
            "monthly_pnl_pct": round(float(risk.get("monthly_pnl_pct") or 0), 4),
            "last_signal_ago": _poll_age_str(_last_signal_ts),
            "last_signal_ts": _last_signal_ts,
            "signals_today": _signal_count_today,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "max_monthly_drawdown": config.MAX_MONTHLY_DRAWDOWN,
            "ibkr_connected": _ibkr_connected,
            "ibkr_last_sync": _ibkr_last_sync or None,
        }
    except Exception as exc:
        logger.error("/api/status error: %s", exc)
        raise HTTPException(500, str(exc))


# ── /api/risk ─────────────────────────────────────────────────────────────────

@app.get("/api/risk")
def get_risk() -> dict:
    """Risk state dettagliato."""
    try:
        risk = _row_to_dict(db.get_risk_state())
        dd = float(risk.get("monthly_pnl_pct") or 0)
        return {
            "drawdown_pct":       round(dd, 4),
            "drawdown_display":   f"{dd*100:+.1f}%",
            "stop_loss_pct":      config.STOP_LOSS_PCT,
            "max_drawdown_pct":   config.MAX_MONTHLY_DRAWDOWN,
            "pause_days":         config.DRAWDOWN_PAUSE_DAYS,
            "paused_until":       risk.get("paused_until"),
            "is_paused":          db.is_paused(),
            "kill_switch_armed":  True,
        }
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── /api/signals ──────────────────────────────────────────────────────────────

@app.get("/api/signals")
def get_signals(
    limit: int = Query(default=50, ge=1, le=500),
    source: str | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
) -> dict:
    """
    Ultimi segnali dal DB.
    Parametri:
      limit     — max righe (default 50)
      source    — filtra per edgar_8k | form4 | usaspending | serenity
      min_score — filtra per score minimo
    """
    try:
        conn = db._conn()
        query = "SELECT * FROM signals WHERE score >= ?"
        params: list[Any] = [min_score]

        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        signals = []
        for r in rows:
            d = dict(r)
            # parse payload JSON se presente
            if d.get("payload"):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    pass
            # Aggiungi tier calcolato
            score = d.get("score") or 0
            d["tier"] = "strong" if score >= config.STRONG_BUY_THRESHOLD else \
                        "alert"  if score >= config.MIN_ALERT_SCORE else "watch"
            signals.append(d)

        return {"ok": True, "count": len(signals), "signals": signals}
    except Exception as exc:
        logger.error("/api/signals error: %s", exc)
        raise HTTPException(500, str(exc))


# ── /api/positions ────────────────────────────────────────────────────────────

@app.get("/api/positions")
def get_positions() -> dict:
    """
    Posizioni aperte: combina tabella positions + wheel_cycles aperti.
    Include percentuale catturata e stato Hogue (close/roll/hold).
    """
    try:
        conn = db._conn()

        # Posizioni stock
        stock_rows = conn.execute(
            "SELECT ticker, entry_price, entry_date, shares FROM positions"
        ).fetchall()

        result = []
        for sr in stock_rows:
            ticker = sr["ticker"]
            entry  = float(sr["entry_price"] or 0)

            # Ciclo wheel più recente non chiuso
            cycle = conn.execute(
                """SELECT * FROM wheel_cycles
                   WHERE ticker=? AND phase != 'closed'
                   ORDER BY opened_at DESC LIMIT 1""",
                (ticker,)
            ).fetchone()

            pos: dict[str, Any] = {
                "ticker":       ticker,
                "entry_price":  entry,
                "entry_date":   sr["entry_date"],
                "shares":       float(sr["shares"] or 0),
                "cycle":        None,
            }

            if cycle:
                pr  = float(cycle["premium_received"] or 0)
                pc  = float(cycle["premium_current"]  or 0)
                cap = round((pr - pc) / pr, 4) if pr > 0 else 0.0

                # Hogue tag semplificato (senza chiamare yfinance qui)
                if cap >= config.HOGUE_EARLY_CLOSE_PCT:
                    hogue_tag = "close"
                elif int(cycle["roll_count"] or 0) >= config.HOGUE_MAX_ROLLS:
                    hogue_tag = "assigned"
                else:
                    hogue_tag = "hold"

                pos["cycle"] = {
                    "id":               cycle["id"],
                    "phase":            cycle["phase"],
                    "strike":           float(cycle["strike"] or 0),
                    "expiry":           cycle["expiry"],
                    "premium_received": pr,
                    "premium_current":  pc,
                    "pct_captured":     cap,
                    "roll_count":       int(cycle["roll_count"] or 0),
                    "hogue_tag":        hogue_tag,
                    "opened_at":        cycle["opened_at"],
                }

            result.append(pos)

        return {"ok": True, "count": len(result), "positions": result}
    except Exception as exc:
        logger.error("/api/positions error: %s", exc)
        raise HTTPException(500, str(exc))


# ── /api/hogue/<ticker> ───────────────────────────────────────────────────────

@app.get("/api/hogue/{ticker}")
def run_hogue(ticker: str, cycle_id: int | None = Query(default=None)) -> dict:
    """
    Esegue run_hogue_check() per il ticker specificato.
    Recupera il premio live da yfinance e valuta le regole Hogue.
    Può richiedere ~2s per la chiamata yfinance.
    """
    try:
        # Import locale per non bloccare l'avvio se yfinance è lento
        from covered_call_optimizer import run_hogue_check
        result = run_hogue_check(ticker.upper(), cycle_id=cycle_id)
        return {"ok": True, "ticker": ticker.upper(), "result": result}
    except Exception as exc:
        logger.error("/api/hogue/%s error: %s", ticker, exc)
        raise HTTPException(500, str(exc))


# ── /api/wheel ────────────────────────────────────────────────────────────────

@app.get("/api/wheel")
def get_wheel(year: int | None = Query(default=None)) -> dict:
    """
    Tutti i cicli wheel dell'anno (default: anno corrente).
    Includie statistiche aggregate per ticker.
    """
    try:
        target_year = year or datetime.now(timezone.utc).year
        conn = db._conn()

        rows = conn.execute(
            "SELECT * FROM wheel_cycles WHERE year=? ORDER BY ticker, cycle_number",
            (target_year,)
        ).fetchall()

        cycles = _rows_to_list(rows)

        # Statistiche per ticker
        tickers: dict[str, dict] = {}
        for c in cycles:
            t = c["ticker"]
            if t not in tickers:
                tickers[t] = {"ticker": t, "total": 0, "closed": 0,
                               "total_pnl": 0.0, "open_cycles": []}
            tickers[t]["total"] += 1
            if c["phase"] == "closed":
                tickers[t]["closed"] += 1
                tickers[t]["total_pnl"] += float(c["pnl_realized"] or 0)
            else:
                tickers[t]["open_cycles"].append(c)

        for t, s in tickers.items():
            closed = s["closed"]
            s["avg_pnl"] = round(s["total_pnl"] / closed, 4) if closed > 0 else 0.0

        total_closed  = db.count_closed_cycles_year(ticker="", year=None) \
            if False else sum(s["closed"] for s in tickers.values())
        ann_return = round(
            sum(s["total_pnl"] for s in tickers.values()) /
            max(sum(s["total"] for s in tickers.values()), 1) *
            config.HOGUE_TARGET_CYCLES_YEAR, 4
        ) if tickers else 0.0

        return {
            "ok":            True,
            "year":          target_year,
            "cycles":        cycles,
            "by_ticker":     list(tickers.values()),
            "total_closed":  total_closed,
            "target_cycles": config.HOGUE_TARGET_CYCLES_YEAR,
            "ann_return_est": ann_return,
        }
    except Exception as exc:
        logger.error("/api/wheel error: %s", exc)
        raise HTTPException(500, str(exc))


# ── /api/health ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "ts": time.time()}


# ── /api/ibkr ─────────────────────────────────────────────────────────────────

@app.get("/api/ibkr")
def get_ibkr() -> dict:
    """Stato connessione IBKR e posizioni sincronizzate."""
    age = None
    if _ibkr_last_sync:
        secs = int(time.time() - _ibkr_last_sync)
        age = f"{secs}s fa" if secs < 60 else f"{secs//60}m fa"
    return {
        "ok": True,
        "connected": _ibkr_connected,
        "last_sync_ago": age,
        "position_count": len(_ibkr_positions),
        "dry_run": config.IBKR_DRY_RUN,
        "host": config.IBKR_HOST,
        "port": config.IBKR_PORT,
    }


# ── SERVER START ──────────────────────────────────────────────────────────────

def start_api_thread(
    host: str = "127.0.0.1",
    port: int = 8080,
) -> threading.Thread:
    """
    Avvia uvicorn in un daemon thread.
    Chiamata da main.py prima del loop EDGAR.
    """
    def _run():
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",   # silenzia i log HTTP in produzione
            access_log=False,
        )

    t = threading.Thread(target=_run, name="api-server", daemon=True)
    t.start()
    logger.info("API server avviato su http://%s:%d/api/docs", host, port)
    return t


if __name__ == "__main__":
    # Avvio standalone per sviluppo/test
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(app, host="127.0.0.1", port=8080, reload=False)
