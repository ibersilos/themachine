"""
ibkr_connector.py — Interactive Brokers connector via ib_insync.

Funzionalità:
  - Connessione a IB Gateway o TWS con retry esponenziale
  - Lettura posizioni opzioni aperte (short calls per wheel strategy)
  - Sincronizzazione premium_current in wheel_cycles dal live feed IBKR
  - Popolamento WheelPosition con dati live dal DB
  - Ordine di stop loss automatico su stock se pnl_pct <= STOP_LOSS_PCT
  - Tutto configurabile via .env
  - Supporto DRY_RUN per test senza ordini reali

Dipendenza: ib_insync>=0.9.86
  pip install ib_insync

Prerequisito: IB Gateway o TWS aperto e configurato per API locale.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from ib_insync import IB, Stock, StopOrder, util

import config
import database as db
from covered_call_optimizer import WheelPosition

logger = logging.getLogger(__name__)

# ── Monkey-patch asyncio per compatibilità con thread non-async ───────────────
util.patchAsyncio()


# ─────────────────────────────────────────────────────────────────────────────
#  Dataclass risultato sync
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    """Risultato di un ciclo di sincronizzazione."""
    synced:        list[WheelPosition]
    stop_orders:   list[str]          # ticker che hanno triggerato stop loss
    errors:        list[str]
    timestamp:     float = 0.0

    def __post_init__(self):
        self.timestamp = time.time()

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  IBKRConnector
# ─────────────────────────────────────────────────────────────────────────────

class IBKRConnector:
    """
    Gestisce la connessione a IB Gateway / TWS, la lettura delle posizioni
    e l'invio di ordini di stop loss automatici.

    Uso tipico:
        connector = IBKRConnector()
        connector.connect()
        positions = connector.sync_wheel_positions()
        connector.disconnect()

    Oppure come daemon:
        connector = start_ibkr_thread(on_sync=my_callback)
        # ... lavora ...
        connector.disconnect()
    """

    def __init__(self) -> None:
        self._ib = IB()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._connected = False
        self._retry_count = 0

        # Registra handler disconnect
        self._ib.disconnectedEvent += self._on_disconnected

    # ── CONNESSIONE ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Connetti a IB Gateway / TWS.
        Legge host, porta, clientId da config (variabili .env).
        Restituisce True se la connessione ha avuto successo.
        """
        try:
            self._ib.connect(
                host=config.IBKR_HOST,
                port=config.IBKR_PORT,
                clientId=config.IBKR_CLIENT_ID,
                timeout=config.IBKR_CONNECT_TIMEOUT,
                readonly=False,
            )
            self._connected = True
            self._retry_count = 0
            logger.info(
                "IBKR connesso: %s:%d (clientId=%d, account=%s)",
                config.IBKR_HOST, config.IBKR_PORT,
                config.IBKR_CLIENT_ID, config.IBKR_ACCOUNT or "default",
            )
            return True
        except Exception as exc:
            logger.error("IBKR connect fallito: %s", exc)
            self._connected = False
            return False

    def _on_disconnected(self) -> None:
        """Callback ib_insync: fired quando la connessione cade."""
        self._connected = False
        logger.warning("IBKR disconnesso — avvio reconnect loop")
        t = threading.Thread(
            target=self._reconnect_loop,
            daemon=True,
            name="ibkr-reconnect",
        )
        t.start()

    def _reconnect_loop(self) -> None:
        """
        Tenta la riconnessione con backoff esponenziale.
        Si ferma dopo IBKR_MAX_RETRIES tentativi o se stop_event è settato.
        """
        delay = config.IBKR_RECONNECT_DELAY
        while (
            not self._stop_event.is_set()
            and self._retry_count < config.IBKR_MAX_RETRIES
        ):
            self._retry_count += 1
            logger.info(
                "Reconnect tentativo %d/%d in %ds...",
                self._retry_count, config.IBKR_MAX_RETRIES, delay,
            )
            self._stop_event.wait(delay)
            if self._stop_event.is_set():
                return
            if self.connect():
                return
            # Backoff esponenziale, cap a 5 minuti
            delay = min(delay * 2, 300)

        if self._retry_count >= config.IBKR_MAX_RETRIES:
            logger.critical(
                "IBKR: raggiunti %d tentativi. Intervento manuale necessario.",
                config.IBKR_MAX_RETRIES,
            )

    def disconnect(self) -> None:
        """Chiudi la connessione in modo pulito."""
        self._stop_event.set()
        if self._ib.isConnected():
            self._ib.disconnect()
        self._connected = False
        logger.info("IBKR disconnesso (clean shutdown)")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    # ── LETTURA POSIZIONI ──────────────────────────────────────────────────────

    def get_option_positions(self) -> list[dict]:
        """
        Restituisce tutte le posizioni opzioni aperte sull'account.
        Filtra per secType == 'OPT'.
        """
        if not self.is_connected:
            logger.warning("get_option_positions: non connesso")
            return []

        account = config.IBKR_ACCOUNT or ""
        result = []
        try:
            for pos in self._ib.positions(account=account):
                c = pos.contract
                if c.secType != "OPT":
                    continue
                # Normalizza la data scadenza IBKR (YYYYMMDD) → YYYY-MM-DD
                raw = c.lastTradeDateOrContractMonth or ""
                if len(raw) == 8 and raw.isdigit():
                    expiry_iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                else:
                    expiry_iso = raw

                result.append({
                    "account":    pos.account,
                    "ticker":     c.symbol,
                    "right":      c.right,          # 'C' = call, 'P' = put
                    "strike":     float(c.strike),
                    "expiry":     expiry_iso,        # YYYY-MM-DD
                    "position":   pos.position,      # negativo = short
                    "avg_cost":   pos.avgCost,
                    "contract":   c,
                    "local_symbol": c.localSymbol,
                })
        except Exception as exc:
            logger.error("get_option_positions error: %s", exc)

        return result

    def get_stock_positions(self) -> list[dict]:
        """
        Restituisce tutte le posizioni azionarie aperte.
        Filtra per secType == 'STK'.
        """
        if not self.is_connected:
            return []

        account = config.IBKR_ACCOUNT or ""
        result = []
        try:
            for pos in self._ib.positions(account=account):
                c = pos.contract
                if c.secType != "STK":
                    continue
                result.append({
                    "ticker":   c.symbol,
                    "position": pos.position,   # numero di azioni (positivo = long)
                    "avg_cost": pos.avgCost,
                    "contract": c,
                })
        except Exception as exc:
            logger.error("get_stock_positions error: %s", exc)

        return result

    # ── PREZZI LIVE ────────────────────────────────────────────────────────────

    def get_live_premium(self, contract) -> Optional[float]:
        """
        Richiede dati di mercato snapshot per un contratto opzione.
        Restituisce il mid price (bid+ask)/2, fallback su last/close.
        Restituisce None in caso di errore o dati non disponibili.
        """
        if not self.is_connected:
            return None
        try:
            # qualify per riempire conId se mancante
            self._ib.qualifyContracts(contract)
            tickers = self._ib.reqTickers(contract)
            if not tickers:
                return None
            tk = tickers[0]

            bid, ask = tk.bid, tk.ask
            if bid and ask and bid > 0 and ask > 0:
                return round((bid + ask) / 2, 4)
            if tk.last and tk.last > 0:
                return round(tk.last, 4)
            if tk.close and tk.close > 0:
                return round(tk.close, 4)
            return None
        except Exception as exc:
            label = getattr(contract, "localSymbol", str(contract))
            logger.warning("get_live_premium(%s): %s", label, exc)
            return None

    def get_stock_price(self, ticker: str) -> Optional[float]:
        """
        Restituisce il prezzo corrente di un'azione via snapshot.
        Mid price se bid/ask disponibili, altrimenti last/close.
        """
        if not self.is_connected:
            return None
        try:
            contract = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            tickers = self._ib.reqTickers(contract)
            if not tickers:
                return None
            tk = tickers[0]

            if tk.bid and tk.ask and tk.bid > 0 and tk.ask > 0:
                return round((tk.bid + tk.ask) / 2, 4)
            if tk.last and tk.last > 0:
                return round(tk.last, 4)
            if tk.close and tk.close > 0:
                return round(tk.close, 4)
            return None
        except Exception as exc:
            logger.warning("get_stock_price(%s): %s", ticker, exc)
            return None

    # ── SINCRONIZZAZIONE WHEEL POSITIONS ──────────────────────────────────────

    def sync_wheel_positions(self) -> SyncResult:
        """
        Pipeline principale:
          1. Legge short calls da IBKR
          2. Trova il ciclo wheel corrispondente nel DB
          3. Richiede premium live, aggiorna DB
          4. Costruisce WheelPosition con dati freschi
          5. Controlla stop loss su ogni stock sottostante

        Restituisce un SyncResult con la lista di WheelPosition aggiornate.
        """
        synced: list[WheelPosition] = []
        stop_orders: list[str] = []
        errors: list[str] = []

        if not self.is_connected:
            errors.append("Non connesso a IBKR")
            return SyncResult(synced, stop_orders, errors)

        # 1. Posizioni opzioni da IBKR (solo short calls per wheel)
        all_opts = self.get_option_positions()
        short_calls = [
            p for p in all_opts
            if p["right"] == "C" and p["position"] < 0
        ]

        if not short_calls:
            logger.debug("sync_wheel_positions: nessuna short call trovata su IBKR")
            return SyncResult(synced, stop_orders, errors)

        # Precarica posizioni stock per lookup entry_price e shares
        stock_map = {p["ticker"]: p for p in self.get_stock_positions()}

        for opt in short_calls:
            ticker  = opt["ticker"]
            strike  = opt["strike"]
            expiry  = opt["expiry"]

            try:
                # 2. Trova ciclo aperto nel DB corrispondente
                db_row = self._find_db_cycle(ticker, strike, expiry)
                if db_row is None:
                    logger.debug(
                        "Nessun ciclo DB per %s $%.2f %s — salto", ticker, strike, expiry
                    )
                    continue

                cycle_id = db_row["id"]

                # 3. Premium live da IBKR
                live_premium = self.get_live_premium(opt["contract"])
                if live_premium is not None:
                    db.update_wheel_premium(cycle_id, live_premium)
                    logger.info(
                        "Premium aggiornato: %s %s $%.2f → $%.4f",
                        ticker, expiry, strike, live_premium,
                    )
                else:
                    logger.warning(
                        "Premium non disponibile per %s %s $%.2f — uso valore DB",
                        ticker, expiry, strike,
                    )
                    live_premium = float(db_row["premium_current"] or 0)

                # 4. entry_price da tabella positions, fallback su strike
                entry_price = self._get_entry_price(ticker, strike)

                # 5. Costruisce WheelPosition
                wp = WheelPosition(
                    cycle_id=cycle_id,
                    ticker=ticker,
                    strike=strike,
                    expiry=_parse_date(expiry),
                    premium_received=float(db_row["premium_received"] or 0),
                    premium_current=live_premium,
                    entry_price=entry_price,
                    roll_count=int(db_row["roll_count"] or 0),
                    phase=db_row["phase"],
                )
                synced.append(wp)

                # 6. Stop loss su stock sottostante
                stock_info = stock_map.get(ticker)
                if stock_info and entry_price > 0:
                    current_price = self.get_stock_price(ticker)
                    if current_price is not None:
                        pnl_pct = (current_price - entry_price) / entry_price
                        if pnl_pct <= -config.STOP_LOSS_PCT:
                            placed = self._execute_stop_loss(
                                ticker=ticker,
                                shares=int(stock_info["position"]),
                                entry_price=entry_price,
                                current_price=current_price,
                                pnl_pct=pnl_pct,
                            )
                            if placed:
                                stop_orders.append(ticker)

            except Exception as exc:
                msg = f"{ticker}: {exc}"
                logger.error("sync_wheel_positions — errore su %s", msg, exc_info=True)
                errors.append(msg)

        logger.info(
            "sync_wheel_positions: %d posizioni sincronizzate, %d stop loss, %d errori",
            len(synced), len(stop_orders), len(errors),
        )
        return SyncResult(synced, stop_orders, errors)

    def _find_db_cycle(self, ticker: str, strike: float, expiry: str):
        """
        Cerca in wheel_cycles un ciclo aperto con ticker+strike+expiry corrispondenti.
        Restituisce la riga DB o None.
        """
        rows = db.get_wheel_cycles_year(ticker)
        for row in rows:
            if row["phase"] == "closed":
                continue
            row_strike = float(row["strike"] or 0)
            if abs(row_strike - strike) > 0.01:
                continue
            if row["expiry"] == expiry:
                return row
        return None

    def _get_entry_price(self, ticker: str, fallback: float) -> float:
        """Legge entry_price dalla tabella positions; ritorna fallback se non trovato."""
        try:
            row = db._conn().execute(
                "SELECT entry_price FROM positions WHERE ticker=?", (ticker,)
            ).fetchone()
            if row and row["entry_price"]:
                return float(row["entry_price"])
        except Exception:
            pass
        return fallback

    # ── STOP LOSS ──────────────────────────────────────────────────────────────

    def _execute_stop_loss(
        self,
        ticker: str,
        shares: int,
        entry_price: float,
        current_price: float,
        pnl_pct: float,
    ) -> bool:
        """
        Emette un ordine SELL STOP sul titolo azionario.
        In modalità DRY_RUN logga senza inviare.
        Il stop price è current_price (esegui subito a mercato-stop).
        """
        logger.warning(
            "STOP LOSS TRIGGERED: %s @ $%.2f (entry $%.2f, P&L %.1f%%)",
            ticker, current_price, entry_price, pnl_pct * 100,
        )

        if config.IBKR_DRY_RUN:
            logger.info(
                "[DRY RUN] Ordine non inviato: SELL %d %s @ STOP $%.2f",
                shares, ticker, current_price,
            )
            return True

        return self.place_stop_order(ticker, shares, current_price)

    def place_stop_order(self, ticker: str, shares: int, stop_price: float) -> bool:
        """
        Invia un ordine SELL STOP su SMART exchange per `shares` azioni di `ticker`.
        Restituisce True se l'ordine è stato accettato da IBKR.
        """
        if not self.is_connected:
            logger.error("place_stop_order: non connesso")
            return False
        if shares <= 0:
            logger.error("place_stop_order(%s): shares=%d non valido", ticker, shares)
            return False

        try:
            contract = Stock(ticker, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            order = StopOrder("SELL", shares, stop_price)
            trade = self._ib.placeOrder(contract, order)
            logger.info(
                "Ordine stop inviato: SELL %d %s @ STOP $%.2f — orderId=%s",
                shares, ticker, stop_price, trade.order.orderId,
            )
            return True
        except Exception as exc:
            logger.error("place_stop_order(%s) fallito: %s", ticker, exc)
            return False

    # ── SYNC LOOP (daemon thread) ──────────────────────────────────────────────

    def run_sync_loop(
        self,
        on_sync: Callable[[SyncResult], None] | None = None,
        interval: int | None = None,
    ) -> None:
        """
        Loop bloccante che esegue sync_wheel_positions ogni `interval` secondi.
        Progettato per girare in un daemon thread.

        Args:
            on_sync:  callback chiamata con SyncResult dopo ogni ciclo
            interval: secondi tra un sync e il successivo (default: IBKR_SYNC_INTERVAL)
        """
        secs = interval if interval is not None else config.IBKR_SYNC_INTERVAL
        logger.info("IBKR sync loop avviato (interval=%ds)", secs)

        while not self._stop_event.is_set():
            if not self.is_connected:
                logger.debug("Sync loop: non connesso, attendo 10s...")
                self._stop_event.wait(10)
                continue

            result = self.sync_wheel_positions()

            if on_sync:
                try:
                    on_sync(result)
                except Exception as exc:
                    logger.error("on_sync callback error: %s", exc)

            self._stop_event.wait(secs)

        logger.info("IBKR sync loop terminato")


# ─────────────────────────────────────────────────────────────────────────────
#  Factory helper
# ─────────────────────────────────────────────────────────────────────────────

def start_ibkr_thread(
    on_sync: Callable[[SyncResult], None] | None = None,
) -> IBKRConnector:
    """
    Crea un IBKRConnector, connette e avvia il sync loop in un daemon thread.
    Restituisce il connector (chiama .disconnect() per fermare).

    Se la connessione iniziale fallisce il connector tenterà il retry in background.
    """
    connector = IBKRConnector()

    if not connector.connect():
        logger.warning(
            "Connessione IBKR iniziale fallita — retry automatico in background attivo"
        )

    t = threading.Thread(
        target=connector.run_sync_loop,
        kwargs={"on_sync": on_sync},
        daemon=True,
        name="ibkr-sync",
    )
    t.start()
    logger.info("IBKR sync thread avviato (daemon=True)")
    return connector


# ─────────────────────────────────────────────────────────────────────────────
#  Utility
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    """Converte stringa YYYY-MM-DD in date. Fallback a today se non parsabile."""
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        logger.warning("_parse_date: formato non valido '%s' — uso today", s)
        return date.today()
