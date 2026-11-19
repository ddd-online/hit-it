import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "leader-hit" / "scripts" / "prepare_leader_candidates.py"
    spec = importlib.util.spec_from_file_location("leader_hit_prepare", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEADER = load_module()


def base_features():
    return {
        "current_price": 10.0,
        "day_return_pct": 1.0,
        "close_location_pct": 60.0,
        "range_position_pct": 28.0,
        "return20_pct": 8.0,
        "return60_pct": 12.0,
        "ma": {"5": 9.8, "10": 9.7, "20": 9.6},
        "ma_spread_pct": 3.0,
        "box30": {
            "high": 10.2,
            "low": 8.8,
            "range_pct": 15.9,
            "distance_to_high_pct": 2.0,
            "breakout_level": 10.2,
            "breakout_source": "30日箱体上沿",
            "breakout_margin_pct": -1.96,
        },
        "support20": 9.2,
        "volume": {"ratio5_to_prior20": 0.7, "today_to_prior20": 1.0},
        "prior_limit_count_5_60": 2,
        "limit_count_1_60": 2,
        "latest_limit_trigger": None,
        "one_word_chain_recent": False,
        "chips": {
            "concentration70_pct": 10.0,
            "concentration90_pct": 24.0,
        },
        "overhead_to_cost90_pct": 15.0,
        "profit_ratio_pct": 60.0,
        "bar_count": 120,
    }


class MainboardTest(unittest.TestCase):
    def test_only_hs_mainboard_is_allowed(self):
        allowed = ["sh600000", "sh601011", "sh603000", "sh605001",
                   "sz000001", "sz001001", "sz002001", "sz003001"]
        blocked = ["sh688001", "sz300001", "bj920001", "sz301001"]
        self.assertTrue(all(LEADER.is_mainboard(code) for code in allowed))
        self.assertFalse(any(LEADER.is_mainboard(code) for code in blocked))

    def test_normalize_code_adds_exchange_prefix(self):
        self.assertEqual(LEADER.normalize_code("600000"), "sh600000")
        self.assertEqual(LEADER.normalize_code("000001"), "sz000001")
        with self.assertRaises(ValueError):
            LEADER.normalize_code("123")

    def test_quote_prefilter_prefers_warm_stock_over_overheated_stock(self):
        warm = {
            "code": "sh600001", "change5_pct": 4.0, "change10_pct": 8.0,
            "change_pct": 2.0, "turnover_pct": 5.0, "volume_ratio": 1.2,
            "amount_yuan": 100_000_000, "main_inflow_yuan": 10_000_000,
        }
        overheated = {
            "code": "sh600002", "change5_pct": 42.0, "change10_pct": 70.0,
            "change_pct": 8.0, "turnover_pct": 36.0, "volume_ratio": 5.0,
            "amount_yuan": 500_000_000, "main_inflow_yuan": -10_000_000,
        }
        ranked = LEADER.preselect_quotes([overheated, warm], 1)
        self.assertEqual(ranked[0]["code"], warm["code"])

    def test_scripts_run_without_other_skill_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "skills" / "leader-hit" / "scripts"
            shutil.copytree(ROOT / "leader-hit" / "scripts", target)
            completed = subprocess.run(
                [sys.executable, "prepare_leader_candidates.py", "--help"],
                cwd=target,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--price-max", completed.stdout)


class ClassificationTest(unittest.TestCase):
    def test_waiting_structure_requires_dry_volume_and_concentrated_chips(self):
        status, score, _checks, exclusions = LEADER.classify(base_features())
        self.assertEqual(status, "待启动")
        self.assertGreaterEqual(score, 60)
        self.assertEqual(exclusions, [])

    def test_breakout_moves_to_early_start(self):
        features = base_features()
        features["current_price"] = 10.4
        features["day_return_pct"] = 9.5
        features["close_location_pct"] = 90.0
        features["box30"]["breakout_margin_pct"] = 1.96
        features["return5_pct"] = 4.0
        features["volume"]["today_to_prior20"] = 1.8
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["breakout"])
        self.assertEqual(status, "启动初段")

    def test_breakout_can_use_recent_limit_pressure(self):
        features = base_features()
        features["box30"]["high"] = 11.0
        features["box30"]["breakout_level"] = 10.2
        features["box30"]["breakout_source"] = "最近涨停压力位"
        features["box30"]["breakout_margin_pct"] = 1.08
        features["current_price"] = 10.31
        features["day_return_pct"] = 8.0
        features["close_location_pct"] = 90.0
        features["return5_pct"] = 4.0
        features["volume"]["today_to_prior20"] = 1.8
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["breakout"])
        self.assertEqual(status, "启动初段")

    def test_confirmed_breakout_does_not_demote_for_auxiliary_chip_metrics(self):
        features = base_features()
        features["current_price"] = 10.4
        features["day_return_pct"] = 9.5
        features["close_location_pct"] = 90.0
        features["box30"]["breakout_margin_pct"] = 1.96
        features["return5_pct"] = 4.0
        features["volume"]["today_to_prior20"] = 1.8
        features["chips"]["concentration70_pct"] = 18.0
        features["chips"]["concentration90_pct"] = 35.0
        features["overhead_to_cost90_pct"] = 30.0
        features["profit_ratio_pct"] = 95.0
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["breakout"])
        self.assertFalse(checks["chips_good"])
        self.assertEqual(status, "启动初段")

    def test_weak_breakout_stays_observation(self):
        features = base_features()
        features["current_price"] = 10.25
        features["day_return_pct"] = 2.35
        features["close_location_pct"] = 60.0
        features["box30"]["breakout_margin_pct"] = 0.49
        features["return5_pct"] = 4.0
        features["volume"]["today_to_prior20"] = 2.5
        status, _score, checks, exclusions = LEADER.classify(features)
        self.assertTrue(checks["breakout"])
        self.assertFalse(checks["breakout_strength"])
        self.assertEqual(status, "只观察")
        self.assertTrue(any("突破确认不足" in reason for reason in exclusions))

    def test_recent_limit_hold_fills_one_to_four_day_gap(self):
        features = base_features()
        features["box30"]["high"] = 11.5
        features["box30"]["breakout_margin_pct"] = -13.04
        features["volume"]["ratio5_to_prior20"] = 1.5
        features["prior_limit_count_5_60"] = 1
        features["limit_count_1_60"] = 1
        features["latest_limit_trigger"] = {
            "date": "2026-09-24",
            "bars_ago": 2,
            "high": 10.8,
            "low": 9.0,
            "volume": 2000.0,
            "post_avg_volume": 1500.0,
        }
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["post_limit_hold"])
        self.assertTrue(checks["wash"])
        self.assertEqual(status, "洗盘中")

    def test_wash_chips_do_not_require_profit_floor(self):
        features = base_features()
        features["box30"]["high"] = 11.5
        features["box30"]["breakout_margin_pct"] = -13.04
        features["profit_ratio_pct"] = 29.31
        features["latest_limit_trigger"] = {
            "date": "2026-09-18",
            "bars_ago": 5,
            "high": 10.8,
            "low": 9.0,
            "volume": 2000.0,
            "post_avg_volume": 1200.0,
        }
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertFalse(checks["chips_good"])
        self.assertTrue(checks["wash_chips_good"])
        self.assertEqual(status, "洗盘中")

    def test_no_left_limit_tight_base_is_early_watch_only(self):
        features = base_features()
        features["prior_limit_count_5_60"] = 0
        features["limit_count_1_60"] = 0
        features["box30"]["range_pct"] = 10.0
        features["box30"]["distance_to_high_pct"] = 3.0
        features["ma_spread_pct"] = 1.5
        features["volume"]["ratio5_to_prior20"] = 0.83
        status, _score, checks, exclusions = LEADER.classify(features)
        self.assertTrue(checks["early_watch"])
        self.assertEqual(status, "只观察")
        self.assertFalse(any("近 5-60" in reason for reason in exclusions))

    def test_shrinking_wash_keeps_support(self):
        features = base_features()
        features["box30"]["high"] = 11.5
        features["volume"]["today_to_prior20"] = 1.0
        features["latest_limit_trigger"] = {
            "date": "2026-10-20",
            "bars_ago": 10,
            "high": 10.8,
            "low": 9.0,
            "volume": 1000.0,
            "post_avg_volume": 650.0,
        }
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["wash"])
        self.assertEqual(status, "洗盘中")

    def test_high_position_is_excluded_even_with_good_chips(self):
        features = base_features()
        features["range_position_pct"] = 86.0
        features["return20_pct"] = 42.0
        status, _score, _checks, exclusions = LEADER.classify(features)
        self.assertEqual(status, "排除")
        self.assertTrue(any("高位" in reason for reason in exclusions))

    def test_dispersed_chips_are_excluded(self):
        features = base_features()
        features["chips"]["concentration70_pct"] = 25.0
        status, _score, _checks, exclusions = LEADER.classify(features)
        self.assertEqual(status, "排除")
        self.assertTrue(any("筹码分散" in reason for reason in exclusions))


class HistoricalReplayTest(unittest.TestCase):
    def test_historical_quote_uses_target_bar_close(self):
        bar = {"date": "2026-06-30", "close": 9.8, "high": 10.0}
        quote = LEADER.historical_quote("sh600001", "测试", bar)
        self.assertEqual(quote["price"], 9.8)
        self.assertEqual(quote["quote_date"], "2026-06-30")

    def test_historical_universe_filters_on_target_date_close(self):
        universe = [
            {"code": "sh600001", "name": "甲"},
            {"code": "sh600002", "name": "乙"},
        ]
        bars_by_code = {
            "sh600001": [{
                "date": "2026-09-28", "open": 9.0, "close": 9.1,
                "high": 9.2, "low": 8.9, "volume_hands": 100.0,
                "amount_yuan": 1000.0, "turnover_pct": 1.0,
            }],
            "sh600002": [{
                "date": "2026-09-28", "open": 16.0, "close": 16.1,
                "high": 16.2, "low": 15.9, "volume_hands": 100.0,
                "amount_yuan": 1000.0, "turnover_pct": 1.0,
            }],
        }

        def fake_kline(code, _days, cutoff):
            self.assertEqual(cutoff, "2026-09-28")
            return bars_by_code[code], "测试日K", []

        with patch.object(
            LEADER, "fetch_mainboard_universe", return_value=universe
        ), patch.object(LEADER.market_data, "fetch_kline", side_effect=fake_kline):
            rows, universe_count, eligible_count = (
                LEADER.scan_historical_universe(
                    15.0, "2026-09-28", 2, universe_only=True)
            )
        self.assertEqual(universe_count, 2)
        self.assertEqual(eligible_count, 1)
        self.assertEqual([row["code"] for row in rows], ["sh600001"])

if __name__ == "__main__":
    unittest.main()
