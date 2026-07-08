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
