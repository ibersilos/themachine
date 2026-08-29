"""
ibkr_connector.py — IBKR Client Portal Web API connector.

Compatibile con IBKR Desktop tramite Client Portal Gateway (porta 5055).
Usa REST HTTP invece di socket ib_insync — nessuna dipendenza da ib_insync.

Setup (una-tantum):
  1. Scarica Client Portal Gateway da:
     https://www.interactivebrokers.com/en/trading/ib-api.php
     → sezione "Client Portal API" → "Download Gateway"
  2. Estrailo in una cartella, es: C:\\ibkr-gateway\\
  3. Avvialo: cd C:\\ibkr-gateway && bin\\run.bat root\\conf.yaml
  4. Apri https://localhost:5055 nel browser e fai login con le tue credenziali IBKR
  5. Da quel momento il gateway è autenticato e risponde alle API

Porta default: 5055 (configurabile in .env come IBKR_CP_PORT=5055)

Funzionalità Level 1 (advisory):
  - Lettura account summary (NetLiq, cash, PnL)
  - Lettura posizioni stock e opzioni live
  - Sincronizzazione posizioni nel DB (positions + wheel_cycles)
  - Keepalive automatico ogni 55 secondi (sessione CP scade senza tickle)
  - Nessun ordine eseguito (DRY_RUN sempre attivo in Level 1)

Funzionalità Level 2 (semi-auto, futura):
  - Invio ordini previa approvazione Telegram
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import httpx

import config
import database as db

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────

_CP_PORT  = int(getattr(config, "IBKR_CP_PORT",  5055))
_BASE_URL = f"https://localhost:{_CP_PORT}/v1/api"
_HEADERS  = {"Content-Type": "application/json"}
_TIMEOUT  = 10


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class AccountSummary:
    account_id:    str
    net_liq:       float = 0.0
    total_cash:    float = 0.0
    gross_pos_val: float = 0.0
    unrealized_pnl: float = 0.0
    currency:      str = "USD"


@dataclass
class IBKRPosition:
    account_id:  str
    ticker:      str
    sec_type:    str          # STK | OPT | FUT
    position:    float        # positivo = long, negativo = short
    avg_cost:    float
    market_val:  float = 0.0
    unrealized:  float = 0.0
    # opzioni
    right:       str = ""     # C | P
    strike:      float = 0.0
    expiry:      str = ""     # YYYY-MM-DD
    conid:       int = 0


@dataclass
class SyncResult:
    """Risultato di un ciclo di sincronizzazione (compatibile con main.py)."""
    synced:      list = field(default_factory=list)
    stop_orders: list = field(default_factory=list)
    errors:      list = field(default_factory=list)
    timestamp:   float = 0.0
    connected:   bool = False

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def ok(self) -> bool:
        return self.connected and not self.errors


# ── Client Portal HTTP client ─────────────────────────────────────────────────

class CPClient:
    """
    Client REST per IBKR Client Portal Gateway.
    Gestisce SSL self-signed, keepalive, retry base.
    """

    def __init__(self, base: str = _BASE_URL):
        self._base   = base
        self._client = httpx.Client(
            verify=False,       # certificato self-signed del gateway locale
            timeout=_TIMEOUT,
            headers=_HEADERS,
        )
        self._account_id: Optional[str] = None
        self._authenticated = False

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, **params) -> dict | list | None:
        try:
            r = self._client.get(f"{self._base}/{path}", params=params)
            if r.status_code == 401:
                self._authenticated = False
                logger.warning("CP API: 401 Unauthorized — riloggati su https://localhost:%d", _CP_PORT)
                return None
            r.raise_for_status()
            return r.json()
        except httpx.ConnectError:
            logger.error("CP Gateway non raggiungibile su %s — avvialo e riprova", self._base)
            return None
        except Exception as exc:
            logger.warning("CP GET /%s: %s", path, exc)
            return None

    def _post(self, path: str, json: dict | None = None) -> dict | list | None:
        try:
            r = self._client.post(f"{self._base}/{path}", json=json or {})
            if r.status_code == 401:
                self._authenticated = False
                return None
            r.raise_for_status()
            return r.json()
        except httpx.ConnectError:
            logger.error("CP Gateway non raggiungibile su %s", self._base)
            return None
        except Exception as exc:
            logger.warning("CP POST /%s: %s", path, exc)
            return None

    # ── Auth / Keepalive ──────────────────────────────────────────────────────

    def auth_status(self) -> bool:
        """Restituisce True se la sessione è autenticata."""
        data = self._get("iserver/auth/status")
        if data and isinstance(data, dict):
            authenticated = data.get("authenticated", False)
            connected     = data.get("connected", False)
            self._authenticated = bool(authenticated and connected)
            logger.debug("CP auth: authenticated=%s connected=%s", authenticated, connected)
            return self._authenticated
        return False

    def tickle(self) -> bool:
        """
        Keepalive: la sessione CP scade dopo ~60s senza attività.
        Deve essere chiamato ogni ~55 secondi.
        """
        data = self._post("tickle")
        return data is not None

    def reauthenticate(self) -> bool:
        """Tenta una re-autenticazione SSO senza aprire il browser."""
        self._post("iserver/reauthenticate")
        time.sleep(2)
        return self.auth_status()

    # ── Account ───────────────────────────────────────────────────────────────

    def get_accounts(self) -> list[str]:
        data = self._get("iserver/accounts")
        if not data:
            return []
        if isinstance(data, dict):
            acc = data.get("selectedAccount") or ""
            all_acc = data.get("accounts") or []
            return [acc] + [a for a in all_acc if a != acc]
        if isinstance(data, list):
            return data
        return []

    def get_account_id(self) -> Optional[str]:
        if not self._account_id:
            accs = self.get_accounts()
            self._account_id = accs[0] if accs else None
        return self._account_id

    def is_paper_account(self) -> bool:
        """
        Account IBKR paper trading iniziano con 'DU', i conti live con 'U'.
        Guardia di sicurezza indipendente da qualsiasi flag di config — se
        questo ritorna False, place_order() rifiuta sempre, a prescindere
        da PAPER_TRADING_MODE. Difesa in profondita': un config sbagliato
        non deve poter piazzare un ordine su capitale reale.
        """
        acc = self.get_account_id()
        return bool(acc) and acc.upper().startswith("DU")

    def get_account_summary(self) -> Optional[AccountSummary]:
        acc = self.get_account_id()
        if not acc:
            return None
        data = self._get(f"portfolio/{acc}/summary")
        if not data or not isinstance(data, dict):
            return None

        def _val(key: str) -> float:
            v = data.get(key, {})
            if isinstance(v, dict):
                return float(v.get("amount", 0) or 0)
            return float(v or 0)

        return AccountSummary(
            account_id=acc,
            net_liq=_val("netliquidation"),
            total_cash=_val("totalcashvalue"),
            gross_pos_val=_val("grosspositionvalue"),
            unrealized_pnl=_val("unrealizedpnl"),
            currency=data.get("currency", "USD"),
        )

    # ── Posizioni ─────────────────────────────────────────────────────────────

    def get_positions(self, page: int = 0) -> list[IBKRPosition]:
        acc = self.get_account_id()
        if not acc:
            return []
        data = self._get(f"portfolio/{acc}/positions/{page}")
        if not data or not isinstance(data, list):
            return []

        result = []
        for p in data:
            sec_type = p.get("assetClass", "").upper()
            ticker   = p.get("ticker") or p.get("symbol") or ""
            conid    = int(p.get("conid") or 0)
            pos_size = float(p.get("position") or 0)
            avg_cost = float(p.get("avgCost") or p.get("averageCost") or 0)
            mkt_val  = float(p.get("mktValue") or 0)
            unreal   = float(p.get("unrealizedPnl") or 0)

            pos = IBKRPosition(
                account_id=acc, ticker=ticker, sec_type=sec_type,
                position=pos_size, avg_cost=avg_cost,
                market_val=mkt_val, unrealized=unreal, conid=conid,
            )

            if sec_type == "OPT":
                pos.right  = p.get("putOrCall", "").upper()[:1]    # C | P
                pos.strike = float(p.get("strike") or 0)
                raw_exp    = str(p.get("expiry") or p.get("lastTradingDayOrContractMonth") or "")
                if len(raw_exp) == 8 and raw_exp.isdigit():
                    pos.expiry = f"{raw_exp[:4]}-{raw_exp[4:6]}-{raw_exp[6:8]}"
                else:
                    pos.expiry = raw_exp

            result.append(pos)

        return result

    def get_all_positions(self) -> list[IBKRPosition]:
        """Legge tutte le pagine di posizioni (max 10 pagine)."""
        all_pos = []
        for page in range(10):
            page_data = self.get_positions(page)
            if not page_data:
                break
            all_pos.extend(page_data)
            if len(page_data) < 100:
                break
        return all_pos

    # ── Prezzi live ───────────────────────────────────────────────────────────

    def get_market_snapshot(self, conids: list[int], fields: list[str] | None = None) -> dict:
        """
        Richiede snapshot di mercato per una lista di conId.
        fields default: 31 (last), 84 (bid), 85 (ask), 86 (volume)
        """
        if not conids:
            return {}
        fields_str = ",".join(fields or ["31", "84", "85", "86"])
        conids_str = ",".join(str(c) for c in conids)
        data = self._get("iserver/marketdata/snapshot", conids=conids_str, fields=fields_str)
        if not data or not isinstance(data, list):
            return {}
        result = {}
        for item in data:
            cid = item.get("conid")
            if cid:
                bid  = _parse_price(item.get("84"))
                ask  = _parse_price(item.get("85"))
                last = _parse_price(item.get("31"))
                mid  = round((bid + ask) / 2, 4) if bid and ask else last
                result[cid] = {"bid": bid, "ask": ask, "last": last, "mid": mid}
        return result

    # ── Ordini (SOLO paper — vedi guardia in place_order) ───────────────────────

    def place_order(self, conid: int, side: str, quantity: float, order_type: str = "LMT",
                     price: float | None = None, tif: str = "DAY") -> dict:
        """
        Piazza un ordine reale via IBKR — RIFIUTA sempre se l'account
        collegato non e' un conto paper (is_paper_account() == False),
        indipendentemente da qualsiasi config. Nessuna eccezione a questa
        regola: e' l'unica barriera tra un bug e capitale vero.

        side: 'BUY' | 'SELL'. order_type: 'LMT' | 'MKT'. price obbligatorio per LMT.
        Gestisce la conferma in due passi che l'API IBKR richiede (un ordine
        puo' tornare un "reply" con warning da confermare — es. "sei su un
        conto paper" — prima di essere effettivamente piazzato).
        """
        if not self.is_paper_account():
            acc = self.get_account_id()
            logger.error("place_order RIFIUTATO: account %s non e' un conto paper (serve prefisso DU)", acc)
            return {"error": f"account {acc} non e' paper trading — ordine rifiutato per sicurezza"}

        acc = self.get_account_id()
        if not acc:
            return {"error": "nessun account collegato"}

        if order_type == "LMT" and price is None:
            return {"error": "price obbligatorio per ordine LMT"}

        order = {
            "conid": conid,
            "orderType": order_type,
            "side": side.upper(),
            "quantity": quantity,
            "tif": tif,
        }
        if price is not None:
            order["price"] = price

        data = self._post(f"iserver/account/{acc}/orders", json={"orders": [order]})
        if not data:
            return {"error": "nessuna risposta da IBKR"}

        # IBKR puo' rispondere con una lista di "reply" da confermare (warning
        # su prezzo, conto paper, ecc.) invece di piazzare subito l'ordine.
        if isinstance(data, list) and data and "id" in data[0] and "orderId" not in data[0]:
            reply_id = data[0]["id"]
            logger.info("place_order: conferma reply %s (warning: %s)", reply_id, data[0].get("message"))
            confirm = self._post(f"iserver/reply/{reply_id}", json={"confirmed": True})
            return {"result": confirm, "note": "confermato reply automaticamente (solo paper)"}

        return {"result": data}

    def close(self):
        self._client.close()


def _parse_price(val) -> Optional[float]:
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


# ── IBKRConnector — facade principale ─────────────────────────────────────────

class IBKRConnector:
    """
    Facade per integrazione con main.py.
    Gestisce keepalive, sync loop e interfaccia compatibile col codice esistente.
    """

    def __init__(self):
        self._cp         = CPClient()
        self._stop       = threading.Event()
        self._lock       = threading.Lock()
        self._connected  = False
        self._keepalive_thread: Optional[threading.Thread] = None

    # ── Connessione ───────────────────────────────────────────────────────────

    def connect(self) -> bool:
        ok = self._cp.auth_status()
        if ok:
            self._connected = True
            logger.info("IBKR Client Portal: autenticato (porta %d)", _CP_PORT)
            self._start_keepalive()
        else:
            logger.warning(
                "IBKR Client Portal non autenticato. "
                "Avvia il gateway e fai login su https://localhost:%d", _CP_PORT
            )
            self._connected = False
        return ok

    def disconnect(self):
        self._stop.set()
        self._connected = False
        self._cp.close()
        logger.info("IBKR Client Portal: disconnesso")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Keepalive thread ──────────────────────────────────────────────────────

    def _start_keepalive(self):
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, daemon=True, name="ibkr-keepalive"
        )
        self._keepalive_thread.start()
        logger.info("IBKR keepalive thread avviato (ogni 55s)")

    def _keepalive_loop(self):
        while not self._stop.wait(55):
            try:
                ok = self._cp.tickle()
                if not ok:
                    # Prova re-autenticazione silenziosa
                    self._cp.reauthenticate()
                logger.debug("IBKR keepalive: ok=%s", ok)
            except Exception as exc:
                logger.warning("Keepalive error: %s", exc)

    # ── Sync principale ───────────────────────────────────────────────────────

    def sync_wheel_positions(self) -> SyncResult:
        """
        Legge posizioni da IBKR Client Portal e sincronizza nel DB.

        Stock positions → aggiorna tabella positions
        Short call/put  → aggiorna premium_current in wheel_cycles
        """
        result = SyncResult(connected=self._connected)

        if not self._connected:
            result.errors.append("Non connesso — avvia Client Portal Gateway")
            return result

        try:
            positions = self._cp.get_all_positions()
        except Exception as exc:
            result.errors.append(f"get_all_positions: {exc}")
            return result

        stocks    = [p for p in positions if p.sec_type == "STK"]
        options   = [p for p in positions if p.sec_type == "OPT"]
        short_opt = [p for p in options if p.position < 0]

        # ── 1. Sincronizza posizioni azionarie nel DB ─────────────────────────
        for s in stocks:
            try:
                existing = db.get_position(s.ticker)
                if not existing:
                    db.upsert_position(s.ticker, s.avg_cost, s.position)
                    logger.info("Posizione IBKR importata: %s %.0faz @ $%.2f",
                                s.ticker, s.position, s.avg_cost)
                else:
                    # aggiorna solo shares (il costo medio è quello che ha inserito l'utente)
                    with db.tx() as conn:
                        conn.execute(
                            "UPDATE positions SET shares=? WHERE ticker=?",
                            (s.position, s.ticker),
                        )
            except Exception as exc:
                result.errors.append(f"sync stock {s.ticker}: {exc}")

        # ── 1b. Stop loss a prezzo su ogni posizione azionaria — disattivato di
        # default (config.PRICE_STOP_LOSS_ENABLED=False, 27/08/2026): la
        # strategia compra titoli che si e' disposti a tenere per il
        # dividendo, uno stop a prezzo venderebbe proprio quando si vorrebbe
        # tenere. Il controllo di rischio vero e' su rottura di tesi (taglio
        # dividendo) — vedi wheel_daemon._check_thesis_break().
        if config.PRICE_STOP_LOSS_ENABLED:
            stock_conids = [s.conid for s in stocks if s.conid]
            stock_snap   = self._cp.get_market_snapshot(stock_conids) if stock_conids else {}

            for s in stocks:
                try:
                    entry_row   = db.get_position(s.ticker)
                    entry_price = float(entry_row["entry_price"]) if entry_row else s.avg_cost
                    stock_mid   = stock_snap.get(s.conid, {}).get("mid") or 0
                    if stock_mid and entry_price:
                        pnl_pct = (stock_mid - entry_price) / entry_price
                        if pnl_pct <= -config.STOP_LOSS_PCT:
                            logger.warning("STOP LOSS: %s @ $%.2f (entry $%.2f, %.1f%%)",
                                           s.ticker, stock_mid, entry_price, pnl_pct * 100)
                            result.stop_orders.append(s.ticker)
                            # Level 1: alert solo via Telegram (nessun ordine automatico)
                            from telegram_bot import check_stop_loss
                            check_stop_loss(s.ticker, entry_price, stock_mid)
                except Exception as exc:
                    result.errors.append(f"stop-loss check {s.ticker}: {exc}")

        # ── 2. Aggiorna premium_current dalle opzioni live ───────────────────
        if short_opt:
            conids = [p.conid for p in short_opt if p.conid]
            snap   = self._cp.get_market_snapshot(conids) if conids else {}

        for opt in short_opt:
            try:
                ticker = opt.ticker
                strike = opt.strike
                expiry = opt.expiry
                phase  = "covered_call" if opt.right == "C" else "csp"

                # Trova ciclo nel DB
                cycle = self._find_db_cycle(ticker, strike, expiry, phase)
                if cycle is None:
                    # Ciclo non presente nel DB → crea automaticamente
                    logger.info("Opzione IBKR non in DB: %s %s $%.1f %s — registro",
                                ticker, opt.right, strike, expiry)
                    prem = snap.get(opt.conid, {}).get("mid") or abs(opt.avg_cost)
                    db.open_wheel_cycle(ticker, strike, expiry, prem, phase)
                    result.synced.append(f"{ticker} {opt.right} ${strike} {expiry} (nuovo)")
                    continue

                # Aggiorna premium live
                mid = snap.get(opt.conid, {}).get("mid")
                if mid is not None and mid > 0:
                    db.update_wheel_premium(cycle["id"], mid)
                    logger.info("Premium aggiornato: %s $%.1f %s → $%.3f",
                                ticker, strike, expiry, mid)

                result.synced.append(f"{ticker} {opt.right} ${strike} {expiry}")

            except Exception as exc:
                result.errors.append(f"sync opt {opt.ticker}: {exc}")

        logger.info(
            "IBKR sync: %d posizioni, %d stop, %d errori",
            len(result.synced), len(result.stop_orders), len(result.errors),
        )
        return result

    def _find_db_cycle(self, ticker: str, strike: float, expiry: str, phase: str):
        rows = db.get_wheel_cycles_year(ticker)
        for row in rows:
            if row["phase"] == "closed":
                continue
            if abs(float(row["strike"] or 0) - strike) > 0.01:
                continue
            if row["expiry"] == expiry:
                return row
            # Tolleranza scadenza ±1 giorno (normalizzazione data IBKR)
            try:
                db_d  = date.fromisoformat(row["expiry"])
                opt_d = date.fromisoformat(expiry)
                if abs((db_d - opt_d).days) <= 1:
                    return row
            except Exception:
                pass
        return None

    # ── Sync loop daemon ──────────────────────────────────────────────────────

    def run_sync_loop(
        self,
        on_sync: Callable[[SyncResult], None] | None = None,
        interval: int | None = None,
    ) -> None:
        secs = interval or config.IBKR_SYNC_INTERVAL
        logger.info("IBKR sync loop avviato (ogni %ds)", secs)

        while not self._stop.wait(secs):
            if not self._connected:
                # Tenta riconnessione silenziosa
                self.connect()
                continue
            result = self.sync_wheel_positions()
            if on_sync:
                try:
                    on_sync(result)
                except Exception as exc:
                    logger.error("on_sync callback: %s", exc)

        logger.info("IBKR sync loop terminato")


# ── Factory ───────────────────────────────────────────────────────────────────

def start_ibkr_thread(
    on_sync: Callable[[SyncResult], None] | None = None,
) -> IBKRConnector:
    """
    Crea connector, tenta connessione, avvia sync loop daemon.
    Se il gateway non è attivo logga un warning e continua senza bloccare.
    """
    connector = IBKRConnector()

    if not connector.connect():
        logger.warning(
            "IBKR Client Portal Gateway non disponibile.\n"
            "  → Scaricalo da: https://www.interactivebrokers.com/en/trading/ib-api.php\n"
            "  → Avvialo: bin/run.bat root/conf.yaml\n"
            "  → Fai login su https://localhost:%d\n"
            "Il bot funziona normalmente; le posizioni IBKR non vengono sincronizzate.",
            _CP_PORT,
        )

    t = threading.Thread(
        target=connector.run_sync_loop,
        kwargs={"on_sync": on_sync},
        daemon=True,
        name="ibkr-sync",
    )
    t.start()
    return connector
