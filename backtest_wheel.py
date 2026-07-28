"""
backtest_wheel.py — Simulazione storica della strategia Wheel/Covered-Call
con regole Hogue (50% early close, 21-DTE, 2 max roll, earnings buffer).

Usa yfinance per prezzi storici + Black-Scholes con HV20 come proxy IV.
Nessuna IV storica reale disponibile da yfinance free — limitazione nota.

Esegui:
    python backtest_wheel.py

Output:
    - Tabella cicli per ogni stock
    - Statistiche aggregate
    - Confronto con buy-and-hold
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

warnings.filterwarnings("ignore")

# ── Parametri backtest ────────────────────────────────────────────────────────
TICKERS        = ["KO", "JPM", "MCD"]
BACKTEST_YEARS = 2
TARGET_DTE     = 35
OTM_PCT        = 0.03          # strike +3% OTM (regime laterale)
EARLY_CLOSE    = 0.50          # chiudi se catturato >=50% del premio
DTE_RULE       = 21            # regola 21-DTE: chiudi se DTE<=21 E catturato>=50%
MAX_ROLLS      = 2
EARNINGS_BUF   = 7             # giorni buffer earnings
RISK_FREE      = 0.05          # tasso risk-free (5% circa yield attuale)
SHARES         = 100           # 1 contratto = 100 azioni

# ── Black-Scholes ─────────────────────────────────────────────────────────────

def bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Prezzo teorico covered call via Black-Scholes."""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Prezzo teorico cash-secured put via Black-Scholes."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def hv20(prices: pd.Series, idx: int, window: int = 20) -> float:
    """HV annualizzata a 20 giorni nel punto idx."""
    if idx < window + 1:
        return 0.25  # fallback 25%
    slice_ = prices.iloc[idx - window - 1 : idx]
    log_ret = np.log(slice_ / slice_.shift(1)).dropna()
    return float(log_ret.std() * np.sqrt(252))


# ── Dati ──────────────────────────────────────────────────────────────────────

def load_data(ticker: str, years: int) -> pd.DataFrame:
    period = f"{years + 1}y"
    hist = yf.Ticker(ticker).history(period=period)
    if isinstance(hist.columns, pd.MultiIndex):
        hist = hist["Close"].to_frame("Close")
    hist = hist[["Close"]].copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    return hist


# ── Ciclo simulato ────────────────────────────────────────────────────────────

@dataclass
class Cycle:
    entry_date:    date
    expiry_date:   date
    entry_price:   float
    strike:        float
    premium_open:  float
    close_date:    date       = field(default=None)
    premium_close: float      = 0.0
    assigned:      bool       = False
    rolled:        int        = 0
    exit_reason:   str        = ""
    pnl:           float      = 0.0


def simulate_ticker(ticker: str, hist: pd.DataFrame, start_date: date) -> list[Cycle]:
    """
    Simula cicli CC sulla storia prezzi dal start_date in poi.
    Ogni ciclo: apri CC a 35 DTE OTM+3%, controlla ogni 5 giorni
    per early close (50%), alla scadenza: assegnato o scade senza valore.
    """
    prices = hist["Close"]
    trading_days = hist.index.tolist()
    # Filtra da start_date
    trading_days = [d for d in trading_days if d.date() >= start_date]

    cycles: list[Cycle] = []
    cursor_idx = 0  # indice nel trading_days filtrato

    all_days = hist.index.tolist()  # tutti i giorni inclusi quelli pre-backtest (per HV)

    while cursor_idx < len(trading_days):
        entry_dt = trading_days[cursor_idx]
        entry_date = entry_dt.date()

        # Indice nel dataset completo per HV
        full_idx = all_days.index(entry_dt)
        S = float(prices.iloc[full_idx])
        vol = hv20(prices, full_idx)

        K = round(S * (1 + OTM_PCT), 0)  # strike +3%
        T = TARGET_DTE / 365.0
        prem_open = bs_call(S, K, T, RISK_FREE, vol)

        expiry_date = entry_date + timedelta(days=TARGET_DTE)

        cyc = Cycle(
            entry_date=entry_date,
            expiry_date=expiry_date,
            entry_price=S,
            strike=K,
            premium_open=round(prem_open, 2),
        )

        # Simula giorno per giorno fino a scadenza
        roll_count = 0
        closed = False
        for td in trading_days[cursor_idx + 1:]:
            td_date = td.date()
            if td_date > expiry_date and not closed:
                # Scaduto: verifica assegnazione
                current_S = float(prices.loc[td])
                if current_S >= K:
                    cyc.assigned = True
                    cyc.exit_reason = f"Assegnato ${K:.0f} (stock ${current_S:.2f})"
                    cyc.close_date = expiry_date
                    # Guadagno = premio + (strike - entry) per la call coperta
                    # In realtà l'azione è venduta a strike
                    cyc.pnl = (cyc.premium_open - cyc.premium_close) * SHARES
                else:
                    cyc.premium_close = 0.0
                    cyc.exit_reason = "Scaduta senza valore (profitto max)"
                    cyc.close_date = expiry_date
                    cyc.pnl = cyc.premium_open * SHARES
                closed = True
                break

            dte = (expiry_date - td_date).days
            full_idx_i = all_days.index(td)
            S_i = float(prices.iloc[full_idx_i])
            vol_i = hv20(prices, full_idx_i)
            T_i = max(dte / 365.0, 0.001)
            prem_now = bs_call(S_i, K, T_i, RISK_FREE, vol_i)
            pct_captured = (prem_open - prem_now) / prem_open if prem_open > 0 else 0

            # Early close 50% rule
            if pct_captured >= EARLY_CLOSE:
                if dte <= DTE_RULE or pct_captured >= EARLY_CLOSE:
                    cyc.premium_close = round(prem_now, 2)
                    rule = "21-DTE" if dte <= DTE_RULE else "50%"
                    cyc.exit_reason = f"Chiusura anticipata ({rule}) DTE={dte}"
                    cyc.close_date = td_date
                    cyc.pnl = (prem_open - prem_now) * SHARES
                    closed = True
                    # Riapri ciclo subito (cursore = giorno attuale)
                    cursor_idx = trading_days.index(td)
                    break

            # Roll check: se stock > strike * 0.97 e DTE > 7
            if S_i > K * 0.97 and dte > 7 and roll_count < MAX_ROLLS:
                # Controlla se roll a credito netto
                new_K = round(S_i * (1 + OTM_PCT), 0)
                new_T = (TARGET_DTE + dte) / 365.0  # roll a +35 DTE
                new_prem = bs_call(S_i, new_K, new_T, RISK_FREE, vol_i)
                net_credit = new_prem - prem_now
                if net_credit > 0:
                    roll_count += 1
                    # Aggiorna posizione al nuovo ciclo
                    K = new_K
                    prem_open = new_prem
                    expiry_date = td_date + timedelta(days=TARGET_DTE)
                    cyc.expiry_date = expiry_date
                    cyc.strike = K
                    cyc.premium_open = round(prem_open, 2)
                    cyc.rolled += 1

        if not closed:
            cyc.close_date = expiry_date
            cyc.exit_reason = "Scaduta (fine dati)"
            cyc.pnl = cyc.premium_open * SHARES

        cycles.append(cyc)

        # Avanza cursore al giorno dopo la chiusura (se non già aggiornato da early close)
        if not closed or cyc.exit_reason.startswith("Scad") or cyc.assigned:
            close_target = cyc.close_date
            # trova il primo trading day >= close_target + 1
            next_days = [d for d in trading_days if d.date() > close_target]
            if not next_days:
                break
            cursor_idx = trading_days.index(next_days[0])

    return cycles


# ── Statistiche ───────────────────────────────────────────────────────────────

def stats(ticker: str, cycles: list[Cycle], hist: pd.DataFrame, start_date: date):
    if not cycles:
        print(f"\n{ticker}: nessun ciclo simulato")
        return

    total_pnl      = sum(c.pnl for c in cycles)
    n              = len(cycles)
    assigned_n     = sum(1 for c in cycles if c.assigned)
    early_close_n  = sum(1 for c in cycles if "anticipata" in c.exit_reason)
    roll_n         = sum(c.rolled for c in cycles)
    avg_pnl        = total_pnl / n
    win_rate       = sum(1 for c in cycles if c.pnl > 0) / n * 100

    # Durata effettiva backtest
    first_date = cycles[0].entry_date
    last_date  = cycles[-1].close_date
    years      = (last_date - first_date).days / 365.0

    # Stima capitale impiegato
    avg_entry  = np.mean([c.entry_price for c in cycles])
    capital    = avg_entry * SHARES

    annual_pnl = (total_pnl / years) if years > 0 else 0
    annual_ret = annual_pnl / capital * 100

    # Buy & hold
    prices = hist["Close"]
    bh_start = prices[prices.index.date >= first_date].iloc[0]
    bh_end   = prices[prices.index.date <= last_date].iloc[-1]
    bh_ret   = (bh_end - bh_start) / bh_start * 100
    bh_ann   = bh_ret / years if years > 0 else 0

    print(
        f"\n{'='*60}\n"
        f"  BACKTEST RESULTS: {ticker}\n"
        f"{'='*60}\n"
        f"  Periodo:                {first_date} to {last_date} ({years:.1f} anni)\n"
        f"  Cicli totali:           {n}\n"
        f"    Chiusura anticipata:  {early_close_n} ({early_close_n/n*100:.0f}%)\n"
        f"    Assegnazioni:         {assigned_n} ({assigned_n/n*100:.0f}%)\n"
        f"    Roll eseguiti:        {roll_n}\n"
        f"\n"
        f"  PnL totale premi:  ${total_pnl:>10,.0f}\n"
        f"  PnL medio/ciclo:   ${avg_pnl:>10,.0f}\n"
        f"  Win rate:          {win_rate:.0f}%\n"
        f"\n"
        f"  Rendimento annuo CC:    {annual_ret:+.1f}%\n"
        f"  Buy & Hold annuo:       {bh_ann:+.1f}%\n"
        f"  Alpha stimato:          {annual_ret - bh_ann:+.1f}%\n"
        f"\n"
        f"  Capitale medio:    ${capital:>10,.0f}\n"
    )

    # Tabella ultimi 10 cicli
    print(f"  Ultimi cicli ({ticker}):")
    print(f"  {'Entry':<12} {'Strike':>7} {'Prem':>6} {'Close%':>7} {'PnL':>8}  Motivo")
    print("  " + "-" * 65)
    for c in cycles[-10:]:
        close_pct = (c.premium_open - c.premium_close) / c.premium_open * 100 if c.premium_open > 0 else 100
        print(
            f"  {c.entry_date.strftime('%Y-%m-%d'):<12}"
            f"  ${c.strike:>5.0f}"
            f"  ${c.premium_open:>5.2f}"
            f"  {close_pct:>5.0f}%"
            f"  ${c.pnl:>7,.0f}"
            f"  {c.exit_reason[:35]}"
        )
    print()
    return {
        "ticker": ticker,
        "cycles": n,
        "win_rate": win_rate,
        "annual_ret_cc": annual_ret,
        "annual_ret_bh": bh_ann,
        "alpha": annual_ret - bh_ann,
        "assignment_rate": assigned_n / n * 100,
        "early_close_rate": early_close_n / n * 100,
    }


# ── Analisi critica ───────────────────────────────────────────────────────────

CRITICAL_ANALYSIS = """
======================================================================
         ANALISI CRITICA -- SENIOR COVERED CALL EXPERT
======================================================================

--- PUNTI DI FORZA ---------------------------------------------------

1. REGOLA 50% EARLY CLOSE — CORRETTAMENTE IMPLEMENTATA
   La chiusura al 50% del premio è il cuore del framework Hogue.
   Catturare il 50% in ≈1/3 del tempo (grazie al theta decay accelerato)
   permette 15-16 cicli/anno invece dei classici 12. L'implementazione
   è corretta: check_early_close() valuta pct_captured vs threshold.

2. REGOLA 21-DTE — OK, MA DA RIVEDERE (vedi criticità)
   Chiudere se DTE≤21 E catturato≥50% è sensato per evitare il gamma risk
   nell'ultima settimana. L'implementazione è corretta.

3. EARNINGS BUFFER 7 GG — FONDAMENTALE, BEN GESTITO
   Bloccare la vendita di calls 7 giorni prima degli earnings è la regola
   più importante dopo l'early close. Le mosse post-earnings sono imprevedibili.

4. ROLL CON CREDITO NETTO — LOGICA CORRETTA
   check_roll_opportunity() valuta il credito netto prima di rollare.
   Un roll a debito è sempre meglio lasciare assegnare.

--- CRITICITA' SERIE -------------------------------------------------

❌ 1. OTM% FISSO PER REGIME — ERRORE FONDAMENTALE
   Regime "bearish" → strike OTM +1%, delta 0.40.
   In un mercato ribassista un delta 0.40 è ALTAMENTE rischioso per una CC.
   Una call delta 0.40 è quasi ATM. Se il mercato si gira al rialzo (bear trap)
   sei quasi sicuro di essere assegnato. In regime bearish dovresti:
   → NON vendere calls (skip ciclo) o usare strike molto OTM (delta ≤0.15)
   FIX: in regime bearish imposta otm_pct=0.08, delta=0.15 oppure blocca il ciclo.

❌ 2. IV RANK PROXY INACCURATO
   _calculate_iv_rank() usa la volatilità storica (HV) come proxy per l'IV rank.
   HV ≠ IV. Il VRP (IV/HV) è positivo per definizione la maggior parte del tempo
   (il mercato prezza sempre una risk premium), quindi l'IV sarà quasi sempre
   > HV storica calcolata. Questo può far scattare il blocco HOGUE_MIN_IV_RANK
   su stock con buona IV reale.
   FIX: usa i dati IV della catena opzioni (atm_iv già disponibile) come benchmark
   invece di confrontare con l'HV storica.

❌ 3. WHEEL_ANN_RETURN_MIN=15% SULLA PUT — TROPPO OTTIMISTA
   Un annualized return del 15% su una CSP OTM su large cap è molto difficile
   da trovare sistematicamente. Su KO/MCD questo filtra quasi tutto.
   Il range realistico per large cap stabili è 8-12% annualizzato.
   FIX: WHEEL_ANN_RETURN_MIN=8.0 per large cap, 12-15% per mid cap più volatili.

❌ 4. ROLL TRIGGER A -3% DAL STRIKE — TROPPO AGGRESSIVO
   HOGUE_ROLL_TRIGGER_PCT=0.97 significa: rollare se stock > strike * 0.97.
   Se sei venduto su una CC a $100 strike, inizi a valutare il roll già a $97.
   Questo ti fa rollare troppo presto, spesso pagando theta ancora elevato
   sulla call vecchia. Il roll ottimale è quando lo stock supera il STRIKE, non -3%.
   FIX: HOGUE_ROLL_TRIGGER_PCT=1.00 (triggera quando ITM) oppure usare delta > 0.70.

❌ 5. MARRIED PUT AL -10% — COSTO TROPPO ELEVATO
   Su large cap a bassa volatilità (KO: vol ~12-15%), una put -10% a 45 DTE
   costa ≈$0.50-0.80 su stock $60 = 1.3% del capitale.
   Con 16 cicli/anno questo è 1.3% * (45/365) * cicli = significativo.
   La Married Put ha senso solo sui PICK, non sulla WHEEL (il collateral è cash).
   FIX: rimuovi Married Put automatica dai wheel candidate; usala solo sui PICK
   (stock picking direzionale dove il rischio di ribasso è più alto).

❌ 6. IRON CONDOR A IV RANK >80% — STRUTTURA INAPPROPRIATA PER WHEEL
   L'Iron Condor sostituisce la CC quando IV Rank > 80%, ma questo crea
   un cambio di strategia non comunicato e non tracciato in DB.
   Un IV Rank > 80% di solito significa evento imminente (earnings, macro).
   In quel caso il blocco earnings dovrebbe già fermarti.
   FIX: se IV Rank > 80%, prima verifica earnings. Se ok, vendi la CC con strike
   ancora più OTM (+7%), non passare all'Iron Condor che è più complesso da gestire.

⚠️  7. REGOLA 21-DTE APPLICATA INDIPENDENTEMENTE DAL PREZZO CORRENTE
   Il codice chiude se DTE≤21 E pct_captured≥50%, ma non controlla
   se lo stock è vicino allo strike. Se DTE=20 e stock è -20% dallo strike,
   tenere la posizione fino a scadenza raccoglierebbe il theta restante.
   FIX: aggiungi moneyness check → se strike > stock_price * 1.05 (profonda OTM),
   NON chiudere per la regola 21-DTE e lascia scadere senza valore.

--- RACCOMANDAZIONI DI OTTIMIZZAZIONE --------------------------------

1. ABBASSA WHEEL_ANN_RETURN_MIN → 8.0% per large cap
2. CORREGGI regime bearish: otm_pct=0.08, delta=0.15 (non +1%)
3. HOGUE_ROLL_TRIGGER_PCT=1.00 (solo se ITM, non -3%)
4. Aggiungi moneyness check alla regola 21-DTE
5. Rimuovi Married Put automatica dai WHEEL candidate
6. Valuta VRP minimo su segnali WHEEL → VRP > 1.2 (alzalo da 1.1)
7. Aggiungi tracking del "ciclo wheel" nel DB con fase: CSP → CC → close

--- LIMITAZIONI DEL BACKTEST -----------------------------------------

• IV usata = HV20 storica (non IV reale) → sottostima il premio in periodi di stress
• Nessun modello di slippage o commissioni (realisticamente -$1-2/contratto)
• Nessuna simulazione dell'assegnazione delle azioni e successivo ciclo CSP
• Earnings non simulati su dati storici reali
• I prezzi sono close giornalieri, non intraday → timing non perfetto

Per un backtest professionale usare CBOE historical IV data (a pagamento)
o OptionMetrics. Questo backtest è orientativo, non predittivo.
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 65)
    print("  THE-MACHINE — WHEEL STRATEGY BACKTEST")
    print("  Framework Hogue | 2 anni | 3 titoli | Black-Scholes (HV proxy)")
    print("=" * 65)

    end_date   = date.today()
    start_date = date(end_date.year - BACKTEST_YEARS, end_date.month, end_date.day)

    all_stats = []
    for ticker in TICKERS:
        print(f"\n  Scaricando dati {ticker}...")
        hist = load_data(ticker, BACKTEST_YEARS + 1)
        print(f"  Simulando cicli da {start_date}...")
        cycles = simulate_ticker(ticker, hist, start_date)
        s = stats(ticker, cycles, hist, start_date)
        if s:
            all_stats.append(s)

    # Riepilogo
    if all_stats:
        print("\n" + "=" * 65)
        print("  RIEPILOGO COMPARATIVO")
        print("=" * 65)
        print(f"  {'Ticker':<8} {'Cicli':>6} {'WinRate':>8} {'CC Ann%':>8} {'B&H Ann%':>9} {'Alpha':>7} {'Assign%':>8}")
        print("  " + "-" * 60)
        for s in all_stats:
            print(
                f"  {s['ticker']:<8}"
                f"  {s['cycles']:>5}"
                f"  {s['win_rate']:>7.0f}%"
                f"  {s['annual_ret_cc']:>7.1f}%"
                f"  {s['annual_ret_bh']:>8.1f}%"
                f"  {s['alpha']:>6.1f}%"
                f"  {s['assignment_rate']:>7.0f}%"
            )

    print(CRITICAL_ANALYSIS)


if __name__ == "__main__":
    main()
