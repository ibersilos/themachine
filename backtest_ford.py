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

import config

warnings.filterwarnings("ignore")

TICKER         = "F"
BACKTEST_YEARS = 5
TARGET_DTE     = 20
# Selezione strike per delta Black-Scholes (banda 0.16-0.30 standard CSP,
# qui fissato al bordo superiore 0.30 — testato anche 0.25, vedi confronto
# nel log skill the-machine-analyst) invece di banda fissa % dello spot.
# ATTENZIONE: questo backtest fissa SEMPRE 0.30, mentre wheel_scanner.py
# (live) sceglie il miglior strike nella banda 0.16-0.30 secondo i filtri di
# premio/rendimento — non e' una replica esatta della logica live, i
# risultati sono un'approssimazione (trovato da code review, 29/08/2026).
TARGET_DELTA   = 0.30
EARLY_CLOSE    = 0.50
DTE_RULE       = 21
MAX_ROLLS      = 2
RISK_FREE      = 0.05
SHARES         = 100
# Fonte unica: config.WHEEL_COMMISSION_PER_ORDER (era duplicata qui come
# stima locale mai verificata $0.70, poi corretta a $1.17 ma isolata da
# wheel_scanner/strategy_advisor/covered_call_optimizer — unificata il
# 29/08/2026 in deep audit per evitare che backtest e moduli live mostrino
# numeri diversi per lo stesso costo reale).
COMMISSION     = config.WHEEL_COMMISSION_PER_ORDER    # $/ordine (1 contratto)
# Bid-ask spread reale (mai gratis, a differenza della commissione IBKR e'
# implicito nel prezzo eseguito). Stima conservativa per 1 contratto su
# un sottostante liquido come F: ~$0.02/azione di mezzo-spread pagato sia
# in apertura che in chiusura di ogni ordine. Applicato per-ordine come la
# commissione, cosi' i cicli a DTE corto (piu' riaperture/anno, premio per
# ciclo piu' piccolo) ne pagano proporzionalmente di piu' — a differenza del
# vecchio haircut piatto "-35%" applicato solo in coda, che non catturava
# questo effetto (bug trovato in verifica avversariale, 29/08/2026).
SPREAD_COST    = 0.02 * SHARES   # $/ordine

# Soglia DTE sotto la quale si tenta un roll del CC (Ford sale verso lo
# strike). Scalata proporzionalmente a TARGET_DTE (era fissa a 7, tarata
# implicitamente solo per TARGET_DTE=20 — bug trovato in verifica
# avversariale 29/08/2026: con TARGET_DTE piu' corto la soglia fissa lasciava
# quasi zero margine di roll, con TARGET_DTE piu' lungo copriva quasi tutto
# il ciclo, rendendo il confronto tra DTE non comparabile).
ROLL_DTE_THRESHOLD = max(2, round(TARGET_DTE * 0.35))


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


def strike_for_delta(S, T, r, sigma, target_delta, right):
    """Inverte la formula del delta Black-Scholes per trovare lo strike che
    produce target_delta (0-1). right: 'call' o 'put'."""
    if T <= 0 or sigma <= 0:
        return S
    d1 = norm.ppf(target_delta) if right == "call" else -norm.ppf(target_delta)
    K = S * math.exp((r + 0.5 * sigma**2) * T - d1 * sigma * math.sqrt(T))
    return K


def main():
    print(f"\nCaricamento dati {TICKER}...")
    hist = yf.Ticker(TICKER).history(period=f"{BACKTEST_YEARS + 1}y")
    if isinstance(hist.columns, pd.MultiIndex):
        hist = hist["Close"].to_frame("Close")
    hist = hist[["Close"]].copy()
    hist.index = pd.to_datetime(hist.index).tz_localize(None)
    # yfinance a volte include una riga finale (sessione odierna/festivo)
    # con Close = NaN, che rendeva NaN Buy&Hold/alpha in coda (bug trovato
    # dallo sweep agent, 29/08/2026).
    hist = hist.dropna(subset=["Close"])

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
            K         = round_ford_strike(strike_for_delta(S, T, RISK_FREE, vol, TARGET_DELTA, "call"))
            prem_open = bs_call(S, K, T, RISK_FREE, vol)
        else:
            K         = round_ford_strike(strike_for_delta(S, T, RISK_FREE, vol, TARGET_DELTA, "put"))
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
                cyc["prem_close"] = round(prem_now, 3)
                cyc["exit"]       = f"Chiusura anticipata (50%) DTE={dte}"
                cyc["pnl"]        = (prem_open - prem_now) * SHARES
                cyc["close_date"] = td_date
                cash += cyc["pnl"]
                closed = True
                cursor_idx = tday_to_pos[td]
                advanced_inline = True
                break

            # Roll CC se stock sale verso strike — controllato PRIMA dello
            # stop meccanico 21-DTE: la difesa via roll deve avere priorita'
            # sulla chiusura a tempo, altrimenti lo stop meccanico intercetta
            # ogni minaccia ITM prima che il roll possa difenderla, per ogni
            # TARGET_DTE in (ROLL_DTE_THRESHOLD, DTE_RULE] — bug trovato in
            # seconda verifica avversariale, 29/08/2026 (la prima versione
            # controllava lo stop meccanico prima del roll).
            rolled_today = False
            if phase == "cc" and S_i > K * 0.97 and dte > ROLL_DTE_THRESHOLD and roll_count < MAX_ROLLS:
                new_T    = (TARGET_DTE + dte) / 365.0
                new_K    = round_ford_strike(strike_for_delta(S_i, new_T, RISK_FREE, vol_i, TARGET_DELTA, "call"))
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
                    rolled_today = True

            # Stop meccanico 21-DTE (Hogue) — indipendente dal profitto,
            # chiude quando restano <=DTE_RULE giorni E il roll di oggi non
            # ha gia' difeso/rinviato la posizione. Reale solo se
            # TARGET_DTE > DTE_RULE: un ciclo aperto gia' a 20 DTE o meno
            # non ha mai 21 giorni residui da raggiungere, quindi la regola
            # semplicemente non si applica (non e' un bug, e' la realta' del
            # meccanismo — prima era solo un'etichetta cosmetica sull'uscita
            # a profitto, senza alcuna chiusura indipendente reale; trovato
            # in verifica avversariale 29/08/2026).
            if not rolled_today and TARGET_DTE > DTE_RULE and dte <= DTE_RULE:
                cyc["prem_close"] = round(prem_now, 3)
                cyc["exit"]       = f"Chiusura meccanica {DTE_RULE}-DTE"
                cyc["pnl"]        = (prem_open - prem_now) * SHARES
                cyc["close_date"] = td_date
                cash += cyc["pnl"]
                closed = True
                cursor_idx = tday_to_pos[td]
                advanced_inline = True
                break

        if not closed:
            cyc["close_date"] = expiry_date
            cyc["exit"]       = "Fine dati"
            cyc["pnl"]        = prem_open * SHARES
            cash += cyc["pnl"]

        # Commissioni: 1 ordine di apertura + 2 per ogni roll (chiudi+riapri)
        # + 1 di buy-to-close se chiusura anticipata. Scadenza OTM e assegnazione
        # non generano un ordine opzioni aggiuntivo (nessun buy-back necessario).
        orders = 1 + 2 * cyc["rolled"]
        if "Chiusura anticipata" in cyc["exit"] or "Chiusura meccanica" in cyc["exit"]:
            orders += 1
        commission = orders * (COMMISSION + SPREAD_COST)
        cyc["commission"] = commission
        cyc["pnl"]       -= commission
        cash             -= commission

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
    n           = len(cycles)
    total_prem  = sum(c["pnl"] for c in cycles)          # netto, commissioni gia' sottratte
    total_comm  = sum(c["commission"] for c in cycles)
    gross_prem  = total_prem + total_comm
    assigned_n = sum(1 for c in cycles if c["assigned"])
    early_n    = sum(1 for c in cycles if "anticipata" in c["exit"] or "meccanica" in c["exit"])
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
    print(f"  Premi lordi totali:   ${gross_prem:.0f}")
    print(f"  Commiss.+spread tot:  ${total_comm:.0f}  (${COMMISSION:.2f} comm + ${SPREAD_COST:.2f} spread /ordine, soglia roll={ROLL_DTE_THRESHOLD}g)")
    print(f"  Premi netti totali:   ${total_prem:.0f}")
    print(f"  Premi netti annui:    ${ann_prem:.0f}/anno")
    print(f"  Rendimento netto:     {ann_pct:+.1f}%/anno sul capitale iniziale")
    print()
    print(f"  Buy & Hold:           ${bh_start:.2f} -> ${bh_end:.2f}")
    print(f"  B&H PnL:              ${bh_pnl:+.0f} ({bh_ret:+.1f}% totale, {bh_ann:+.1f}%/anno)")
    print(f"  Alpha premi vs B&H:   {ann_pct - bh_ann:+.1f}%")
    print()

    # Rendimento totale stimato (premi + variazione azionaria) — supplemento
    # trasparente al "Premi netti annui" sopra, che conta solo la gamba
    # opzione. Per TARGET_DTE>21 lo stop meccanico chiude spesso call ITM
    # "in perdita" sulla sola gamba opzione senza mai accreditare il guadagno
    # non realizzato sul sottostante che il wheel continua di fatto a
    # detenere — questa riga rende visibile se quella perdita apparente e'
    # reale o solo un artefatto della contabilita' per-opzione. Approssimato:
    # assume 100 azioni detenute per l'intero periodo (non traccia i cambi
    # di fase CC/CSP cambio per cambio) — non sostituisce un vero motore a
    # stato azionario, solo un segnale di allarme. Trovato in deep audit
    # 29/08/2026, backlog #10 — mitigazione, non un fix completo.
    stock_change_pnl = (bh_end - bh_start) * SHARES
    total_est_pnl = total_prem + stock_change_pnl
    total_est_ann_pct = (total_est_pnl / initial_capital) / years * 100
    print(f"  -- Rendimento totale stimato (premi + azionario, approssimato) --")
    print(f"  Variazione azionaria: ${stock_change_pnl:+.0f} (100 az. per l'intero periodo, approssimato)")
    print(f"  Totale stimato:       ${total_est_pnl:+.0f} ({total_est_ann_pct:+.1f}%/anno)")
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
    print("  NOTA: commissioni + spread bid-ask per-ordine gia' sottratti sopra.")
    print("  Approssimazioni residue: IV storica (HV20) come proxy dell'IV reale delle")
    print("  opzioni (non e' dato storico di option chain reale); nessun rischio di")
    print("  assegnazione anticipata da dividendo; execution a mid-price teorico BS.")


if __name__ == "__main__":
    main()
