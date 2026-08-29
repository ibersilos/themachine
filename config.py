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

# Database
DB_PATH: Path = Path(os.getenv("DB_PATH", "data/the_machine.db"))

# Risk controls
STOP_LOSS_PCT: float        = _float("STOP_LOSS_PCT", 0.15)
# Disattivato di proposito (27/08/2026): la strategia e' comprare titoli che
# si e' disposti a tenere per il dividendo — uno stop-loss a prezzo vende
# proprio quando si vorrebbe tenere/mediare. Il controllo di rischio vero e'
# _check_thesis_break() in wheel_daemon.py (taglio dividendo, non prezzo).
PRICE_STOP_LOSS_ENABLED: bool = os.getenv("PRICE_STOP_LOSS_ENABLED", "false").lower() in ("1", "true", "yes")
MAX_MONTHLY_DRAWDOWN: float = _float("MAX_MONTHLY_DRAWDOWN", 0.20)
DRAWDOWN_PAUSE_DAYS: int    = _int("DRAWDOWN_PAUSE_DAYS", 30)

# Scoring
MIN_ALERT_SCORE: int      = _int("MIN_ALERT_SCORE", 20)
STRONG_BUY_THRESHOLD: int = _int("STRONG_BUY_THRESHOLD", 50)

# Alert dispatch — interruttore per pipeline. Default: solo stock-picking
# (small/mid cap, più drift post-notizia). WHEEL disattivato finché il
# capitale dedicato al wheel non giustifica nuove posizioni — i segnali
# restano comunque salvati nel DB, solo non generano alert Telegram.
WHEEL_ALERTS_ENABLED: bool = os.getenv("WHEEL_ALERTS_ENABLED", "false").lower() in ("1", "true", "yes")

# ── Dual pipeline filters ─────────────────────────────────────────────────────
# STOCK_PICKING (fonti: form4 + usaspending)
PICK_CAP_MIN:   int   = _int("PICK_CAP_MIN",    50_000_000)   # $50M  — evita micro cap
PICK_CAP_MAX:   int   = _int("PICK_CAP_MAX",   500_000_000)   # $500M — small/mid cap
PICK_VOL_MIN:   int   = _int("PICK_VOL_MIN",       200_000)   # volume medio giornaliero minimo
PICK_PRICE_MIN: float = _float("PICK_PRICE_MIN",       2.0)   # prezzo minimo $
PICK_PRICE_MAX: float = _float("PICK_PRICE_MAX",      20.0)   # prezzo massimo $

# WHEEL_CANDIDATES (fonte: edgar_8k) — filtri base scoring_engine
WHEEL_CAP_MIN:      int   = _int("WHEEL_CAP_MIN",   1_000_000_000)  # $1B — liquidità opzioni
WHEEL_OI_MIN:       int   = _int("WHEEL_OI_MIN",             100)   # OI minimo per strike
WHEEL_VRP_MIN:      float = _float("WHEEL_VRP_MIN",           1.1)  # VRP min (IV/HV20) — stockpile

# WHEEL_CANDIDATES — parametri wheel_scanner (3-tier, ispirato wheel-scout + stockpile)
WHEEL_DTE_MIN:          int   = _int("WHEEL_DTE_MIN",           14)   # DTE minimo
WHEEL_DTE_MAX:          int   = _int("WHEEL_DTE_MAX",           42)   # DTE massimo
WHEEL_MAX_SPREAD_PCT:   float = _float("WHEEL_MAX_SPREAD_PCT",  0.10) # max spread bid-ask %
WHEEL_MAX_SPREAD_ABS:   float = _float("WHEEL_MAX_SPREAD_ABS",  0.05) # OPPURE spread assoluto $ max
# — su opzioni a premio basso ($0.15-0.25) lo spread % esplode anche con
# tick minimo del market maker (bid/ask $0.05 di differenza = 20-30%),
# scartando opzioni con OI altissimo (es. PBR $17P: OI 9.518, spread $0.04
# = "solo" percentuale alta). Passa se soddisfa la % OPPURE il $ assoluto.
# Bug trovato 27/08/2026 verificando PBR sul chain reale.
WHEEL_MIN_PREMIUM:      float = _float("WHEEL_MIN_PREMIUM",     0.10) # premio minimo $/contratto — solo
# floor anti-rumore (evita mid quasi zero/illiquidi): la soglia economica
# vera e' WHEEL_ANN_RETURN_MIN, che scala col prezzo. A $0.30 fisso
# scartava opportunita' valide su titoli piu' cari di F (es. T, PBR con
# ann.ret 15-22% ma premio assoluto $0.15-0.23) — bug trovato 27/08/2026.
WHEEL_ANN_RETURN_MIN:   float = _float("WHEEL_ANN_RETURN_MIN",  15.0) # rendimento annualizzato min %

# Selezione strike per delta (Black-Scholes) invece di banda fissa % dello
# spot — si auto-adatta a IV/tempo residuo invece di ignorarli. Standard di
# settore per CSP: delta 0.16-0.30 (~16-30% probabilita' di assegnazione).
WHEEL_PUT_DELTA_MIN:    float = _float("WHEEL_PUT_DELTA_MIN",   0.16)
WHEEL_PUT_DELTA_MAX:    float = _float("WHEEL_PUT_DELTA_MAX",   0.30)

# HV Rank minimo (percentile 0-100, proxy di IV Rank — vedi wheel_scanner._compute_hv_rank)
# per vendere premio — sotto soglia la volatilita' non e' oggettivamente
# elevata per quel titolo nel suo range recente, anche se VRP>1.1 nel momento.
WHEEL_MIN_HV_RANK:      float = _float("WHEEL_MIN_HV_RANK",     30.0)

# Tetto di concentrazione per singolo sottostante — % del valore totale del
# bucket (posizioni + cash). Gap trovato il 27/08/2026 aprendo PBR senza un
# limite esplicito: ogni posizione veniva valutata isolata, mai contro il
# totale del bucket.
WHEEL_MAX_CONCENTRATION_PCT: float = _float("WHEEL_MAX_CONCENTRATION_PCT", 40.0)

# ── Tier-1 universe — candidati wheel a capitale ridotto ─────────────────────
# Criteri: prezzo $10-30 (lotto 100az abbordabile con capitale piccolo),
# dividend yield >3%, market cap >$1B (liquidita' opzioni), sopra 200-SMA.
# Screening manuale 27/08/2026 — dati vanno riverificati prima di ogni uso,
# non e' uno screener live. PBR ha rischio EM/valuta/politico aggiuntivo
# (Petrobras, statale brasiliana) — yield alto ma piu' volatile del gruppo.
# Mortgage REIT ad alto yield (NLY, AGNC) esclusi di proposito: profilo di
# rischio diverso (leva su mutui, erosione NAV nel tempo) da un dividend
# grower — da aggiungere solo con scelta consapevole, non come default.
WHEEL_TIER1_UNIVERSE: list[str] = [
    "F",     # Ford — gia' in portafoglio
    "T",     # AT&T — div 4.3%, liquidita' altissima
    "PFE",   # Pfizer — div 6.2%
    "KEY",   # KeyCorp — div 3.8%, banca regionale
    "PBR",   # Petrobras — div 9.4%, rischio EM/valuta piu' alto del gruppo
]

# yfinance
FUNDAMENTALS_CACHE_TTL: int = _int("FUNDAMENTALS_CACHE_TTL", 3600)

# ── API Server (FastAPI/uvicorn) ──────────────────────────────────────────────
API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = _int("API_PORT", 8080)

# ── Interactive Brokers (ib_insync) ──────────────────────────────────────────
# Host IB Gateway / TWS (di solito localhost)
IBKR_HOST: str             = os.getenv("IBKR_HOST", "127.0.0.1")
# Porta: TWS live=7496, TWS paper=7497, Gateway live=4001, Gateway paper=4002
IBKR_PORT: int             = _int("IBKR_PORT", 7497)
# clientId univoco per questa app (non condividere con altre sessioni API)
IBKR_CLIENT_ID: int        = _int("IBKR_CLIENT_ID", 10)
# Account IBKR specifico (vuoto = account principale); es: "U1234567"
IBKR_ACCOUNT: str          = os.getenv("IBKR_ACCOUNT", "")
# Timeout connessione in secondi
IBKR_CONNECT_TIMEOUT: int  = _int("IBKR_CONNECT_TIMEOUT", 20)
# Delay iniziale retry su disconnessione (raddoppia a ogni tentativo, cap 300s)
IBKR_RECONNECT_DELAY: int  = _int("IBKR_RECONNECT_DELAY", 30)
# Numero massimo tentativi di riconnessione prima di loggare CRITICAL
IBKR_MAX_RETRIES: int      = _int("IBKR_MAX_RETRIES", 10)
# Intervallo tra sync di posizioni nel loop daemon (secondi)
IBKR_SYNC_INTERVAL: int    = _int("IBKR_SYNC_INTERVAL", 60)
IBKR_CP_PORT:      int    = _int("IBKR_CP_PORT", 5055)   # Client Portal Gateway
# Se True: stop loss vengono loggati ma NON inviati a IBKR (test sicuro)
IBKR_DRY_RUN: bool         = os.getenv("IBKR_DRY_RUN", "true").lower() in ("1", "true", "yes")

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
# Giorni prima dell'ex-dividend entro cui avvisare sul rischio di assegnazione anticipata
# per cicli covered call gia' aperti (non solo in fase di apertura nuovo ciclo)
HOGUE_DIV_WARNING_DAYS: int = _int("HOGUE_DIV_WARNING_DAYS", 7)
# Profitto su stock per triggera Collar automatico
HOGUE_COLLAR_TRIGGER_PCT: float = _float("HOGUE_COLLAR_TRIGGER_PCT", 0.20)
# Collar: costo netto sotto cui segnalare come "protezione quasi gratuita"
HOGUE_FREE_COLLAR_THRESHOLD: float = _float("HOGUE_FREE_COLLAR_THRESHOLD", 0.05)
# Target cicli/anno (usato per annualizzazione)
HOGUE_TARGET_CYCLES_YEAR: int = _int("HOGUE_TARGET_CYCLES_YEAR", 16)
# DTE target per selezione opzioni (cerca expiry vicina a questo valore)
HOGUE_TARGET_DTE: int         = _int("HOGUE_TARGET_DTE", 35)
