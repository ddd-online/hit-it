import importlib.util
import pathlib
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "limit-hit"
    / "scripts"
    / "backtest_limit_hit.py"
)
SPEC = importlib.util.spec_from_file_location("backtest_limit_hit", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TradeSimulationTest(unittest.TestCase):
    def test_simulates_round_lot_and_costs(self):
        fees = {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_rate": 0.00001,
        }
        trade = MODULE.simulate_trade(
            {"theme": "测试"}, entry_price=10.0, exit_price=11.0,
            capital=6000.0, fees=fees)

        self.assertEqual(trade["shares"], 600)
        self.assertEqual(trade["gross_gap_pct"], 10.0)
        expected_fees = 5 + 0.06 + 5 + 3.3 + 0.066
        self.assertAlmostEqual(trade["fees"], round(expected_fees, 2))
        self.assertAlmostEqual(
            trade["net_pnl"],
            round(6600 - 6000 - expected_fees, 2),
        )

    def test_fractional_position_uses_only_requested_capital(self):
        fees = {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_rate": 0.00001,
        }
        trade = MODULE.simulate_trade(
            {"theme": "测试"}, entry_price=10.0, exit_price=11.0,
            capital=100000.0, fees=fees, position_fraction=0.4)

        self.assertEqual(trade["shares"], 4000)
        self.assertEqual(trade["position_value"], 40000.0)
        self.assertEqual(trade["cash_after_buy"], 60000.0)
        self.assertAlmostEqual(trade["capital_after"], 103956.16)


class MetricsTest(unittest.TestCase):
    def test_summarizes_win_rate_odds_and_drawdown(self):
        trades = [
            {"gross_gap_pct": 2.0, "net_pnl": 60.0,
             "net_return_pct": 1.0, "capital_after": 6060.0},
            {"gross_gap_pct": -1.0, "net_pnl": -120.0,
             "net_return_pct": -2.0, "capital_after": 5940.0},
            {"gross_gap_pct": 1.0, "net_pnl": 30.0,
             "net_return_pct": 0.5, "capital_after": 5970.0},
        ]
        summary = MODULE.summarize_trades(trades, 6000.0, signal_days=3)

        self.assertEqual(summary["direction_win_rate_pct"], 66.67)
        self.assertEqual(summary["net_win_rate_pct"], 66.67)
        self.assertEqual(summary["odds"], 0.38)
        self.assertEqual(summary["avg_high_open_pct"], 1.5)
        self.assertEqual(summary["avg_low_open_pct"], -1.0)
        self.assertEqual(summary["ending_capital"], 5970.0)
        self.assertEqual(summary["max_drawdown_pct"], 1.98)


class CandidateFilterTest(unittest.TestCase):
    def test_theme_and_candidate_thresholds_reduce_signals(self):
        rows = [
            {"code": "sh600001", "name": "甲", "board_count": 1,
             "first_seal": "09:30:00", "open_count": 0, "turnover_pct": 8,
             "reason_tags": ["机器人"]},
            {"code": "sh600002", "name": "乙", "board_count": 1,
             "first_seal": "09:31:00", "open_count": 1, "turnover_pct": 9,
             "reason_tags": ["机器人"]},
        ]
        ranked = MODULE.ranked_candidates(
            rows, rows, max_themes=3,
            min_theme_score=40, min_candidate_score=60)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["theme"], "机器人")
        self.assertGreaterEqual(ranked[0]["candidate_score"], 60)


if __name__ == "__main__":
    unittest.main()
