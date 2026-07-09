"""
drive_export.py — Export locale su file system (no dipendenze Google Cloud).

Struttura cartelle:
  exports/
    logs/    → segnali giornalieri  (the-machine-signals-YYYY-MM-DD.csv)
    reports/ → report wheel mensili (wheel-report-YYYY-MM.csv)
    tax/     → riepilogo PnL anno   (pnl-YYYY.csv)

Funzioni pubbliche:
  export_daily_signals(signals)       → salva in exports/logs/
  export_wheel_report(cycles)         → salva in exports/reports/
  export_tax_summary(cycles, year)    → salva in exports/tax/
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

EXPORTS_DIR = Path("exports")
LOGS_DIR    = EXPORTS_DIR / "logs"
REPORTS_DIR = EXPORTS_DIR / "reports"
TAX_DIR     = EXPORTS_DIR / "tax"


def _ensure_dirs() -> None:
    for d in (LOGS_DIR, REPORTS_DIR, TAX_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Salvato: %s (%d righe)", path, len(rows))
    return path


# ── SEGNALI GIORNALIERI ───────────────────────────────────────────────────────

def export_daily_signals(signals: list[dict]) -> str | None:
    """
    Salva i segnali del giorno in exports/logs/the-machine-signals-YYYY-MM-DD.csv.
    Chiamata da main.py a fine giornata UTC.
    Restituisce il path assoluto del file o None se non ci sono segnali.
    """
    if not signals:
        logger.info("Nessun segnale da esportare oggi")
        return None

    _ensure_dirs()
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = LOGS_DIR / f"the-machine-signals-{today}.csv"

    fieldnames = [
        "timestamp", "source", "ticker", "score", "tier",
        "flags", "filing_url", "award_amount",
        "insider_name", "transaction_value_usd",
        "serenity_confidence", "pe_ratio",
    ]

    rows = []
    for sig in signals:
        row = {k: sig.get(k, "") for k in fieldnames}
        if isinstance(row.get("flags"), list):
            row["flags"] = "; ".join(row["flags"])
        rows.append(row)

    _write_csv(out_path, fieldnames, rows)
    return str(out_path.resolve())


# ── REPORT WHEEL MENSILE ──────────────────────────────────────────────────────

def export_wheel_report(cycles: list[dict], month: str | None = None) -> str | None:
    """
    Salva il report cicli wheel in exports/reports/wheel-report-YYYY-MM.csv.
    `cycles` è una lista di dict con campi dal DB wheel_cycles.
    """
    if not cycles:
        return None

    _ensure_dirs()
    month    = month or datetime.now(timezone.utc).strftime("%Y-%m")
    out_path = REPORTS_DIR / f"wheel-report-{month}.csv"

    fieldnames = [
        "id", "ticker", "year", "cycle_number", "phase",
        "strike", "expiry", "premium_received", "premium_current",
        "roll_count", "opened_at", "closed_at", "pnl_realized",
    ]

    _write_csv(out_path, fieldnames, [dict(c) for c in cycles])
    return str(out_path.resolve())


# ── RIEPILOGO TAX / PNL ANNUO ─────────────────────────────────────────────────

def export_tax_summary(cycles: list[dict], year: int | None = None) -> str | None:
    """
    Salva il riepilogo PnL annuo in exports/tax/pnl-YYYY.csv.
    Calcola per ogni ticker: premi totali ricevuti, PnL realizzato, n° cicli.
    """
    if not cycles:
        return None

    _ensure_dirs()
    year     = year or datetime.now(timezone.utc).year
    out_path = TAX_DIR / f"pnl-{year}.csv"

    # Aggrega per ticker
    summary: dict[str, dict] = {}
    for c in cycles:
        ticker = c.get("ticker", "UNKNOWN")
        if ticker not in summary:
            summary[ticker] = {
                "ticker":            ticker,
                "year":              year,
                "cycles_closed":     0,
                "premium_total":     0.0,
                "pnl_realized_total":0.0,
                "avg_pnl_per_cycle": 0.0,
            }
        if c.get("phase") == "closed":
            summary[ticker]["cycles_closed"]      += 1
            summary[ticker]["premium_total"]       += float(c.get("premium_received") or 0)
            summary[ticker]["pnl_realized_total"]  += float(c.get("pnl_realized") or 0)

    rows = list(summary.values())
    for r in rows:
        n = r["cycles_closed"]
        r["avg_pnl_per_cycle"] = round(r["pnl_realized_total"] / n, 2) if n else 0.0
        r["premium_total"]     = round(r["premium_total"], 2)
        r["pnl_realized_total"]= round(r["pnl_realized_total"], 2)

    fieldnames = [
        "ticker", "year", "cycles_closed",
        "premium_total", "pnl_realized_total", "avg_pnl_per_cycle",
    ]
    _write_csv(out_path, fieldnames, rows)
    return str(out_path.resolve())
