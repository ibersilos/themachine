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

        -- Backtest: ritorno realizzato T0..T0+N per ogni segnale alertato,
        -- per validare se lo score della pipeline correla con edge reale.
        CREATE TABLE IF NOT EXISTS backtest_results (
            signal_id   INTEGER PRIMARY KEY REFERENCES signals(id),
            ticker      TEXT NOT NULL,
            score       INTEGER,
            source      TEXT,
            pipeline    TEXT,
            t0_date     TEXT,
            t0_price    REAL,
            t5_price    REAL,
            t10_price   REAL,
            t20_price   REAL,
            t60_price   REAL,
            return_5d   REAL,
            return_10d  REAL,
            return_20d  REAL,
            return_60d  REAL,
            updated_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_score  ON backtest_results(score);
        CREATE INDEX IF NOT EXISTS idx_backtest_source ON backtest_results(source);

        -- Ultimo digest "scan universo" inviato — evita di rimandare lo
        -- stesso alert ogni mattina se il miglior candidato non e' cambiato.
        CREATE TABLE IF NOT EXISTS universe_scan_state (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            ticker      TEXT,
            strike      REAL,
            expiry      TEXT,
            ann_return  REAL,
            sent_at     TEXT
        );

        -- Audit trail: ogni decisione/raccomandazione con la motivazione
        -- testuale, interrogabile — non solo il messaggio Telegram (che
        -- sparisce nella chat) ma un log strutturato persistente.
        CREATE TABLE IF NOT EXISTS decision_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT,
            action      TEXT NOT NULL,   -- es: universe_scan_top | thesis_break | concentration_block | strategy_compare
            rationale   TEXT NOT NULL,
            source      TEXT,            -- funzione/modulo che ha generato la decisione
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decision_log_ticker ON decision_log(ticker);
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


def sync_capital_from_broker(real_cash: float, note: str = "") -> float:
    """
    Forza il balance a coincidere col cash reale IBKR (fonte di verità).

    A differenza di log_capital() (delta incrementale — fragile: un trade
    eseguito manualmente su IBKR senza passare da /open o /close disallinea
    silenziosamente il ledger), questa funzione riallinea il balance al
    valore reale e registra la differenza come evento "broker_sync" per
    audit trail — non nasconde la discrepanza, la rende esplicita.
    """
    with tx() as conn:
        row = conn.execute("SELECT balance FROM capital WHERE id=1").fetchone()
        current = float(row["balance"]) if row else 0.0
        diff = real_cash - current
        conn.execute(
            "UPDATE capital SET balance=?, updated_at=datetime('now') WHERE id=1",
            (real_cash,),
        )
        conn.execute(
            "INSERT INTO capital_log (event, amount, note, balance_after) VALUES (?,?,?,?)",
            ("broker_sync", diff, note or f"Riallineato a cash reale IBKR ${real_cash:.2f} (diff ${diff:+.2f})", real_cash),
        )
    return real_cash


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


# ── Backtest helpers ──────────────────────────────────────────────────────────

def get_signals_for_backtest(min_age_days: int, max_age_days: int, limit: int = 200) -> list:
    """
    Segnali alertati, con ticker, abbastanza vecchi da avere almeno il
    ritorno a 5gg disponibile, non piu' vecchi di max_age_days (oltre non
    ha senso riprovare a scaricare storico), e non ancora completi
    (return_60d mancante = manca almeno un orizzonte).
    """
    return _conn().execute(
        """
        SELECT s.id, s.ticker, s.score, s.source, s.pipeline, s.created_at
        FROM signals s
        LEFT JOIN backtest_results b ON b.signal_id = s.id
        WHERE s.ticker IS NOT NULL
          AND s.alerted = 1
          AND s.created_at <= datetime('now', ?)
          AND s.created_at >= datetime('now', ?)
          AND (b.signal_id IS NULL OR b.return_60d IS NULL)
        ORDER BY s.created_at ASC
        LIMIT ?
        """,
        (f"-{min_age_days} days", f"-{max_age_days} days", limit),
    ).fetchall()


def upsert_backtest_result(signal_id: int, ticker: str, score: int | None,
                           source: str | None, pipeline: str | None,
                           t0_date: str, prices: dict, returns: dict) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
                (signal_id, ticker, score, source, pipeline, t0_date, t0_price,
                 t5_price, t10_price, t20_price, t60_price,
                 return_5d, return_10d, return_20d, return_60d, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(signal_id) DO UPDATE SET
                t0_price=excluded.t0_price,
                t5_price=excluded.t5_price, t10_price=excluded.t10_price,
                t20_price=excluded.t20_price, t60_price=excluded.t60_price,
                return_5d=excluded.return_5d, return_10d=excluded.return_10d,
                return_20d=excluded.return_20d, return_60d=excluded.return_60d,
                updated_at=datetime('now')
            """,
            (signal_id, ticker, score, source, pipeline, t0_date, prices.get("t0"),
             prices.get(5), prices.get(10), prices.get(20), prices.get(60),
             returns.get(5), returns.get(10), returns.get(20), returns.get(60)),
        )


def log_decision(action: str, rationale: str, ticker: str | None = None, source: str = "") -> None:
    """Registra una decisione/raccomandazione con motivazione — audit trail
    interrogabile, non solo il testo effimero di un alert Telegram."""
    with tx() as conn:
        conn.execute(
            "INSERT INTO decision_log (ticker, action, rationale, source) VALUES (?,?,?,?)",
            (ticker, action, rationale, source),
        )


def get_decision_log(ticker: str | None = None, limit: int = 50) -> list:
    conn = _conn()
    if ticker:
        return conn.execute(
            "SELECT * FROM decision_log WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_last_universe_scan() -> dict | None:
    row = _conn().execute("SELECT * FROM universe_scan_state WHERE id=1").fetchone()
    return dict(row) if row else None


def set_last_universe_scan(ticker: str, strike: float, expiry: str, ann_return: float) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO universe_scan_state (id, ticker, strike, expiry, ann_return, sent_at)
            VALUES (1, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                ticker=excluded.ticker, strike=excluded.strike, expiry=excluded.expiry,
                ann_return=excluded.ann_return, sent_at=excluded.sent_at
            """,
            (ticker, strike, expiry, ann_return),
        )


def get_backtest_stats_for(source: str, min_score: int) -> dict:
    """
    Aggrega i ritorni realizzati per una fonte segnale con score >= min_score
    (pool piu' ampio del bucket singolo di get_backtest_report — serve
    massimizzare n per stimare l'edge "trading" quando si confronta con
    l'annualized return del wheel su un segnale specifico).
    """
    row = _conn().execute(
        """
        SELECT
            COUNT(*) AS n,
            AVG(return_20d) AS avg_20d,
            SUM(CASE WHEN return_20d > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(SUM(CASE WHEN return_20d IS NOT NULL THEN 1 ELSE 0 END), 0) AS win_rate_20d
        FROM backtest_results
        WHERE source = ? AND score >= ?
        """,
        (source, min_score),
    ).fetchone()
    return {
        "n": row["n"] or 0,
        "avg_20d": float(row["avg_20d"]) if row["avg_20d"] is not None else None,
        "win_rate_20d": float(row["win_rate_20d"]) if row["win_rate_20d"] is not None else None,
    }


def get_backtest_report() -> list:
    """Aggrega i ritorni per bucket di score e per fonte del segnale."""
    return _conn().execute(
        """
        SELECT
            source,
            CASE
                WHEN score >= 50 THEN '50+'
                WHEN score >= 40 THEN '40-49'
                WHEN score >= 30 THEN '30-39'
                WHEN score >= 20 THEN '20-29'
                ELSE '<20'
            END AS bucket,
            COUNT(*) AS n,
            AVG(return_5d)  AS avg_5d,
            AVG(return_10d) AS avg_10d,
            AVG(return_20d) AS avg_20d,
            AVG(return_60d) AS avg_60d,
            SUM(CASE WHEN return_20d > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(SUM(CASE WHEN return_20d IS NOT NULL THEN 1 ELSE 0 END), 0) AS win_rate_20d
        FROM backtest_results
        GROUP BY source, bucket
        ORDER BY source, bucket DESC
        """
    ).fetchall()
