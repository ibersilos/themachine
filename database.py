"""SQLite persistence layer – schema creation + helpers."""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import config

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


@contextmanager
def tx():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    with tx() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,          -- edgar | form4 | usaspending | serenity
            ticker      TEXT,
            score       INTEGER,
            pipeline    TEXT DEFAULT 'unknown', -- stock_picking | wheel_candidate
            payload     TEXT,                   -- JSON blob
            alerted     INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS risk_state (
            id               INTEGER PRIMARY KEY CHECK (id = 1),
            paused_until     TEXT,              -- ISO datetime or NULL
            monthly_pnl_pct  REAL DEFAULT 0.0,
            month_start      TEXT DEFAULT (strftime('%Y-%m-01', 'now'))
        );

        INSERT OR IGNORE INTO risk_state (id) VALUES (1);

        CREATE TABLE IF NOT EXISTS positions (
            ticker       TEXT PRIMARY KEY,
            entry_price  REAL,
            entry_date   TEXT,
            shares       REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS risk_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT NOT NULL,   -- stop_loss | drawdown_pause | kill_switch
            ticker      TEXT,
            detail      TEXT,            -- JSON
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source);
        CREATE INDEX IF NOT EXISTS idx_signals_pipeline ON signals(pipeline);

        -- Wheel cycle tracking per framework Hogue
        CREATE TABLE IF NOT EXISTS wheel_cycles (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            year             INTEGER DEFAULT (strftime('%Y','now')),
            cycle_number     INTEGER DEFAULT 1,   -- ciclo progressivo nell'anno per questo ticker
            phase            TEXT DEFAULT 'covered_call', -- csp|covered_call|assigned|closed
            strike           REAL,
            expiry           TEXT,                -- YYYY-MM-DD
            premium_received REAL DEFAULT 0.0,   -- premio incassato all'apertura
            premium_current  REAL DEFAULT 0.0,   -- valore attuale dell'opzione (costo chiusura)
            roll_count       INTEGER DEFAULT 0,
            opened_at        TEXT DEFAULT (datetime('now')),
            closed_at        TEXT,
            pnl_realized     REAL,               -- PnL realizzato alla chiusura
            notes            TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_wheel_ticker ON wheel_cycles(ticker, year);
        """)
        # Migrazione: aggiungi colonna pipeline se non esiste (DB pre-dual-pipeline)
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN pipeline TEXT DEFAULT 'unknown'")
            conn.commit()
        except Exception:
            pass  # colonna già presente


# ── Risk helpers ──────────────────────────────────────────────────────────────

def get_risk_state() -> sqlite3.Row:
    return _conn().execute("SELECT * FROM risk_state WHERE id=1").fetchone()


def set_pause(until: datetime) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE risk_state SET paused_until=? WHERE id=1",
            (until.isoformat(),),
        )


def update_monthly_pnl(delta_pct: float) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE risk_state SET monthly_pnl_pct = monthly_pnl_pct + ? WHERE id=1",
            (delta_pct,),
        )


def is_paused() -> bool:
    row = get_risk_state()
    if not row["paused_until"]:
        return False
    return datetime.fromisoformat(row["paused_until"]) > datetime.utcnow()


# ── Signal helpers ────────────────────────────────────────────────────────────

def save_signal(source: str, ticker: str | None, score: int, payload: str,
                pipeline: str = "unknown") -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO signals (source, ticker, score, pipeline, payload) VALUES (?,?,?,?,?)",
            (source, ticker, score, pipeline, payload),
        )
        return cur.lastrowid


def mark_alerted(signal_id: int) -> None:
    with tx() as conn:
        conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (signal_id,))


# ── Wheel cycle helpers ───────────────────────────────────────────────────────

def open_wheel_cycle(
    ticker: str, strike: float, expiry: str,
    premium_received: float, phase: str = "covered_call",
) -> int:
    """Apre un nuovo ciclo wheel. Restituisce l'ID del ciclo."""
    with tx() as conn:
        year = datetime.utcnow().year
        row = conn.execute(
            "SELECT COALESCE(MAX(cycle_number),0) FROM wheel_cycles WHERE ticker=? AND year=?",
            (ticker, year),
        ).fetchone()
        next_cycle = row[0] + 1
        cur = conn.execute(
            """INSERT INTO wheel_cycles
               (ticker, year, cycle_number, phase, strike, expiry, premium_received, premium_current)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker, year, next_cycle, phase, strike, expiry, premium_received, premium_received),
        )
        return cur.lastrowid


def update_wheel_premium(cycle_id: int, premium_current: float) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE wheel_cycles SET premium_current=? WHERE id=?",
            (premium_current, cycle_id),
        )


def close_wheel_cycle(cycle_id: int, pnl: float, notes: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE wheel_cycles SET phase='closed', closed_at=datetime('now'), "
            "pnl_realized=?, notes=? WHERE id=?",
            (pnl, notes, cycle_id),
        )


def increment_roll_count(cycle_id: int) -> int:
    """Incrementa roll_count e restituisce il nuovo valore."""
    with tx() as conn:
        conn.execute(
            "UPDATE wheel_cycles SET roll_count = roll_count + 1 WHERE id=?",
            (cycle_id,),
        )
        row = conn.execute(
            "SELECT roll_count FROM wheel_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        return row["roll_count"] if row else 0


def get_wheel_cycles_year(ticker: str, year: int | None = None) -> list:
    if year is None:
        year = datetime.utcnow().year
    return _conn().execute(
        "SELECT * FROM wheel_cycles WHERE ticker=? AND year=? ORDER BY cycle_number",
        (ticker, year),
    ).fetchall()


def count_closed_cycles_year(ticker: str, year: int | None = None) -> int:
    if year is None:
        year = datetime.utcnow().year
    row = _conn().execute(
        "SELECT COUNT(*) FROM wheel_cycles WHERE ticker=? AND year=? AND phase='closed'",
        (ticker, year),
    ).fetchone()
    return row[0] if row else 0


def avg_pnl_per_cycle(ticker: str, year: int | None = None) -> float:
    if year is None:
        year = datetime.utcnow().year
    row = _conn().execute(
        "SELECT AVG(pnl_realized) FROM wheel_cycles WHERE ticker=? AND year=? AND phase='closed'",
        (ticker, year),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0
