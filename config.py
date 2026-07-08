"""Central config – reads exclusively from environment / .env file."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v

def _float(key: str, default: float) -> float:
    return float(os.getenv(key, default))

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, default))

# Telegram
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str   = _require("TELEGRAM_CHAT_ID")

# SEC EDGAR
EDGAR_USER_AGENT: str       = os.getenv("EDGAR_USER_AGENT", "the-machine bot@example.com")
EDGAR_POLL_INTERVAL: int    = _int("EDGAR_POLL_INTERVAL", 60)

# USAspending
USASPENDING_POLL_INTERVAL: int = _int("USASPENDING_POLL_INTERVAL", 300)

# Google Drive
GOOGLE_SERVICE_ACCOUNT_JSON: Path = Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json"))
GOOGLE_DRIVE_FOLDER_ID: str       = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")

# Database
DB_PATH: Path = Path(os.getenv("DB_PATH", "data/the_machine.db"))

# Risk controls
STOP_LOSS_PCT: float        = _float("STOP_LOSS_PCT", 0.15)
MAX_MONTHLY_DRAWDOWN: float = _float("MAX_MONTHLY_DRAWDOWN", 0.20)
DRAWDOWN_PAUSE_DAYS: int    = _int("DRAWDOWN_PAUSE_DAYS", 30)

# Scoring
MIN_ALERT_SCORE: int      = _int("MIN_ALERT_SCORE", 60)
STRONG_BUY_THRESHOLD: int = _int("STRONG_BUY_THRESHOLD", 80)

# Serenity
SERENITY_ARCHIVE_PATH: Path = Path(os.getenv("SERENITY_ARCHIVE_PATH", "data/serenity_tweets.json"))
SERENITY_RECENCY_DAYS: int  = _int("SERENITY_RECENCY_DAYS", 30)

# yfinance
FUNDAMENTALS_CACHE_TTL: int = _int("FUNDAMENTALS_CACHE_TTL", 3600)

# ── Hogue Framework ───────────────────────────────────────────────────────────
# Chiusura anticipata: chiudi se catturato >= questa percentuale del premio
HOGUE_EARLY_CLOSE_PCT: float  = _float("HOGUE_EARLY_CLOSE_PCT", 0.50)
# Regola 21-DTE: chiudi sempre se catturato >= 50% E DTE <= questo valore
HOGUE_DTE_THRESHOLD: int      = _int("HOGUE_DTE_THRESHOLD", 21)
# Roll: massimo roll consentiti per posizione prima di lasciare assegnare
HOGUE_MAX_ROLLS: int          = _int("HOGUE_MAX_ROLLS", 2)
# Roll: valuta roll se stock_price > strike * questa soglia
HOGUE_ROLL_TRIGGER_PCT: float = _float("HOGUE_ROLL_TRIGGER_PCT", 0.97)
# IV Rank minimo per vendere calls (sotto → skip ciclo)
HOGUE_MIN_IV_RANK: float      = _float("HOGUE_MIN_IV_RANK", 20.0)
# IV Rank alto → regime aggressivo (Iron Condor eligibile)
HOGUE_HIGH_IV_RANK: float     = _float("HOGUE_HIGH_IV_RANK", 80.0)
# Calo massimo settimanale prima di bloccare vendita calls
HOGUE_WEEKLY_DROP_BLOCK: float = _float("HOGUE_WEEKLY_DROP_BLOCK", 0.10)
# Giorni da earnings per bloccare automaticamente vendita calls
HOGUE_EARNINGS_BUFFER_DAYS: int = _int("HOGUE_EARNINGS_BUFFER_DAYS", 7)
# Profitto su stock per triggera Collar automatico
HOGUE_COLLAR_TRIGGER_PCT: float = _float("HOGUE_COLLAR_TRIGGER_PCT", 0.20)
# Collar: costo netto sotto cui segnalare come "protezione quasi gratuita"
HOGUE_FREE_COLLAR_THRESHOLD: float = _float("HOGUE_FREE_COLLAR_THRESHOLD", 0.05)
# Target cicli/anno (usato per annualizzazione)
HOGUE_TARGET_CYCLES_YEAR: int = _int("HOGUE_TARGET_CYCLES_YEAR", 16)
# DTE target per selezione opzioni (cerca expiry vicina a questo valore)
HOGUE_TARGET_DTE: int         = _int("HOGUE_TARGET_DTE", 35)
