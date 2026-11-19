import importlib.util
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "backtest" / "limit-hit" / "backtest_sector_second_line.py"
SPEC = importlib.util.spec_from_file_location("sector_second_line", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TimeAndThemeTest(unittest.TestCase):
    def test_parse_times_is_fixed_and_ordered(self):
        self.assertEqual(
            MODULE.parse_times("09:40,10:00,10:30,11:00"),
            ("09:40:00", "10:00:00", "10:30:00", "11:00:00"),
        )

    def test_visible_rows_use_first_seal_cutoff(self):
        rows = [
            {"code": "sh600001", "first_seal": "09:35:00"},
            {"code": "sh600002", "first_seal": "10:05:00"},
            {"code": "sh600003", "first_seal": None},
        ]
        self.assertEqual(
            [row["code"] for row in MODULE.visible_limit_rows(rows, "10:00:00")],
            ["sh600001"],
        )

    def test_theme_ladder_keeps_width_and_promotion(self):
        current = [
            {"code": "sh600001", "name": "甲", "board_count": 2,
             "reason_tags": ["机器人"], "first_seal": "09:31:00"},
            {"code": "sh600002", "name": "乙", "board_count": 1,
             "reason_tags": ["机器人"], "first_seal": "09:35:00"},
        ]
        previous = [
            {"code": "sh600001", "board_count": 1, "reason_tags": ["机器人"]},
        ]
        themes = MODULE.theme_ladder(
            current, previous, min_score=1, max_themes=3)
        self.assertEqual(themes[0]["theme"], "机器人")
        self.assertEqual(themes[0]["current_count"], 2)
        self.assertEqual(themes[0]["promoted_count"], 1)


class MembershipTest(unittest.TestCase):
    def test_board_index_uses_exact_name_only(self):
        index = MODULE.build_board_index([
            {"name": "机器人概念", "code": "BK1001"},
            {"name": "机器人", "code": "BK1002"},
        ])
        self.assertEqual(index["机器人"], "BK1002")
        self.assertNotIn("机器人设备", index)

    def test_member_rows_filter_mainboard_and_parse_codes(self):
        raw = [
            {"SECURITY_CODE": "600001", "SECURITY_NAME_ABBR": "甲"},
            {"SECURITY_CODE": "300001", "SECURITY_NAME_ABBR": "乙"},
            {"SECURITY_CODE": "000002", "SECURITY_NAME_ABBR": "丙"},
        ]
        rows = MODULE.normalize_members(raw)
        self.assertEqual([row["code"] for row in rows], ["sh600001", "sz000002"])

    def test_cross_theme_dedupe_keeps_stronger_theme(self):
        rows = [
            {"code": "sh600001", "theme": "弱题材", "theme_score": 50},
            {"code": "sh600001", "theme": "强题材", "theme_score": 80},
        ]
        deduped = MODULE.dedupe_candidates(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["theme"], "强题材")


def make_bars():
    bars = []
    for index in range(25):
        close = 10.0 + index * 0.01
        bars.append({
            "date": "2026-01-{:02d}".format(index + 1),
            "open": close - 0.03,
            "high": close + 0.08,
            "low": close - 0.08,
            "close": close,
            "volume_hands": 1000.0,
        })
    return bars


def make_trade_bars(open_price, close_price, next_open):
    bars = []
    for index in range(24):
        close = 10.0 + index * 0.01
        bars.append({
            "date": "2026-01-{:02d}".format(index + 1),
            "open": close - 0.03,
            "high": close + 0.08,
            "low": close - 0.08,
            "close": close,
            "volume_hands": 1000.0,
        })
    bars.append({
        "date": "2026-01-25",
        "open": open_price,
        "high": max(open_price, close_price) + 0.05,
        "low": min(open_price, close_price) - 0.05,
        "close": close_price,
        "volume_hands": 1200.0,
    })
    bars.append({
        "date": "2026-01-26",
        "open": next_open,
        "high": next_open + 0.05,
        "low": next_open - 0.05,
        "close": next_open,
        "volume_hands": 900.0,
    })
    return bars


class FeatureTest(unittest.TestCase):
    def test_features_use_previous_day_structure(self):
        bars = make_bars()
        day = bars[-1]
        day["open"] = round(bars[-2]["close"] * 1.02, 2)
        feature = MODULE.bar_features(bars, day["date"])
        self.assertEqual(feature["previous_close"], bars[-2]["close"])
        self.assertEqual(feature["open_gap_pct"], 1.955)

    def test_second_line_rejects_limit_open(self):
        bars = make_bars()
        bars[-1]["open"] = round(bars[-2]["close"] * 1.10, 2)
        feature = MODULE.bar_features(bars, bars[-1]["date"])
        self.assertFalse(MODULE.second_line_eligible(feature))

    def test_second_line_accepts_controlled_open(self):
        bars = make_bars()
        bars[-1]["open"] = round(bars[-2]["close"] * 1.02, 2)
        feature = MODULE.bar_features(bars, bars[-1]["date"])
        self.assertTrue(MODULE.second_line_eligible(feature))


class SimulationTest(unittest.TestCase):
    def test_candidate_outcome_marks_seal_and_premium(self):
        feature = {
            "data_date": "2026-01-05",
            "open": 10.0,
            "close": 11.0,
            "limit_price": 11.0,
            "previous_close": 10.0,
        }
        outcome = MODULE.candidate_outcome(feature, {"open": 11.3})
        self.assertTrue(outcome["sealed_close"])
        self.assertEqual(outcome["next_open_premium_pct"], 2.7273)

    def test_open_limit_cannot_fill_second_line(self):
        feature = {
            "data_date": "2026-01-05",
            "open": 11.0,
            "close": 11.0,
            "limit_price": 11.0,
            "previous_close": 10.0,
        }
        self.assertFalse(MODULE.can_fill_second_line(feature))

    def test_group_capital_compounds_once_per_day(self):
        signals = [
            {"date": "2026-01-05", "entry_price": 10.0,
             "exit_price": 11.0, "theme": "机器人"},
            {"date": "2026-01-05", "entry_price": 10.0,
             "exit_price": 12.0, "theme": "机器人"},
            {"date": "2026-01-06", "entry_price": 11.0,
             "exit_price": 10.5, "theme": "机器人"},
        ]
        result = MODULE.run_group(
            signals, 10000.0,
            {"commission_rate": 0.00025, "min_commission": 5.0,
             "stamp_duty_rate": 0.0005, "transfer_rate": 0.00001},
        )
        self.assertEqual(result["summary"]["trade_count"], 2)
        self.assertEqual(result["summary"]["ending_capital"], 10519.37)

    def test_group_summary_reports_seal_and_premium_rates(self):
        signals = [
            {
                "date": "2026-01-05",
                "entry_price": 10.0,
                "exit_price": 11.0,
                "theme": "机器人",
                "outcome": {
                    "sealed_close": True,
                    "next_open_premium_pct": 2.0,
                },
            },
            {
                "date": "2026-01-06",
                "entry_price": 10.0,
                "exit_price": 9.8,
                "theme": "机器人",
                "outcome": {
                    "sealed_close": False,
                    "next_open_premium_pct": -1.0,
                },
            },
        ]
        result = MODULE.run_group(
            signals,
            10000.0,
            {"commission_rate": 0.00025, "min_commission": 5.0,
             "stamp_duty_rate": 0.0005, "transfer_rate": 0.00001},
        )
        summary = result["summary"]
        self.assertEqual(summary["seal_rate_pct"], 50.0)
        self.assertEqual(summary["positive_next_open_rate_pct"], 50.0)
        self.assertEqual(summary["avg_next_open_premium_pct"], 0.5)


class ReportTest(unittest.TestCase):
    def test_limitations_state_v17_diffusion_admission_is_not_backtested(self):
        limitations = MODULE.backtest_limitations()

        self.assertTrue(any(
            "无法严格验证 v1.7" in text and "D" in text
            for text in limitations
        ))

    def test_time_report_contains_both_groups_and_limits(self):
        result = {
            "time": "09:40:00",
            "start": "20260101",
            "end": "20261119",
            "capital": 10000.0,
            "groups": {
                "limit": {
                    "summary": {
                        "ending_capital": 10000.0,
                        "trade_count": 0,
                        "direction_win_rate_pct": None,
                        "net_win_rate_pct": None,
                        "net_return_pct": 0.0,
                        "max_drawdown_pct": 0.0,
                    },
                    "trades": [],
                },
                "second_line": {
                    "summary": {
                        "ending_capital": 10000.0,
                        "trade_count": 0,
                        "direction_win_rate_pct": None,
                        "net_win_rate_pct": None,
                        "net_return_pct": 0.0,
                        "max_drawdown_pct": 0.0,
                    },
                    "trades": [],
                },
            },
            "limitations": ["当前概念成分不是历史快照"],
        }
        text = MODULE.render_time_report(result)
        self.assertIn("打板股组", text)
        self.assertIn("第二梯队组", text)
        self.assertIn("当日封板率", text)
        self.assertIn("次日正溢价率", text)
        self.assertIn("当前概念成分不是历史快照", text)
        self.assertIn("不构成投资建议", text)

    def test_comparison_has_all_supported_times(self):
        results = [
            {
                "time": time,
                "groups": {
                    "limit": {"summary": {
                        "ending_capital": 10000.0,
                        "trade_count": 0,
                    }},
                    "second_line": {"summary": {
                        "ending_capital": 10000.0,
                        "trade_count": 0,
                    }},
                },
            }
            for time in ("09:40:00", "10:00:00", "10:30:00", "11:00:00")
        ]
        text = MODULE.render_comparison(results)
        self.assertIn("| 09:40:00 |", text)
        self.assertIn("| 11:00:00 |", text)
        self.assertIn("不构成投资建议", text)

    def test_report_paths_are_unique(self):
        paths = MODULE.report_paths(
            pathlib.Path("C:/work"),
            "20260101",
            "20261119",
            ("09:40:00", "10:00:00", "10:30:00", "11:00:00"),
        )
        self.assertEqual(len(set(paths.values())), 5)
        self.assertIn("09-40", str(paths["09:40:00"]))
        self.assertIn("时点对比", str(paths["comparison"]))

    def test_cli_exposes_backtest_arguments(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--times", completed.stdout)


class IntegrationTest(unittest.TestCase):
    def test_selects_limit_and_second_line_groups_separately(self):
        current_rows = [
            {
                "code": "sh600001",
                "name": "甲",
                "board_count": 2,
                "first_seal": "09:31:00",
                "last_seal": "10:45:00",
                "open_count": 1,
                "turnover_pct": 10.0,
                "reason_tags": ["机器人"],
            },
        ]
        bars = {
            "sh600001": make_trade_bars(11.25, 11.25, 11.50),
            "sh600002": make_trade_bars(10.43, 10.80, 10.90),
        }
        members = [
            {"code": "sh600001", "name": "甲", "float_cap_yi": 50.0},
            {"code": "sh600002", "name": "乙", "float_cap_yi": 30.0},
        ]
        signals, skipped = MODULE.select_candidates_for_day(
            "10:30:00",
            "2026-01-25",
            [{"theme": "机器人", "theme_score": 70}],
            current_rows,
            {"机器人": "BK1001"},
            bars,
            lambda code: bars[code],
            lambda _board: members,
            {},
            min_candidate_score=60,
        )
        by_group = {row["group"]: row for row in signals}
        self.assertEqual(by_group["limit"]["code"], "sh600001")
        self.assertEqual(by_group["second_line"]["code"], "sh600002")
        self.assertEqual(skipped, [])

    def test_missing_board_mapping_keeps_limit_group_only(self):
        current_rows = [
            {
                "code": "sh600001",
                "name": "甲",
                "board_count": 2,
                "first_seal": "09:31:00",
                "last_seal": "10:45:00",
                "open_count": 1,
                "turnover_pct": 10.0,
                "reason_tags": ["机器人"],
            },
        ]
        bars = {"sh600001": make_trade_bars(11.25, 11.25, 11.50)}
        signals, skipped = MODULE.select_candidates_for_day(
            "10:30:00",
            "2026-01-25",
            [{"theme": "机器人", "theme_score": 70}],
            current_rows,
            {},
            bars,
            lambda code: bars[code],
            lambda _board: [],
            {},
            min_candidate_score=60,
        )
        self.assertEqual([row["group"] for row in signals], ["limit"])
        self.assertEqual(skipped[0]["reason"], "未找到同名精确概念板块")

    def test_time_point_keeps_group_capital_independent(self):
        dates = ["2026-01-24", "2026-01-25", "2026-01-26"]
        current = [
            {
                "code": "sh600001",
                "name": "甲",
                "board_count": 2,
                "first_seal": "09:31:00",
                "last_seal": "10:45:00",
                "open_count": 1,
                "turnover_pct": 10.0,
                "reason_tags": ["机器人"],
            },
            {
                "code": "sh600003",
                "name": "丙",
                "board_count": 1,
                "first_seal": "09:35:00",
                "last_seal": "10:40:00",
                "open_count": 1,
                "turnover_pct": 9.0,
                "reason_tags": ["机器人"],
            },
        ]
        pools = {
            dates[0]: [{
                "code": "sh600001",
                "name": "甲",
                "board_count": 1,
                "first_seal": None,
                "reason_tags": ["机器人"],
            }],
            dates[1]: current,
            dates[2]: [],
        }
        bars = {
            "sh600001": make_trade_bars(11.25, 11.25, 11.50),
            "sh600002": make_trade_bars(10.43, 10.80, 10.90),
            "sh600003": make_trade_bars(11.25, 11.25, 11.20),
        }
        members = [
            {"code": "sh600001", "name": "甲", "float_cap_yi": 50.0},
            {"code": "sh600002", "name": "乙", "float_cap_yi": 30.0},
            {"code": "sh600003", "name": "丙", "float_cap_yi": 40.0},
        ]
        result = MODULE.run_time_point(
            "10:30:00",
            pools,
            dates,
            {"机器人": "BK1001"},
            10000.0,
            {
                "commission_rate": 0.00025,
                "min_commission": 5.0,
                "stamp_duty_rate": 0.0005,
                "transfer_rate": 0.00001,
            },
            load_bars=lambda code: bars[code],
            load_members=lambda _board: members,
            min_theme_score=40,
            min_candidate_score=60,
        )
        self.assertEqual(result["groups"]["limit"]["summary"]["trade_count"], 1)
        self.assertEqual(
            result["groups"]["second_line"]["summary"]["trade_count"], 1)
        self.assertEqual(
            result["groups"]["limit"]["summary"]["initial_capital"], 10000.0)
        self.assertEqual(
            result["groups"]["second_line"]["summary"]["initial_capital"],
            10000.0,
        )


class BulkLoadTest(unittest.TestCase):
    def test_preload_bars_loads_each_missing_code_once(self):
        loaded = []

        def load_bars(code):
            loaded.append(code)
            return [{"date": "2026-01-25", "code": code}]

        bars, errors = MODULE.preload_bars(
            ["sh600001", "sh600002", "sh600001"],
            {},
            load_bars,
            workers=2,
        )

        self.assertEqual(set(bars), {"sh600001", "sh600002"})
        self.assertEqual(sorted(loaded), ["sh600001", "sh600002"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
