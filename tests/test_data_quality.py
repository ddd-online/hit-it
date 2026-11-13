import importlib.util
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "limit-hit" / "scripts"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HISTORY = load_module("limit_hit_history_quality", "fetch_limit_up_history.py")
BACKTEST = load_module("limit_hit_backtest_quality", "backtest_limit_hit.py")
LIMIT_UP = load_module("limit_hit_pool_quality", "fetch_limit_up.py")
PREPARE = load_module("limit_hit_prepare_quality", "prepare_candidates.py")
FLOW = load_module("limit_hit_flow_quality", "fetch_capital_flow.py")
FALLBACK = load_module("limit_hit_fallback_quality", "fallback_sources.py")
LEADERS = load_module("limit_hit_leaders_quality", "fetch_sector_leaders.py")
BOARDS = load_module("limit_hit_boards_quality", "fetch_sector_boards.py")
STRUCTURE = load_module("limit_hit_structure_quality", "fetch_stock_structure.py")
METRICS = load_module("limit_hit_metrics_quality", "fetch_stock_metrics.py")


class HistoryBoardSemanticsTest(unittest.TestCase):
    def test_window_limit_count_is_not_consecutive_board_count(self):
        row = HISTORY.normalize_row({
            "code": "601188",
            "name": "龙江交通",
            "high_days_value": 393227,
            "reason_type": "冰雪旅游保障+高速公路运营",
        })

        self.assertEqual(row["board_days"], 11)
        self.assertEqual(row["board_window_count"], 6)
        self.assertIsNone(row["board_count"])

    def test_equal_window_days_and_limit_count_proves_consecutive_board(self):
        row = HISTORY.normalize_row({
            "code": "002623",
            "name": "亚玛顿",
            "high_days_value": 4 * 65536 + 4,
            "reason_type": "光伏",
        })

        self.assertEqual(row["board_window"], None)
        self.assertEqual(row["board_window_count"], 4)
        self.assertEqual(row["board_count"], 4)


class BacktestBoardDerivationTest(unittest.TestCase):
    def test_board_count_comes_from_consecutive_pool_membership(self):
        dates = [
            "2026-11-10",
            "2026-11-11",
            "2026-11-12",
            "2026-11-13",
        ]
        pools = {
            dates[0]: [],
            dates[1]: [
                {"code": "sh600001", "board_window_count": 3},
            ],
            dates[2]: [
                {"code": "sh600001", "board_window_count": 4},
                {"code": "sh600002", "board_window_count": 2},
            ],
            dates[3]: [
                {"code": "sh600001", "board_window_count": 5},
                {"code": "sh600002", "board_window_count": 3},
                {"code": "sh600003", "board_window_count": 2},
            ],
        }

        BACKTEST.apply_consecutive_board_counts(pools, dates)

        self.assertEqual(pools[dates[1]][0]["board_count"], 1)
        self.assertEqual(pools[dates[2]][0]["board_count"], 2)
        self.assertEqual(pools[dates[2]][1]["board_count"], 1)
        self.assertEqual(pools[dates[3]][0]["board_count"], 3)
        self.assertEqual(pools[dates[3]][1]["board_count"], 2)
        self.assertEqual(pools[dates[3]][2]["board_count"], 1)

    def test_missing_previous_pool_does_not_invent_one_board(self):
        dates = ["2026-11-10", "2026-11-11"]
        pools = {
            dates[1]: [{"code": "sh600001", "board_window_count": 1}],
        }

        BACKTEST.apply_consecutive_board_counts(pools, dates)

        self.assertIsNone(pools[dates[1]][0]["board_count"])

    def test_exact_history_window_beats_incomplete_pool_membership(self):
        dates = ["2026-11-11", "2026-11-12"]
        pools = {
            dates[0]: [{"code": "sz002623", "board_count": 3,
                        "board_window_count": 3}],
            dates[1]: [{"code": "sz002623", "board_count": 4,
                        "board_window_count": 4}],
        }

        BACKTEST.apply_consecutive_board_counts(pools, dates)

        self.assertEqual(pools[dates[1]][0]["board_count"], 4)

    def test_requested_window_keeps_previous_trading_days_for_board_count(self):
        bars = {
            day: {"date": day}
            for day in (
                "2026-11-06",
                "2026-11-09",
                "2026-11-10",
                "2026-11-11",
                "2026-11-12",
                "2026-11-13",
            )
        }
        with mock.patch.object(BACKTEST, "fetch_raw_kline", return_value=bars):
            requested, warmup, _ = BACKTEST.get_trading_dates(
                datetime(2026, 11, 11).date(),
                datetime(2026, 11, 13).date(),
                lookback_days=2,
            )

        self.assertEqual(warmup, ["2026-11-09", "2026-11-10"])
        self.assertEqual(requested, ["2026-11-11", "2026-11-12", "2026-11-13"])


class PoolFieldTruthTest(unittest.TestCase):
    def test_zero_break_count_stays_zero(self):
        row = LIMIT_UP.normalize_pool_row({
            "c": "001358",
            "n": "兴欣新材",
            "m": "0",
            "p": 26640,
            "zbc": 0,
            "lbc": 2,
        })

        self.assertEqual(row["break_count"], 0)

    def test_missing_quote_date_stays_missing(self):
        self.assertIsNone(LIMIT_UP.resolve_quote_date({}))
        self.assertEqual(
            LIMIT_UP.resolve_quote_date({
                "000001": {"quote_date": "20261113"},
                "600000": {"quote_date": "20261112"},
            }),
            "20261113",
        )


class FeedbackStatsTest(unittest.TestCase):
    def test_mode_feedback_groups_yesterday_board_count(self):
        previous = [
            {"code": "sh600001", "board_count": 1},
            {"code": "sh600002", "board_count": 1},
            {"code": "sh600003", "board_count": 2},
        ]
        current = {
            "yesterday_performance": {
                "rows": [
                    {"code": "sh600001", "gap_pct": 2.0, "today_pct": 10.0,
                     "continue_limit": True},
                    {"code": "sh600002", "gap_pct": -1.0, "today_pct": -2.0,
                     "continue_limit": False},
                    {"code": "sh600003", "gap_pct": 3.0, "today_pct": 4.0,
                     "continue_limit": False},
                ],
            },
        }

        feedback = PREPARE.mode_feedback(previous, current)

        one_to_two = feedback["one_to_two"]
        two_to_three = feedback["two_to_three"]
        self.assertEqual(one_to_two["sample"], 2)
        self.assertEqual(one_to_two["promoted"], 1)
        self.assertEqual(one_to_two["promotion_rate_pct"], 50.0)
        self.assertEqual(one_to_two["high_open_rate_pct"], 50.0)
        self.assertEqual(one_to_two["avg_open_pct"], 0.5)
        self.assertEqual(two_to_three["sample"], 1)
        self.assertEqual(two_to_three["promotion_rate_pct"], 0.0)
        self.assertEqual(two_to_three["high_open_rate_pct"], 100.0)

    def test_current_board_structure_counts_two_plus_limit_ups(self):
        stats = LIMIT_UP.board_structure([
            {"board_count": 1},
            {"board_count": 2},
            {"board_count": 2},
            {"board_count": 4},
            {"board_count": None},
        ])

        self.assertEqual(stats["max_board"], 4)
        self.assertEqual(stats["two_plus_count"], 3)
        self.assertEqual(stats["board_counts"], {"1": 1, "2": 2, "4": 1, "未获取": 1})
        self.assertEqual(LIMIT_UP.board_bucket(None), "未获取")

    def test_compact_payload_keeps_mode_feedback(self):
        payload = {
            "generated_at": "2026-11-13T16:00:00+08:00",
            "data_date": "2026-11-13",
            "mode_feedback": {"one_to_two": {"sample": 2}},
            "themes": [],
            "candidates": [],
        }

        compact = PREPARE.build_compact_payload(payload)

        self.assertEqual(compact["mode_feedback"]["one_to_two"]["sample"], 2)


class MarketClockTest(unittest.TestCase):
    def test_market_close_uses_shanghai_time(self):
        utc_close = datetime(2026, 11, 13, 7, 0, tzinfo=timezone.utc)
        self.assertTrue(
            PREPARE.market_closed(utc_close, "2026-11-13", "20261113")
        )


class CapitalFlowQualityTest(unittest.TestCase):
    def test_auto_source_falls_back_when_five_day_series_is_incomplete(self):
        one_day = {
            "name": "测试",
            "latest_day": {"date": "2026-11-13", "main": 10.0},
            "main_5d": [{"date": "2026-11-13", "main": 10.0}],
        }
        five_days = {
            "name": "测试",
            "latest_day": {"date": "2026-11-13", "main": 11.0},
            "main_5d": [
                {"date": f"2026-11-{day:02d}", "main": float(day)}
                for day in (9, 10, 11, 12, 13)
            ],
        }

        with mock.patch.object(FLOW, "fetch", return_value=one_day), \
                mock.patch.object(FLOW, "fetch_sina", return_value=five_days):
            payload = FLOW.build_payload("sh600001", "auto")

        self.assertEqual(payload["source"], "sina")
        self.assertTrue(payload["main_5d_complete"])
        self.assertEqual(payload["main_5d_available_days"], 5)
        self.assertIn("东财", payload["fallback_reason"])


class SectorPeriodQualityTest(unittest.TestCase):
    def test_duplicate_gain5_and_gain10_is_marked_unavailable(self):
        rows = [
            {"change5_pct": 1.14, "change10_pct": 1.14},
            {"change5_pct": 2.50, "change10_pct": 2.50},
        ]

        changed = FALLBACK.clear_duplicate_gain10(rows)

        self.assertTrue(changed)
        self.assertEqual(rows[0]["change10_pct"], None)
        self.assertEqual(rows[1]["change10_pct"], None)

    def test_multiple_theme_searches_share_one_filtered_result(self):
        rows = [
            {"name": "影视概念"},
            {"name": "人工智能"},
            {"name": "机器人概念"},
        ]

        filtered = BOARDS.filter_rows(rows, 0, ["影视概念", "人工智能"])

        self.assertEqual(
            [row["name"] for row in filtered],
            ["影视概念", "人工智能"],
        )


class MetricQualityTest(unittest.TestCase):
    def test_fallback_kline_is_marked_degraded(self):
        self.assertEqual(
            METRICS.metric_quality("腾讯日K（换手率≈）", ["近似换手"]),
            "degraded",
        )
        self.assertEqual(
            METRICS.metric_quality("东方财富日K（精确换手率）", []),
            "exact",
        )

    def test_eastmoney_kline_failure_opens_circuit(self):
        self.addCleanup(
            setattr, STRUCTURE, "_EASTMONEY_KLINE_UNAVAILABLE", False
        )
        bars = [
            ["2026-11-12", "10", "10", "10", "10", "1000"],
            ["2026-11-13", "10", "11", "11", "10", "1500"],
        ]
        STRUCTURE._EASTMONEY_KLINE_UNAVAILABLE = False
        with mock.patch.object(STRUCTURE, "http_get", side_effect=OSError("closed")) as eastmoney, \
                mock.patch.object(
                    STRUCTURE.fallback_sources, "fetch_tencent_kline",
                    return_value=bars), \
                mock.patch.object(
                    STRUCTURE.fallback_sources, "fetch_tencent_quotes",
                    return_value={"sh600001": {
                        "price": 11.0, "float_cap_yi": 10.0, "name": "测试",
                    }}):
            STRUCTURE.fetch_kline("sh600001", 2, None)
            STRUCTURE.fetch_kline("sh600002", 2, None)

        self.assertEqual(eastmoney.call_count, 1)
        self.assertTrue(STRUCTURE._EASTMONEY_KLINE_UNAVAILABLE)

    def test_compact_candidate_exposes_metric_quality(self):
        payload = {
            "themes": [],
            "candidates": [{
                "code": "sh600001",
                "metrics": {
                    "source": "腾讯日K（换手率≈）",
                    "quality": "degraded",
                    "limitations": ["近似换手"],
                },
            }],
        }

        compact = PREPARE.build_compact_payload(payload)

        self.assertEqual(compact["candidates"][0]["metric_quality"], "degraded")


class PoolFetchPerformanceTest(unittest.TestCase):
    def test_related_pools_are_fetched_once_without_base_dates(self):
        def fake_fetch(kind, date):
            return {"kind": kind, "date": date}

        with mock.patch.object(LIMIT_UP, "fetch_pool", side_effect=fake_fetch) as fetch:
            result = LIMIT_UP.fetch_related_pools("20261113", "20261112")

        self.assertCountEqual(
            fetch.call_args_list,
            [
                mock.call("dt", "20261113"),
                mock.call("zb", "20261113"),
                mock.call("dt", "20261112"),
                mock.call("zb", "20261112"),
            ],
        )
        self.assertEqual(result["prev_dt"]["kind"], "dt")


class CompactOutputTest(unittest.TestCase):
    def test_compact_output_caps_theme_list_and_reports_total(self):
        payload = {
            "themes": [
                {"theme": "题材{}".format(index)}
                for index in range(35)
            ],
            "candidates": [],
        }

        compact = PREPARE.build_compact_payload(payload, theme_limit=30)

        self.assertEqual(len(compact["themes"]), 30)
        self.assertEqual(compact["theme_total_count"], 35)
        self.assertTrue(compact["themes_truncated"])

    def test_compact_output_keeps_metric_quality_summary(self):
        payload = {
            "themes": [],
            "candidates": [],
            "metric_quality_summary": {"exact": 2, "degraded": 3},
        }

        compact = PREPARE.build_compact_payload(payload)

        self.assertEqual(
            compact["metric_quality_summary"],
            {"exact": 2, "degraded": 3},
        )


class BoardPermissionTest(unittest.TestCase):
    def test_candidates_are_limited_to_hs_mainboard(self):
        rows = [
            {"code": "sh600001", "board_count": 1},
            {"code": "sz000001", "board_count": 2},
            {"code": "sz300001", "board_count": 1},
            {"code": "sh688001", "board_count": 1},
            {"code": "bj920001", "board_count": 1},
            {"code": "sh600002", "board_count": 3},
        ]

        selected = PREPARE.select_candidates(rows)

        self.assertEqual(
            [row["code"] for row in selected],
            ["sh600001", "sz000001"],
        )

    def test_theme_candidate_codes_follow_board_permission(self):
        current = [
            {"code": "sh600001", "board_count": 1, "themes": ["机器人"]},
            {"code": "sz300001", "board_count": 1, "themes": ["机器人"]},
        ]

        themes = PREPARE.theme_summary(current, [], {})

        self.assertEqual(themes[0]["current_codes"], ["sh600001", "sz300001"])
        self.assertEqual(themes[0]["candidate_codes"], ["sh600001"])


if __name__ == "__main__":
    unittest.main()
