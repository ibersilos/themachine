"""
backtest_ford.py — Backtest Wheel completa su Ford (F)
Partenza: 100 azioni in portafoglio → CC. Se assegnato → CSP. Se CSP assegnato → CC.
"""
import math
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm

warnings.filterwarnings("ignore")

TICKER         = "F"
BACKTEST_YEARS = 5
TARGET_DTE     = 20
OTM_PCT        = 0.03    # strike +3% OTM per CC, -3% per CSP
EARLY_CLOSE    = 0.50
DTE_RULE       = 21
MAX_ROLLS      = 2
RISK_FREE      = 0.05
SHARES         = 100


def bs_call(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def precompute_hv20(prices, window=20):
    """Vettorizzato una volta sola — evita di ricreare Series pandas per ogni giorno."""
    log_ret = np.log(prices / prices.shift(1))
    roll_std = log_ret.rolling(window).std() * np.sqrt(252)
    return roll_std.fillna(0.35).to_numpy()


def round_ford_strike(x):
    """Ford si scambia in incrementi di $0.50."""
    return round(x * 2) / 2


def main():
    print(f"\nCaricamento dati {TICKER}...")
    hist = yf.Ticker(TICKER).history(period=f"{BACKTEST_YEARS + 1}y")
    if isinstance(hist.columns, pd.MultiIndex):
        hist = hist["Close"].to_frame("Close")
    hist = hist[["Close"]].copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)

    end_date   = date.today()
    start_date = date(end_date.year - BACKTEST_YEARS, end_date.month, end_date.day)

    prices    = hist["Close"]
    all_days  = hist.index.tolist()
    tdays     = [d for d in all_days if d.date() >= start_date]

    # Lookup O(1) invece di list.index() O(n) ripetuto per ogni giorno simulato
    # (con centinaia/migliaia di chiamate il costo O(n) diventa O(n^2) complessivo).
    day_to_pos   = {d: i for i, d in enumerate(all_days)}
    tday_to_pos  = {d: i for i, d in enumerate(tdays)}
    hv_arr       = precompute_hv20(prices)

    entry_stock_price = float(prices[prices.index.date >= start_date].iloc[0])
    initial_capital   = entry_stock_price * SHARES
    print(f"Prezzo Ford il {start_date}: ${entry_stock_price:.2f} | Capitale 100az: ${initial_capital:.2f}\n")

    # Stato wheel
    phase = "cc"   # inizio con azioni in mano
    cash  = 0.0    # premi accumulati
    cycles = []
    cursor_idx = 0
    _prev_cursor = -1
    _iter_guard = 0

    while cursor_idx < len(tdays):
        _iter_guard += 1
        if cursor_idx == _prev_cursor:
            # Sicurezza: non dovrebbe più accadere (bug del cursore fisso risolto),
            # ma previene un hang silenzioso se una futura modifica reintroduce il problema.
            print(f"[WARN] cursor_idx non avanza a {cursor_idx} (iter {_iter_guard}) — interrotto per sicurezza")
            break
        _prev_cursor = cursor_idx
        entry_dt   = tdays[cursor_idx]
        entry_date = entry_dt.date()
        full_idx   = day_to_pos[entry_dt]
        S          = float(prices.iloc[full_idx])
        vol        = float(hv_arr[full_idx])
        T          = TARGET_DTE / 365.0

        if phase == "cc":
            K         = round_ford_strike(S * (1 + OTM_PCT))
            prem_open = bs_call(S, K, T, RISK_FREE, vol)
        else:
            K         = round_ford_strike(S * (1 - OTM_PCT))
            prem_open = bs_put(S, K, T, RISK_FREE, vol)

        expiry_date = entry_date + timedelta(days=TARGET_DTE)
        cyc = {
            "entry": entry_date, "expiry": expiry_date,
            "close_date": expiry_date,
            "S": round(S, 2), "K": round(K, 2), "vol": round(vol * 100, 1),
            "prem_open": round(prem_open, 3), "phase": phase.upper(),
            "prem_close": 0.0, "exit": "", "pnl": 0.0,
            "assigned": False, "rolled": 0,
        }

        roll_count      = 0
        closed          = False
        advanced_inline = False   # True se il cursore e' gia' stato avanzato dentro il ramo early-close

        for td in tdays[cursor_idx + 1 :]:
            td_date = td.date()
            fi      = day_to_pos[td]
            S_i     = float(prices.iloc[fi])
            vol_i   = float(hv_arr[fi])
            dte     = (expiry_date - td_date).days

            # Scadenza raggiunta
            if td_date > expiry_date and not closed:
                if phase == "cc":
                    if S_i >= K:
                        cyc["assigned"] = True
                        cyc["exit"]     = f"ASSEGNATO ${K:.1f} (stock ${S_i:.2f})"
                        cyc["pnl"]      = prem_open * SHARES + (K - S) * SHARES
                        cash += cyc["pnl"]
                        phase = "csp"
                    else:
                        cyc["exit"] = "Scaduta OTM — profitto max"
                        cyc["pnl"]  = prem_open * SHARES
                        cash += cyc["pnl"]
                else:
                    if S_i <= K:
                        cyc["assigned"] = True
                        cyc["exit"]     = f"ASSEGNATO CSP ${K:.1f} (stock ${S_i:.2f})"
                        cyc["pnl"]      = prem_open * SHARES - (K - S_i) * SHARES
                        cash += cyc["pnl"]
                        phase = "cc"
                    else:
                        cyc["exit"] = "CSP scaduta OTM — profitto max"
                        cyc["pnl"]  = prem_open * SHARES
                        cash += cyc["pnl"]
                cyc["close_date"] = expiry_date
                closed = True
                break

            # Early close
            T_i      = max(dte / 365.0, 0.001)
            prem_now = bs_call(S_i, K, T_i, RISK_FREE, vol_i) if phase == "cc" \
                       else bs_put(S_i, K, T_i, RISK_FREE, vol_i)
            pct = (prem_open - prem_now) / prem_open if prem_open > 0 else 0

            if pct >= EARLY_CLOSE:
                rule = "21-DTE" if dte <= DTE_RULE else "50%"
                cyc["prem_close"] = round(prem_now, 3)
                cyc["exit"]       = f"Chiusura anticipata ({rule}) DTE={dte}"
                cyc["pnl"]        = (prem_open - prem_now) * SHARES
                cyc["close_date"] = td_date
                cash += cyc["pnl"]
                closed = True
                cursor_idx = tday_to_pos[td]
                advanced_inline = True
                break

            # Roll CC se stock sale verso strike
            if phase == "cc" and S_i > K * 0.97 and dte > 7 and roll_count < MAX_ROLLS:
                new_K    = round_ford_strike(S_i * (1 + OTM_PCT))
                new_T    = (TARGET_DTE + dte) / 365.0
                new_prem = bs_call(S_i, new_K, new_T, RISK_FREE, vol_i)
                net_cr   = new_prem - prem_now
                if net_cr > 0:
                    roll_count += 1
                    cyc["rolled"] += 1
                    cash += net_cr * SHARES
                    K = new_K
                    prem_open = new_prem
                    cyc["K"] = round(K, 2)
                    expiry_date = td_date + timedelta(days=TARGET_DTE)
                    cyc["expiry"] = expiry_date

        if not closed:
            cyc["close_date"] = expiry_date
            cyc["exit"]       = "Fine dati"
            cyc["pnl"]        = prem_open * SHARES
            cash += cyc["pnl"]

        cycles.append(cyc)

        if not advanced_inline:
            close_t = cyc["close_date"]
            nxt_pos = next(
                (i for i in range(cursor_idx + 1, len(tdays)) if tdays[i].date() > close_t),
                None,
            )
            if nxt_pos is None:
                break
            cursor_idx = nxt_pos

    # ── Statistiche ────────────────────────────────────────────────────────────
    n          = len(cycles)
    total_prem = sum(c["pnl"] for c in cycles)
    assigned_n = sum(1 for c in cycles if c["assigned"])
    early_n    = sum(1 for c in cycles if "anticipata" in c["exit"])
    cc_n       = sum(1 for c in cycles if c["phase"] == "CC")
    csp_n      = sum(1 for c in cycles if c["phase"] == "CSP")
    roll_tot   = sum(c["rolled"] for c in cycles)

    first_d = cycles[0]["entry"]
    last_d  = cycles[-1]["close_date"]
    years   = (last_d - first_d).days / 365.0

    bh_start   = float(prices[prices.index.date >= first_d].iloc[0])
    bh_end     = float(prices[prices.index.date <= last_d].iloc[-1])
    bh_pnl     = (bh_end - bh_start) * SHARES
    bh_ret     = (bh_end - bh_start) / bh_start * 100
    bh_ann     = bh_ret / years

    ann_prem   = total_prem / years
    ann_pct    = (total_prem / initial_capital) / years * 100
    avg_pnl    = total_prem / n

    print("=" * 63)
    print(f"  BACKTEST FORD (F) — WHEEL COMPLETA (CC -> CSP -> CC)")
    print("=" * 63)
    print(f"  Periodo:          {first_d} a {last_d} ({years:.1f} anni)")
    print(f"  Cicli totali:     {n}  (CC={cc_n}, CSP={csp_n})")
    print(f"  Early close:      {early_n} ({early_n/n*100:.0f}%)")
    print(f"  Assegnazioni:     {assigned_n} ({assigned_n/n*100:.0f}%)")
    print(f"  Roll eseguiti:    {roll_tot}")
    print()
    print(f"  Premio medio/ciclo:   ${avg_pnl:.1f}")
    print(f"  Premi totali:         ${total_prem:.0f}")
    print(f"  Premi annui:          ${ann_prem:.0f}/anno")
    print(f"  Rendimento premi:     {ann_pct:+.1f}%/anno sul capitale iniziale")
    print()
    print(f"  Buy & Hold:           ${bh_start:.2f} -> ${bh_end:.2f}")
    print(f"  B&H PnL:              ${bh_pnl:+.0f} ({bh_ret:+.1f}% totale, {bh_ann:+.1f}%/anno)")
    print(f"  Alpha premi vs B&H:   {ann_pct - bh_ann:+.1f}%")
    print()

    # Dettaglio cicli
    print(f"  {'Data':<12} {'Fase':<4} {'Stock':>6} {'K':>5} {'Vol%':>5} {'Prem':>5} {'PnL':>7}  Esito")
    print("  " + "-" * 75)
    for c in cycles:
        print(
            f"  {str(c['entry']):<12}"
            f" {c['phase']:<4}"
            f" ${c['S']:>5.2f}"
            f" ${c['K']:>4.1f}"
            f" {c['vol']:>4.0f}%"
            f" ${c['prem_open']:>4.3f}"
            f" ${c['pnl']:>6.1f}"
            f"  {c['exit'][:38]}"
        )

    print()
    print("  NOTA: rendimenti sovrastimati (IV=HV20, no commissioni, riapertura immediata)")
    print(f"  Stima realistica: divide premi del 30-40% -> ~{ann_pct*0.65:+.0f}%/anno")


if __name__ == "__main__":
    main()
