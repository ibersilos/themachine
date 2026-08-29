"""
backtest_forex_strategies.py — Test di 5 strategie forex (fonte:
forextester.com/blog/forex-trading-strategies-explained/, "40 Forex Trading
Strategies Explained in 2026") sulle 4 coppie principali.

Selezionate tra 40 per criteri oggettivi: solo indicatori standard
implementabili con precisione (niente Alligator/Frattali/TDI/ZigZag/Murray/
Ozymandias, che richiederebbero una ricostruzione non verificabile), e
meccaniche distinte tra loro (non ripetizioni di Stocastico+RSI+EMA, gia'
testato e scartato il 29/08/2026 — vedi log skill the-machine-analyst):

  1. Holy Grail        — EMA(20) + ADX(14), pullback in trend forte
  2. Double MACD        — MACD doppio timeframe + SMA(60), esplicitamente
                          per EUR/USD, GBP/USD, USD/JPY nella fonte
  3. 4UJ                — SMA(48) + ATR come filtro volatilita', esplicitamente
                          per USD/JPY nella fonte
  4. Parabolic SAR+MACD — trend-following con stop-and-reverse
  5. Two Groups of SMA  — puro allineamento di medie mobili, zero oscillatori

NOTA ONESTA: la fonte descrive le regole in modo informale (tipico di
strategie da forum MT4), senza SL/TP precisi in alcuni casi. Dove il testo
originale specifica un valore, e' usato quello; altrimenti e' documentata
qui l'interpretazione scelta — non e' una replica esatta 1:1, e' la
migliore interpretazione meccanica fedele possibile.

Costi: spread realistico in pips (round-turn) sottratto ad ogni trade —
1.5 pips per le coppie EUR/GBP/CHF, 1.5 pips (in unita' JPY, 0.01) per
USD/JPY — stima conservativa per un conto retail su majors, non un dato
misurato (nessuno storico bid/ask reale disponibile via yfinance).
"""
import warnings
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

PAIRS = {
    "EURUSD=X": {"pip": 0.0001, "label": "EUR/USD"},
    "GBPUSD=X": {"pip": 0.0001, "label": "GBP/USD"},
    "USDJPY=X": {"pip": 0.01,   "label": "USD/JPY"},
    "USDCHF=X": {"pip": 0.0001, "label": "USD/CHF"},
}
YEARS = 5
SPREAD_PIPS = 1.5  # round-turn, stima — vedi docstring


# ── Indicatori ────────────────────────────────────────────────────────────────

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def sma(s, n):
    return s.rolling(n).mean()


def atr(df, n=14):
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def adx(df, n=14):
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = atr(df, 1) * 1  # true range grezzo (n=1 = TR non smussato)
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr_raw = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_n = tr_raw.rolling(n).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(n).mean() / atr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(n).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(n).mean()


def macd(s, fast, slow, signal):
    macd_line = ema(s, fast) - ema(s, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def parabolic_sar(df, af_step=0.02, af_max=0.2):
    """Implementazione standard iterativa (non vettorizzabile per natura)."""
    high, low = df["High"].values, df["Low"].values
    n = len(df)
    sar = np.zeros(n)
    trend_up = True
    af = af_step
    ep = high[0]
    sar[0] = low[0]
    for i in range(1, n):
        prev_sar = sar[i - 1]
        if trend_up:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i - 1], low[max(i - 2, 0)])
            if high[i] > ep:
                ep = high[i]
                af = min(af + af_step, af_max)
            if low[i] < sar[i]:
                trend_up = False
                sar[i] = ep
                ep = low[i]
                af = af_step
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i - 1], high[max(i - 2, 0)])
            if low[i] < ep:
                ep = low[i]
                af = min(af + af_step, af_max)
            if high[i] > sar[i]:
                trend_up = True
                sar[i] = ep
                ep = high[i]
                af = af_step
    return pd.Series(sar, index=df.index)


# ── Motore di backtest generico (una posizione alla volta) ────────────────────

def run_backtest(df, signals, sl_pips, tp_pips, pip, exit_signals=None, label=""):
    """
    signals: Series di +1 (buy), -1 (sell), 0 (nessun segnale) allineata a df.
    exit_signals: Series opzionale di True dove chiudere per regola indicatore
                  (oltre a SL/TP). Se None, esce solo per SL/TP o segnale opposto.
    Ritorna lista di trade dict.
    """
    trades = []
    position = 0       # 0, 1 (long), -1 (short)
    entry_price = None
    entry_idx = None
    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    idx = df.index

    for i in range(1, len(df)):
        if position != 0:
            sl_price = entry_price - position * sl_pips * pip
            tp_price = entry_price + position * tp_pips * pip
            hit_sl = (position == 1 and lows[i] <= sl_price) or (position == -1 and highs[i] >= sl_price)
            hit_tp = (position == 1 and highs[i] >= tp_price) or (position == -1 and lows[i] <= tp_price)
            ind_exit = exit_signals is not None and bool(exit_signals.iloc[i])
            opp_signal = signals.iloc[i] == -position

            if hit_sl or hit_tp or ind_exit or opp_signal:
                exit_price = sl_price if hit_sl else (tp_price if hit_tp else closes[i])
                pnl_pips = (exit_price - entry_price) / pip * position - SPREAD_PIPS
                trades.append({
                    "entry_date": idx[entry_idx], "exit_date": idx[i],
                    "direction": "LONG" if position == 1 else "SHORT",
                    "entry": entry_price, "exit": exit_price,
                    "pnl_pips": pnl_pips,
                    "exit_reason": "SL" if hit_sl else ("TP" if hit_tp else ("IND" if ind_exit else "OPP")),
                })
                position = 0
                entry_price = None
                # Re-entry nello stesso bar se il segnale opposto lo richiede
                if opp_signal and signals.iloc[i] != 0:
                    position = signals.iloc[i]
                    entry_price = closes[i]
                    entry_idx = i
                continue

        if position == 0 and signals.iloc[i] != 0:
            position = signals.iloc[i]
            entry_price = closes[i]
            entry_idx = i

    return trades


def summarize(trades, years, label):
    if not trades:
        return {"label": label, "n": 0}
    pnl = [t["pnl_pips"] for t in trades]
    n = len(pnl)
    wins = sum(1 for p in pnl if p > 0)
    total_pips = sum(pnl)
    avg_pips = total_pips / n
    win_rate = wins / n * 100
    ann_pips = total_pips / years
    return {
        "label": label, "n": n, "win_rate": win_rate,
        "total_pips": total_pips, "avg_pips": avg_pips, "ann_pips": ann_pips,
    }


# ── Strategie ───────────────────────────────────────────────────────────────

def strat_holy_grail(df):
    e20 = ema(df["Close"], 20)
    adx14 = adx(df, 14)
    trend_up = e20.diff(5) > 0
    trend_dn = e20.diff(5) < 0
    strong = adx14 > 30
    dip_below = (df["Close"].shift(1) < e20.shift(1)) & (df["Close"] > e20)
    dip_above = (df["Close"].shift(1) > e20.shift(1)) & (df["Close"] < e20)

    buy = trend_up & strong & dip_below
    sell = trend_dn & strong & dip_above
    signals = pd.Series(0, index=df.index)
    signals[buy] = 1
    signals[sell] = -1

    adx_falling = adx14 < adx14.shift(1)
    exit_sig = adx_falling & (adx14.shift(1) > 30)
    return signals, exit_sig, 40, 100  # SL/TP non specificati nel testo — backstop di rischio ragionevole


def strat_double_macd(df):
    ml_s, sl_s, _ = macd(df["Close"], 30, 60, 30)
    ml_j, sl_j, hist_j = macd(df["Close"], 6, 12, 5)
    sma60 = sma(df["Close"], 60)

    above = df["Close"] > sma60
    below = df["Close"] < sma60
    buy = above & (ml_s > 0) & (ml_j > 0)
    sell = below & (ml_s < 0) & (ml_j < 0)
    signals = pd.Series(0, index=df.index)
    signals[buy] = 1
    signals[sell] = -1

    # Uscita = inversione MACD senior CON conferma dell'istogramma junior —
    # prima mancava del tutto la conferma (usava solo il senior, molto piu'
    # rumoroso da solo) e mancava lo stop forzato su rottura SMA(60), entrambi
    # richiesti esplicitamente dal testo originale. Bug trovato in review
    # avversariale 30/08/2026 — gonfiava i trade (troppo rumore) e toglieva
    # l'unico vero freno del sistema.
    senior_turn_dn = (ml_s < ml_s.shift(1)) & (ml_s.shift(1) >= ml_s.shift(2))
    senior_turn_up = (ml_s > ml_s.shift(1)) & (ml_s.shift(1) <= ml_s.shift(2))
    junior_confirms_dn = hist_j < hist_j.shift(1)
    junior_confirms_up = hist_j > hist_j.shift(1)
    macd_exit = (senior_turn_dn & junior_confirms_dn) | (senior_turn_up & junior_confirms_up)
    sma60_break = (above != above.shift(1))  # chiusura sopra/sotto SMA(60) cambiata rispetto al bar precedente
    exit_sig = macd_exit | sma60_break
    return signals, exit_sig, 60, 150  # SL "almeno 50-70" -> 60; TP non specificato, trend-following


def strat_4uj(df):
    s48 = sma(df["Close"], 48)
    atr7 = atr(df, 7)
    atr_ma = sma(atr7, 30)

    above = df["Close"] > s48
    below = df["Close"] < s48
    atr_up = (atr7 > atr_ma) & (atr7.shift(1) <= atr_ma.shift(1))
    atr_dn = (atr7 < atr_ma) & (atr7.shift(1) >= atr_ma.shift(1))

    buy = above & atr_up
    sell = below & atr_dn
    signals = pd.Series(0, index=df.index)
    signals[buy] = 1
    signals[sell] = -1

    cross_back = ((df["Close"] < s48) & (df["Close"].shift(1) >= s48.shift(1))) | \
                 ((df["Close"] > s48) & (df["Close"].shift(1) <= s48.shift(1)))
    return signals, cross_back, 25, 110  # SL 25 min, TP 100-120 -> 110 (testo originale, per USD/JPY)


def strat_psar_macd(df):
    sar = parabolic_sar(df)
    ml, sl_line, hist = macd(df["Close"], 12, 26, 9)

    hist_up = (hist > 0) & (hist.shift(1) <= 0)
    hist_dn = (hist < 0) & (hist.shift(1) >= 0)
    sar_below = sar < df["Close"]
    sar_above = sar > df["Close"]

    # "Gap up/down sul PSAR" nel testo originale significa un flip appena
    # avvenuto, non lo stato statico "SAR sotto/sopra il prezzo" — che resta
    # vero per tutta la durata di un trend gia' consolidato. La versione
    # precedente usava lo stato statico, facendo scattare l'entrata su
    # qualunque incrocio a zero del MACD dentro un trend gia' avviato, non
    # solo al vero ribaltamento — spiegava l'uniformita' sospetta dei
    # risultati su tutte e 4 le coppie. Bug trovato in review avversariale
    # 30/08/2026. Ora richiede un flip SAR avvenuto entro le ultime 5 barre.
    sar_flip = (sar_below != sar_below.shift(1))
    # Flip avvenuto nelle ultime 5 barre: dato che lo stato sar_below/above si
    # alterna ad ogni flip, "flip recente" + "stato attuale sotto/sopra"
    # identifica correttamente la direzione del flip nella quasi totalita'
    # dei casi (un secondo flip entro 5 barre sarebbe comunque coerente col
    # nuovo stato attuale).
    recent_flip = sar_flip.rolling(5, min_periods=1).max().astype(bool)

    buy = hist_up & sar_below & recent_flip
    sell = hist_dn & sar_above & recent_flip
    signals = pd.Series(0, index=df.index)
    signals[buy] = 1
    signals[sell] = -1

    return signals, sar_flip, 20, 60  # SL "5-30" -> 20 medio; TP non specificato, uso 3x SL


def strat_two_sma_groups(df):
    fast_periods = [3, 5, 8, 10, 12, 15]
    slow_periods = [30, 35, 40, 45, 50, 60]
    fast_mas = [sma(df["Close"], p) for p in fast_periods]
    slow_mas = [sma(df["Close"], p) for p in slow_periods]

    def ordered_up(mas):
        ok = pd.Series(True, index=df.index)
        for a, b in zip(mas[:-1], mas[1:]):
            ok &= (a > b)
        return ok

    def ordered_down(mas):
        ok = pd.Series(True, index=df.index)
        for a, b in zip(mas[:-1], mas[1:]):
            ok &= (a < b)
        return ok

    fast_up = ordered_up(fast_mas)
    slow_up = ordered_up(slow_mas)
    fast_dn = ordered_down(fast_mas)
    slow_dn = ordered_down(slow_mas)

    buy = fast_up & slow_up & ~(fast_up.shift(1).fillna(False) & slow_up.shift(1).fillna(False))
    sell = fast_dn & slow_dn & ~(fast_dn.shift(1).fillna(False) & slow_dn.shift(1).fillna(False))
    signals = pd.Series(0, index=df.index)
    signals[buy] = 1
    signals[sell] = -1

    fast_cross_back = (fast_up != fast_up.shift(1)) | (fast_dn != fast_dn.shift(1))
    return signals, fast_cross_back, 40, 999999  # SL 30-70 -> 40; nessun TP fisso nel testo, solo trailing/exit a incrocio


STRATEGIES = {
    "Holy Grail":        strat_holy_grail,
    "Double MACD":       strat_double_macd,
    "4UJ":               strat_4uj,
    "PSAR+MACD":         strat_psar_macd,
    "Two SMA Groups":    strat_two_sma_groups,
}


def main():
    results = []
    for ticker, meta in PAIRS.items():
        print(f"\nCaricamento {meta['label']}...")
        hist = yf.Ticker(ticker).history(period=f"{YEARS + 1}y", interval="1d")
        hist = hist.dropna(subset=["Close"])
        end_date = date.today()
        start_date = date(end_date.year - YEARS, end_date.month, end_date.day)
        df = hist[hist.index.date >= start_date].copy()
        if len(df) < 100:
            print(f"  Dati insufficienti per {meta['label']}, salto.")
            continue

        pip = meta["pip"]
        bh_start = float(df["Close"].iloc[0])
        bh_end = float(df["Close"].iloc[-1])
        bh_pips = (bh_end - bh_start) / pip

        for strat_name, strat_fn in STRATEGIES.items():
            signals, exit_sig, sl, tp = strat_fn(df)
            trades = run_backtest(df, signals, sl, tp, pip, exit_sig, label=strat_name)
            summary = summarize(trades, YEARS, f"{strat_name} / {meta['label']}")
            summary["pair"] = meta["label"]
            summary["strategy"] = strat_name
            summary["bh_pips"] = bh_pips
            results.append(summary)
            n = summary.get("n", 0)
            if n > 0:
                print(
                    f"  {strat_name:16s} {meta['label']:8s}  n={n:4d}  "
                    f"win={summary['win_rate']:5.1f}%  "
                    f"pips/anno={summary['ann_pips']:+8.1f}  "
                    f"(B&H: {bh_pips:+.0f} pips totali)"
                )
            else:
                print(f"  {strat_name:16s} {meta['label']:8s}  n=0 (nessun trade generato)")

    print("\n" + "=" * 90)
    print("RIEPILOGO — pips/anno netti (spread incluso, no leva/size — solo direzione ed edge)")
    print("=" * 90)
    df_res = pd.DataFrame(results)
    if not df_res.empty:
        pivot = df_res.pivot_table(index="strategy", columns="pair", values="ann_pips")
        print(pivot.round(1).to_string())
        print("\nNumero trade per combinazione:")
        pivot_n = df_res.pivot_table(index="strategy", columns="pair", values="n", aggfunc="sum")
        print(pivot_n.fillna(0).astype(int).to_string())


if __name__ == "__main__":
    main()
