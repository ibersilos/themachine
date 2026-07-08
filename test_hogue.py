"""
test_hogue.py — Unit test per il framework Hogue.

Esegui con:  python -m pytest test_hogue.py -v
oppure:      python test_hogue.py
"""

import sys
import os
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, PropertyMock
import pandas as pd

# Stub delle variabili d'ambiente necessarie prima di importare config
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_000")
os.environ.setdefault("TELEGRAM_CHAT_ID",   "12345")

from covered_call_optimizer import (
    HogueOptimizer,
    WheelPosition,
    HogueAction,
    _mid_price,
    _find_strike_by_delta,
)
import config


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: crea una WheelPosition di test
# ─────────────────────────────────────────────────────────────────────────────

def _make_position(
    ticker="AAPL",
    strike=190.0,
    expiry_in_days=30,
    premium_received=3.00,
    premium_current=1.50,
    entry_price=180.0,
    roll_count=0,
) -> WheelPosition:
    return WheelPosition(
        cycle_id=1,
        ticker=ticker,
        strike=strike,
        expiry=date.today() + timedelta(days=expiry_in_days),
        premium_received=premium_received,
        premium_current=premium_current,
        entry_price=entry_price,
        roll_count=roll_count,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Test: utilità
# ─────────────────────────────────────────────────────────────────────────────

class TestUtilities(unittest.TestCase):

    def test_mid_price_bid_ask(self):
        """Mid price calcolato correttamente da bid/ask."""
        row = pd.Series({"bid": 1.00, "ask": 1.50, "lastPrice": 0.0})
        self.assertAlmostEqual(_mid_price(row), 1.25)

    def test_mid_price_fallback_last(self):
        """Se bid/ask sono 0, usa lastPrice."""
        row = pd.Series({"bid": 0.0, "ask": 0.0, "lastPrice": 2.75})
        self.assertAlmostEqual(_mid_price(row), 2.75)

    def test_mid_price_partial_zero(self):
        """Se solo bid è 0, usa lastPrice come fallback."""
        row = pd.Series({"bid": 0.0, "ask": 1.80, "lastPrice": 1.60})
        self.assertAlmostEqual(_mid_price(row), 1.60)

    def test_find_strike_by_delta_empty(self):
        """DataFrame vuoto → restituisce strike calcolato da OTM%."""
        strike, prem = _find_strike_by_delta(pd.DataFrame(), 100.0, 0.30, 0.03)
        self.assertAlmostEqual(strike, 103.0)
        self.assertEqual(prem, 0.0)

    def test_find_strike_by_delta_selects_otm(self):
        """Deve selezionare strike OTM (> stock price)."""
        calls = pd.DataFrame({
            "strike":            [95.0, 100.0, 103.0, 108.0, 115.0],
            "bid":               [5.0,  3.0,   2.0,   1.0,   0.5],
            "ask":               [5.5,  3.5,   2.5,   1.5,   1.0],
            "lastPrice":         [5.2,  3.2,   2.2,   1.2,   0.7],
            "impliedVolatility": [0.3,  0.28,  0.26,  0.24,  0.22],
        })
        strike, prem = _find_strike_by_delta(calls, 100.0, 0.30, 0.03)
        self.assertGreater(strike, 100.0, "Lo strike selezionato deve essere OTM")
        self.assertGreater(prem, 0.0, "Il premio deve essere positivo")


# ─────────────────────────────────────────────────────────────────────────────
#  Test: WheelPosition
# ─────────────────────────────────────────────────────────────────────────────

class TestWheelPosition(unittest.TestCase):

    def test_days_to_expiry(self):
        pos = _make_position(expiry_in_days=15)
        self.assertEqual(pos.days_to_expiry, 15)

    def test_days_to_expiry_expired(self):
        pos = _make_position(expiry_in_days=0)
        self.assertEqual(pos.days_to_expiry, 0)

    def test_pct_captured_50(self):
        pos = _make_position(premium_received=4.00, premium_current=2.00)
        self.assertAlmostEqual(pos.pct_captured, 0.50)

    def test_pct_captured_zero_premium(self):
        pos = _make_position(premium_received=0.0)
        self.assertEqual(pos.pct_captured, 0.0)

    def test_pct_captured_full(self):
        pos = _make_position(premium_received=3.00, premium_current=0.01)
        self.assertGreater(pos.pct_captured, 0.99)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: check_early_close
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckEarlyClose(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    def test_hold_below_threshold(self):
        """30% catturato, 30 DTE → mantieni."""
        pos = _make_position(premium_received=4.00, premium_current=2.80, expiry_in_days=30)
        result = self.opt.check_early_close(pos)
        self.assertEqual(result.action, "hold")

    def test_close_50pct_dte_above_threshold(self):
        """50% catturato, 25 DTE → chiudi (regola 50%)."""
        pos = _make_position(premium_received=4.00, premium_current=2.00, expiry_in_days=25)
        result = self.opt.check_early_close(pos)
        self.assertEqual(result.action, "close")
        self.assertNotEqual(result.urgency, "immediate")

    def test_close_21dte_rule(self):
        """50% catturato, 15 DTE → chiudi IMMEDIATAMENTE (regola 21-DTE)."""
        pos = _make_position(premium_received=4.00, premium_current=2.00, expiry_in_days=15)
        result = self.opt.check_early_close(pos)
        self.assertEqual(result.action, "close")
        self.assertEqual(result.urgency, "immediate")

    def test_close_21dte_reopen_flag(self):
        """Chiusura immediata deve avere flag reopen_now=True."""
        pos = _make_position(premium_received=4.00, premium_current=1.00, expiry_in_days=10)
        result = self.opt.check_early_close(pos)
        self.assertTrue(result.details.get("reopen_now"))

    def test_close_profit_calculation(self):
        """Il profitto calcolato deve essere corretto."""
        pos = _make_position(premium_received=3.00, premium_current=1.00, expiry_in_days=10)
        result = self.opt.check_early_close(pos)
        expected_profit = (3.00 - 1.00) * 100
        self.assertAlmostEqual(result.details["profit_usd"], expected_profit)

    def test_75pct_captured_is_also_close(self):
        """75% catturato → deve chiudere (sopra la soglia 50%)."""
        pos = _make_position(premium_received=4.00, premium_current=1.00, expiry_in_days=30)
        result = self.opt.check_early_close(pos)
        self.assertEqual(result.action, "close")

    def test_threshold_respects_config(self):
        """La soglia di chiusura viene letta da config."""
        self.assertAlmostEqual(config.HOGUE_EARLY_CLOSE_PCT, 0.50)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: should_sell_call
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldSellCall(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer._get_ticker")
    def test_block_low_iv_rank(self, mock_get_ticker):
        """IV Rank < 20% → blocca."""
        mock_get_ticker.return_value = MagicMock()
        ok, reason = self.opt.should_sell_call("TEST", iv_rank=15.0)
        self.assertFalse(ok)
        self.assertIn("IV Rank", reason)

    @patch("covered_call_optimizer._earnings_date", return_value=date.today() + timedelta(days=4))
    @patch("covered_call_optimizer._weekly_return", return_value=0.02)
    @patch("covered_call_optimizer._get_ticker")
    def test_block_earnings_within_7_days(self, mock_ticker, mock_weekly, mock_earnings):
        """Earnings tra 4 giorni → blocca."""
        mock_ticker.return_value = MagicMock()
        ok, reason = self.opt.should_sell_call("TEST", iv_rank=50.0)
        self.assertFalse(ok)
        self.assertIn("Earnings", reason)

    @patch("covered_call_optimizer._earnings_date", return_value=date.today() + timedelta(days=20))
    @patch("covered_call_optimizer._weekly_return", return_value=-0.12)
    @patch("covered_call_optimizer._get_ticker")
    def test_block_weekly_drop(self, mock_ticker, mock_weekly, mock_earnings):
        """Calo settimanale >10% → blocca."""
        mock_ticker.return_value = MagicMock()
        ok, reason = self.opt.should_sell_call("TEST", iv_rank=50.0)
        self.assertFalse(ok)
        self.assertIn("Calo settimanale", reason)

    @patch("covered_call_optimizer._earnings_date", return_value=date.today() + timedelta(days=20))
    @patch("covered_call_optimizer._weekly_return", return_value=0.01)
    @patch("covered_call_optimizer._get_ticker")
    def test_allow_normal_conditions(self, mock_ticker, mock_weekly, mock_earnings):
        """Condizioni normali → permetti."""
        mock_ticker.return_value = MagicMock()
        ok, reason = self.opt.should_sell_call("TEST", iv_rank=45.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    @patch("covered_call_optimizer._earnings_date", return_value=None)
    @patch("covered_call_optimizer._weekly_return", return_value=0.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_allow_no_earnings_date(self, mock_ticker, mock_weekly, mock_earnings):
        """Nessuna data earnings disponibile → non blocca per earnings."""
        mock_ticker.return_value = MagicMock()
        ok, reason = self.opt.should_sell_call("TEST", iv_rank=60.0)
        self.assertTrue(ok)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: check_roll_opportunity
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRoll(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer._current_price", return_value=185.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_no_roll_price_below_trigger(self, mock_ticker, mock_price):
        """Prezzo lontano dallo strike → nessun roll."""
        mock_ticker.return_value = MagicMock()
        pos = _make_position(strike=200.0, expiry_in_days=20)
        result = self.opt.check_roll_opportunity(pos)
        self.assertEqual(result.action, "hold")

    @patch("covered_call_optimizer._current_price", return_value=198.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_no_roll_too_close_to_expiry(self, mock_ticker, mock_price):
        """Troppo vicino a scadenza (<=7 DTE) → no roll."""
        mock_ticker.return_value = MagicMock()
        pos = _make_position(strike=200.0, expiry_in_days=5)
        result = self.opt.check_roll_opportunity(pos)
        self.assertEqual(result.action, "hold")

    @patch("covered_call_optimizer._current_price", return_value=198.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_assigned_max_rolls_reached(self, mock_ticker, mock_price):
        """Roll_count al massimo → lascia assegnare."""
        mock_ticker.return_value = MagicMock()
        pos = _make_position(strike=200.0, expiry_in_days=20, roll_count=2)
        result = self.opt.check_roll_opportunity(pos)
        self.assertEqual(result.action, "assigned")

    @patch("covered_call_optimizer._current_price", return_value=0.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_hold_on_price_unavailable(self, mock_ticker, mock_price):
        """Prezzo non disponibile → hold."""
        mock_ticker.return_value = MagicMock()
        pos = _make_position(strike=200.0, expiry_in_days=20)
        result = self.opt.check_roll_opportunity(pos)
        self.assertEqual(result.action, "hold")


# ─────────────────────────────────────────────────────────────────────────────
#  Test: calculate_married_put
# ─────────────────────────────────────────────────────────────────────────────

class TestMarriedPut(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer._best_expiry", return_value="2026-09-19")
    @patch("covered_call_optimizer._get_option_chain")
    @patch("covered_call_optimizer._get_ticker")
    def test_put_strike_near_minus10pct(self, mock_ticker, mock_chain, mock_expiry):
        """Put strike deve essere vicino al -10% dell'entry price."""
        puts = pd.DataFrame({
            "strike":            [160.0, 162.0, 165.0, 168.0, 170.0],
            "bid":               [1.0,   1.2,   1.5,   1.8,   2.0],
            "ask":               [1.2,   1.4,   1.7,   2.0,   2.2],
            "lastPrice":         [1.1,   1.3,   1.6,   1.9,   2.1],
            "impliedVolatility": [0.25] * 5,
        })
        mock_chain.return_value = (pd.DataFrame(), puts)
        mock_ticker.return_value = MagicMock()

        result = self.opt.calculate_married_put("TEST", entry_price=180.0)

        self.assertNotIn("error", result)
        # -10% di 180 = 162; la put selezionata deve essere vicina
        self.assertAlmostEqual(result["put_strike"], 162.0)
        self.assertLess(result["cost_pct_trade"], 5.0)   # <5% del trade
        self.assertLess(result["max_downside_pct"], 0)   # downside è negativo

    @patch("covered_call_optimizer._best_expiry", return_value=None)
    @patch("covered_call_optimizer._get_ticker")
    def test_error_no_expiry(self, mock_ticker, mock_expiry):
        """Nessuna scadenza disponibile → errore."""
        mock_ticker.return_value = MagicMock()
        result = self.opt.calculate_married_put("TEST", entry_price=100.0)
        self.assertIn("error", result)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: calculate_collar
# ─────────────────────────────────────────────────────────────────────────────

class TestCollar(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    def test_collar_not_triggered_low_profit(self):
        """Profitto <20% → collar non calcolato."""
        pos = _make_position(entry_price=100.0)
        with patch("covered_call_optimizer._current_price", return_value=115.0):
            with patch("covered_call_optimizer._get_ticker", return_value=MagicMock()):
                result = self.opt.calculate_collar(pos, stock_price=115.0)
        self.assertIsNone(result)

    @patch("covered_call_optimizer._best_expiry", return_value="2026-09-19")
    @patch("covered_call_optimizer._get_option_chain")
    @patch("covered_call_optimizer._current_price", return_value=125.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_collar_triggered_above_20pct(self, mock_ticker, mock_price, mock_chain, mock_expiry):
        """Profitto >20% → collar calcolato."""
        calls = pd.DataFrame({
            "strike":            [130.0, 135.0, 140.0, 145.0],
            "bid":               [2.0,   1.5,   1.0,   0.6],
            "ask":               [2.4,   1.8,   1.3,   0.9],
            "lastPrice":         [2.2,   1.6,   1.1,   0.7],
            "impliedVolatility": [0.25] * 4,
        })
        puts = pd.DataFrame({
            "strike":            [110.0, 112.0, 115.0, 118.0],
            "bid":               [1.0,   1.2,   1.5,   1.8],
            "ask":               [1.2,   1.4,   1.7,   2.0],
            "lastPrice":         [1.1,   1.3,   1.6,   1.9],
            "impliedVolatility": [0.25] * 4,
        })
        mock_chain.return_value = (calls, puts)
        mock_ticker.return_value = MagicMock()

        pos = _make_position(entry_price=100.0)
        result = self.opt.calculate_collar(pos, stock_price=125.0)

        self.assertIsNotNone(result)
        self.assertGreater(result["call_strike"], 125.0)
        self.assertLess(result["put_strike"],    125.0)
        self.assertIn("telegram_msg", result)
        self.assertGreater(result["profit_pct"], 20.0)

    @patch("covered_call_optimizer._best_expiry", return_value="2026-09-19")
    @patch("covered_call_optimizer._get_option_chain")
    @patch("covered_call_optimizer._get_ticker")
    def test_collar_nearly_free_flag(self, mock_ticker, mock_chain, mock_expiry):
        """Se costo netto <$0.05 → is_nearly_free=True."""
        calls = pd.DataFrame({
            "strike":   [143.0], "bid": [2.00], "ask": [2.00], "lastPrice": [2.00],
            "impliedVolatility": [0.25],
        })
        puts = pd.DataFrame({
            "strike":   [112.0], "bid": [2.00], "ask": [2.00], "lastPrice": [2.00],
            "impliedVolatility": [0.25],
        })
        mock_chain.return_value = (calls, puts)
        mock_ticker.return_value = MagicMock()

        pos = _make_position(entry_price=100.0)
        # put_prem ≈ call_prem → net_cost ≈ 0
        result = self.opt.calculate_collar(pos, stock_price=125.0)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_nearly_free"])


# ─────────────────────────────────────────────────────────────────────────────
#  Test: calculate_iron_condor
# ─────────────────────────────────────────────────────────────────────────────

class TestIronCondor(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer._get_ticker")
    def test_none_below_iv_threshold(self, mock_ticker):
        """IV Rank <80% → Iron Condor non calcolato."""
        mock_ticker.return_value = MagicMock()
        result = self.opt.calculate_iron_condor("TEST", iv_rank=60.0)
        self.assertIsNone(result)

    @patch("covered_call_optimizer._best_expiry", return_value="2026-09-19")
    @patch("covered_call_optimizer._get_option_chain")
    @patch("covered_call_optimizer._current_price", return_value=100.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_condor_above_iv_threshold(self, mock_ticker, mock_price, mock_chain, mock_expiry):
        """IV Rank >80% → Iron Condor calcolato con struttura valida."""
        calls = pd.DataFrame({
            "strike":            [100.0, 105.0, 110.0, 115.0, 120.0],
            "bid":               [5.0,   3.0,   1.5,   0.8,   0.4],
            "ask":               [5.5,   3.5,   1.8,   1.0,   0.6],
            "lastPrice":         [5.2,   3.2,   1.6,   0.9,   0.5],
            "impliedVolatility": [0.35] * 5,
        })
        puts = pd.DataFrame({
            "strike":            [80.0, 85.0, 90.0, 95.0, 100.0],
            "bid":               [0.3,  0.6,  1.2,  2.5,  5.0],
            "ask":               [0.5,  0.8,  1.5,  2.8,  5.5],
            "lastPrice":         [0.4,  0.7,  1.3,  2.6,  5.2],
            "impliedVolatility": [0.35] * 5,
        })
        mock_chain.return_value = (calls, puts)
        mock_ticker.return_value = MagicMock()

        result = self.opt.calculate_iron_condor("TEST", iv_rank=85.0)

        self.assertIsNotNone(result)
        # Struttura: long_put < short_put < stock < short_call < long_call
        self.assertLess(result["long_put_strike"],   result["short_put_strike"])
        self.assertLess(result["short_put_strike"],  100.0)
        self.assertGreater(result["short_call_strike"], 100.0)
        self.assertGreater(result["long_call_strike"],  result["short_call_strike"])
        self.assertGreater(result["net_premium_usd"], 0)
        self.assertIn("telegram_msg", result)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: get_annualized_return
# ─────────────────────────────────────────────────────────────────────────────

class TestAnnualizedReturn(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer.db.count_closed_cycles_year", return_value=8)
    @patch("covered_call_optimizer.db.avg_pnl_per_cycle", return_value=150.0)
    @patch("covered_call_optimizer._current_price", return_value=100.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_projection(self, mock_ticker, mock_price, mock_avg, mock_count):
        """Proiezione annuale basata su cicli completati."""
        mock_ticker.return_value = MagicMock()
        result = self.opt.get_annualized_return("TEST")
        self.assertEqual(result["cycles_completed"], 8)
        self.assertEqual(result["target_cycles_year"], config.HOGUE_TARGET_CYCLES_YEAR)
        self.assertGreater(result["projected_annual_pct"], 0)

    def test_direct_params(self):
        """Calcolo con parametri diretti senza DB."""
        result = self.opt.get_annualized_return(
            "TEST",
            cycles_completed=6,
            avg_monthly_return=1.5,
        )
        expected = 1.5 * config.HOGUE_TARGET_CYCLES_YEAR
        self.assertAlmostEqual(result["projected_annual_pct"], expected)
        self.assertEqual(result["realized_ytd_pct"], 1.5 * 6)


# ─────────────────────────────────────────────────────────────────────────────
#  Test: format_pick_alert
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatPickAlert(unittest.TestCase):

    def setUp(self):
        self.opt = HogueOptimizer()

    @patch("covered_call_optimizer._earnings_date", return_value=None)
    @patch("covered_call_optimizer._current_price", return_value=180.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_pick_alert_contains_key_fields(self, mock_ticker, mock_price, mock_earnings):
        """L'alert del pick deve contenere ticker, score, stop loss e married put."""
        mock_ticker.return_value = MagicMock()
        signal = {
            "short_name":    "Apple Inc.",
            "current_price": 180.0,
            "flags":         ["PE=12.0 (attractive)", "Insider purchase"],
            "source":        "form4",
        }
        married_put = {
            "put_strike":     162.0,
            "put_expiry":     "2026-09-19",
            "put_dte":        45,
            "total_cost_usd": 120.0,
            "cost_pct_trade": 0.67,
            "max_downside_pct": -10.0,
        }
        next_cc = {
            "strike":             185.0,
            "expiry":             "2026-08-15",
            "premium":            2.50,
            "monthly_return_pct": 1.4,
            "iv_rank":            45.0,
        }

        msg = self.opt.format_pick_alert("AAPL", 78, signal, married_put, next_cc)

        self.assertIn("AAPL", msg)
        self.assertIn("78", msg)
        self.assertIn("162", msg)         # put strike
        self.assertIn("Stop loss", msg)
        self.assertIn("Married Put", msg)

    @patch("covered_call_optimizer._earnings_date", return_value=None)
    @patch("covered_call_optimizer._current_price", return_value=100.0)
    @patch("covered_call_optimizer._get_ticker")
    def test_pick_alert_no_cc_available(self, mock_ticker, mock_price, mock_earnings):
        """Se CC non disponibile (IV bassa), il messaggio lo indica."""
        mock_ticker.return_value = MagicMock()
        signal = {"current_price": 100.0, "flags": [], "source": "edgar_8k"}
        married_put = {
            "put_strike": 90.0, "put_expiry": "2026-09-19",
            "put_dte": 45, "total_cost_usd": 50.0,
            "cost_pct_trade": 0.5, "max_downside_pct": -10.0,
        }
        next_cc = {"error": "IV insufficiente"}

        msg = self.opt.format_pick_alert("XYZ", 65, signal, married_put, next_cc)
        self.assertIn("IV insufficiente", msg)


# ─────────────────────────────────────────────────────────────────────────────
#  Runner diretto
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loader  = unittest.TestLoader()
    suite   = loader.discover(".", pattern="test_hogue.py")
    runner  = unittest.TextTestRunner(verbosity=2)
    result  = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
