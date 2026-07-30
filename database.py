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
            source      TEXT NOT NULL,          -- edgar_8k | form4 | usaspending
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

        -- Capital tracking
        CREATE TABLE IF NOT EXISTS capital (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            balance   REAL NOT NULL DEFAULT 0.0,   -- cash disponibile
            seed      REAL NOT NULL DEFAULT 0.0,   -- capitale iniziale (mai cambia)
            updated_at TEXT DEFAULT (datetime('now'))
        );

        INSERT OR IGNORE INTO capital (id, balance, seed) VALUES (1, 0.0, 0.0);

        CREATE TABLE IF NOT EXISTS capital_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT NOT NULL,   -- seed | premium_in | premium_out | buy_shares | sell_shares | dividend | adjustment
            amount      REAL NOT NULL,   -- positivo = entrata, negativo = uscita
            ticker      TEXT,
            note        TEXT,
            balance_after REAL NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        """)
        # Migrazione: aggiungi colonna pipeline se non esiste (DB pre-dual-pipeline)
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN pipeline TEXT DEFAULT 'unknown'")
            conn.commit()
        except Exception:
            pass  # colonna già presente
        # Indice su pipeline creato dopo la migration (evita errore su DB vecchi)
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_pipeline ON signals(pipeline)")
            conn.commit()
        except Exception:
            pass


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


def was_recently_alerted(source: str, ticker: str | None, hours: int = 6) -> bool:
    """
    True se un segnale della stessa fonte+ticker e' gia' stato alertato
    nelle ultime `hours` ore in QUESTO database.

    Mitiga (non risolve del tutto) i doppi alert quando il demone locale
    e il job cloud schedulato girano sullo stesso DB (es. import/export
    manuale) o quando lo stesso processo scansiona due volte la stessa
    finestra RSS. Non protegge contro demone locale e job cloud che usano
    due FILE DB fisicamente separati (limite architetturale noto).
    """
    if not ticker:
        return False
    row = _conn().execute(
        "SELECT 1 FROM signals WHERE source=? AND ticker=? AND alerted=1 "
        "AND created_at >= datetime('now', ?) LIMIT 1",
        (source, ticker, f"-{hours} hours"),
    ).fetchone()
    return row is not None


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


def get_open_cycles() -> list:
    """Restituisce tutti i cicli wheel ancora aperti (non closed)."""
    return _conn().execute(
        "SELECT * FROM wheel_cycles WHERE phase != 'closed' ORDER BY opened_at DESC"
    ).fetchall()


def get_open_cycle(ticker: str):
    """Restituisce il ciclo aperto più recente per un ticker, o None."""
    return _conn().execute(
        "SELECT * FROM wheel_cycles WHERE ticker=? AND phase != 'closed' "
        "ORDER BY opened_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()


def upsert_position(ticker: str, cost_basis: float, shares: float,
                    entry_date: str | None = None) -> None:
    """Inserisce o aggiorna una posizione azionaria nel portafoglio."""
    ed = entry_date or datetime.utcnow().strftime("%Y-%m-%d")
    with tx() as conn:
        conn.execute(
            "INSERT INTO positions (ticker, entry_price, entry_date, shares) VALUES (?,?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET entry_price=excluded.entry_price, "
            "shares=excluded.shares, entry_date=excluded.entry_date",
            (ticker, cost_basis, ed, shares),
        )


def get_position(ticker: str):
    return _conn().execute(
        "SELECT * FROM positions WHERE ticker=?", (ticker,)
    ).fetchone()


def get_all_positions() -> list:
    return _conn().execute("SELECT * FROM positions WHERE shares > 0").fetchall()


def get_income_report(year: int | None = None, month: int | None = None) -> dict:
    """Riepilogo income per anno/mese: premi chiusi + cicli aperti."""
    now = datetime.utcnow()
    year  = year  or now.year
    month = month or now.month
    conn  = _conn()

    month_str = f"{year}-{month:02d}"
    # Cicli chiusi nel mese
    closed = conn.execute(
        "SELECT ticker, pnl_realized, closed_at FROM wheel_cycles "
        "WHERE phase='closed' AND strftime('%Y-%m', closed_at)=? "
        "ORDER BY closed_at",
        (month_str,),
    ).fetchall()

    # Cicli aperti (premi non ancora realizzati)
    open_cyc = conn.execute(
        "SELECT ticker, premium_received, premium_current, phase FROM wheel_cycles "
        "WHERE phase != 'closed'"
    ).fetchall()

    total_realized  = sum(float(r["pnl_realized"] or 0) for r in closed)
    total_unrealized = sum(
        (float(r["premium_received"]) - float(r["premium_current"])) * 100
        for r in open_cyc
    )

    # YTD
    ytd = conn.execute(
        "SELECT SUM(pnl_realized) FROM wheel_cycles "
        "WHERE phase='closed' AND strftime('%Y', closed_at)=?",
        (str(year),),
    ).fetchone()[0] or 0.0

    return {
        "year": year, "month": month,
        "closed_cycles": [dict(r) for r in closed],
        "open_cycles":   [dict(r) for r in open_cyc],
        "total_realized": total_realized,
        "total_unrealized": total_unrealized,
        "ytd_realized": float(ytd),
        "n_closed": len(closed),
    }


# ── Capital helpers ───────────────────────────────────────────────────────────

def get_capital() -> dict:
    """Restituisce stato capitale: balance cash, seed, e log recente."""
    conn = _conn()
    row  = conn.execute("SELECT * FROM capital WHERE id=1").fetchone()
    log  = conn.execute(
        "SELECT * FROM capital_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    return {
        "balance":  float(row["balance"]) if row else 0.0,
        "seed":     float(row["seed"])    if row else 0.0,
        "log":      [dict(r) for r in log],
    }


def seed_capital(amount: float) -> None:
    """Imposta il capitale iniziale (solo prima volta — se seed già > 0 non fa nulla)."""
    with tx() as conn:
        row = conn.execute("SELECT seed FROM capital WHERE id=1").fetchone()
        if row and float(row["seed"]) > 0:
            return  # già seeded
        conn.execute(
            "UPDATE capital SET balance=?, seed=?, updated_at=datetime('now') WHERE id=1",
            (amount, amount),
        )
        conn.execute(
            "INSERT INTO capital_log (event, amount, note, balance_after) VALUES (?,?,?,?)",
            ("seed", amount, f"Capitale iniziale ${amount:.2f}", amount),
        )


def log_capital(event: str, amount: float, ticker: str | None = None,
                note: str = "") -> float:
    """Aggiorna il balance e registra il movimento. Restituisce il nuovo balance."""
    with tx() as conn:
        row = conn.execute("SELECT balance FROM capital WHERE id=1").fetchone()
        current = float(row["balance"]) if row else 0.0
        new_bal = current + amount
        conn.execute(
            "UPDATE capital SET balance=?, updated_at=datetime('now') WHERE id=1",
            (new_bal,),
        )
        conn.execute(
            "INSERT INTO capital_log (event, amount, ticker, note, balance_after) VALUES (?,?,?,?,?)",
            (event, amount, ticker, note, new_bal),
        )
        return new_bal
