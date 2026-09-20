"""
manual_preflight.py — pre-flight calendario per il flusso IBKR manuale.

Uso:  python manual_preflight.py TICKER [YYYY-MM-DD]
      (la data e' la scadenza del contratto che si sta valutando, opzionale)

Sola lettura: nessun alert Telegram, nessuna scrittura sul DB.
"NON VERIFICATO" = dato assente da yfinance, NON "nessun evento".
"""
import sys
from datetime import date

import config
from covered_call_optimizer import _earnings_date, _get_ticker
from wheel_daemon import _annual_dividend, _last_dividend_age_days, _next_ex_div


def _line(label: str, value: str) -> None:
    print(f"{label:<12}{value}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    ticker = argv[0].upper()
    expiry = date.fromisoformat(argv[1]) if len(argv) > 1 else None
    today = date.today()
    t = _get_ticker(ticker)

    print(f"Pre-flight {ticker}" + (f" — scadenza {expiry}" if expiry else ""))

    earn = _earnings_date(t)
    if earn is None:
        _line("Earnings", "NON VERIFICATO (controllare news/8-K prima di proporre)")
    else:
        days = (earn - today).days
        flags = []
        if 0 <= days <= config.HOGUE_EARNINGS_BUFFER_DAYS:
            flags.append(f"entro {config.HOGUE_EARNINGS_BUFFER_DAYS}gg: call NON venduta")
        if expiry and today <= earn <= expiry:
            flags.append("prima della scadenza: put non proposta di default")
        _line("Earnings", f"{earn} (tra {days}gg)" + (" | " + "; ".join(flags) if flags else ""))

    ex_div = _next_ex_div(t)
    if ex_div is None:
        _line("Ex-div", "nessuna futura nota (o dato assente)")
    else:
        flag = " | prima della scadenza: rischio early assignment sulle call" if expiry and ex_div <= expiry else ""
        _line("Ex-div", f"{ex_div} (tra {(ex_div - today).days}gg){flag}")

    rate = _annual_dividend(t)
    age = _last_dividend_age_days(t)
    _line(
        "Dividendo",
        (f"rate annuo {rate:.2f}" if rate else "rate NON RILEVATO")
        + (f" | ultimo pagamento {age}gg fa" if age is not None else " | storico non disponibile"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
