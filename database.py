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

        CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
        CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source);
        """)


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

def save_signal(source: str, ticker: str | None, score: int, payload: str) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO signals (source, ticker, score, payload) VALUES (?,?,?,?)",
            (source, ticker, score, payload),
        )
        return cur.lastrowid


def mark_alerted(signal_id: int) -> None:
    with tx() as conn:
        conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (signal_id,))
