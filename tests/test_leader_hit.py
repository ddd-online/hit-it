import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
        },
        "support20": 9.2,
        "volume": {"ratio5_to_prior20": 0.7, "today_to_prior20": 1.0},
        "prior_limit_count_5_60": 2,
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
        features["current_price"] = 10.2
        features["return5_pct"] = 4.0
        features["volume"]["today_to_prior20"] = 1.8
        status, _score, checks, _exclusions = LEADER.classify(features)
        self.assertTrue(checks["breakout"])
        self.assertEqual(status, "启动初段")

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
        self.assertEqual(status, "高位排除")
        self.assertTrue(any("高位" in reason for reason in exclusions))

    def test_dispersed_chips_are_excluded(self):
        features = base_features()
        features["chips"]["concentration70_pct"] = 25.0
        status, _score, _checks, exclusions = LEADER.classify(features)
        self.assertEqual(status, "排除")
        self.assertTrue(any("筹码分散" in reason for reason in exclusions))


if __name__ == "__main__":
    unittest.main()
