import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "limit-hit" / "scripts"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIMIT_UP = load_module("limit_hit_pool_suspension", "fetch_limit_up.py")
PREPARE = load_module("limit_hit_prepare_suspension", "prepare_candidates.py")


class SuspendedQuoteTest(unittest.TestCase):
    def test_zero_trade_current_quote_is_suspended_without_fake_gap(self):
        previous = [{
            "code": "sh605255",
            "name": "天普股份",
            "industry": "汽车零部",
            "board_count": 4,
            "amount_yi": 5.0,
        }]
        quotes = {
            "605255": {
                "price": 73.82,
                "prev_close": 73.82,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume_hands": 0.0,
                "pct": 0.0,
                "quote_date": "20261116",
            },
        }

        row = LIMIT_UP.prev_zt_today_rows(previous, quotes, set(), set())[0]

        self.assertTrue(row["suspended"])
        self.assertIsNone(row["today_open"])
        self.assertIsNone(row["gap_pct"])
        self.assertEqual(row["open_class"], "-")
        self.assertIsNone(row["today_pct"])
        self.assertEqual(row["close_state"], "停牌")
        self.assertFalse(row["continue_limit"])

    def test_missing_quote_is_not_classified_as_suspended(self):
        previous = [{"code": "sh600001", "name": "测试股份", "industry": "测试",
                     "board_count": 1, "amount_yi": 1.0}]

        row = LIMIT_UP.prev_zt_today_rows(previous, {}, set(), set())[0]

        self.assertFalse(row["suspended"])
        self.assertIsNone(row["gap_pct"])
        self.assertEqual(row["close_state"], "-")

    def test_stale_zero_trade_quote_is_not_classified_as_suspended(self):
        quote = {
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume_hands": 0.0,
            "quote_date": "20261113",
        }

        self.assertFalse(LIMIT_UP.quote_is_suspended(quote, "20261116"))

    def test_batch_quote_parser_preserves_volume_hands(self):
        fields = [""] * 49
        fields[1] = "天普股份"
        fields[2] = "605255"
        fields[3] = "73.82"
        fields[4] = "73.82"
        fields[5] = "0.00"
        fields[6] = "0"
        fields[30] = "20261116150000"
        fields[32] = "0.00"
        fields[33] = "0.00"
        fields[34] = "0.00"
        raw = 'v_sh605255="{}";'.format("~".join(fields)).encode("gbk")

        with mock.patch.object(LIMIT_UP, "http_get", return_value=raw):
            quote = LIMIT_UP.fetch_quotes(["sh605255"])["605255"]

        self.assertEqual(quote["volume_hands"], 0.0)
        self.assertEqual(quote["quote_date"], "20261116")
        self.assertEqual(quote["quote_time"], "20261116150000")

    def test_open_close_summary_excludes_suspended_samples(self):
        rows = [
            {"open_class": "高开", "gap_pct": 2.0, "today_pct": 3.0,
             "close_state": "上涨", "suspended": False},
            {"open_class": "低开", "gap_pct": -2.0, "today_pct": -3.0,
             "close_state": "下跌", "suspended": False},
            {"open_class": "-", "gap_pct": None, "today_pct": None,
             "close_state": "停牌", "suspended": True},
        ]

        summary = LIMIT_UP.open_close_summary(rows)

        self.assertEqual(summary["sample"], 3)
        self.assertEqual(summary["quoted"], 2)
        self.assertEqual(summary["missing_quote"], 0)
        self.assertEqual(summary["suspended"], 1)
        self.assertEqual(summary["up_open"] + summary["flat_open"] + summary["down_open"], 2)
        self.assertEqual(summary["avg_gap_pct"], 0.0)
        self.assertEqual(summary["median_gap_pct"], 0.0)
        self.assertEqual(summary["avg_pct"], 0.0)

    def test_text_report_can_classify_suspended_previous_limit(self):
        previous = [{
            "code": "sh605255",
            "name": "天普股份",
            "industry": "汽车零部",
            "board_count": 4,
            "amount_yi": 5.0,
        }]
        quotes = {
            "605255": {
                "price": 73.82,
                "prev_close": 73.82,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume_hands": 0.0,
                "pct": 0.0,
                "quote_date": "20261116",
            },
        }

        text = LIMIT_UP.render_text(
            datetime(2026, 11, 16), datetime(2026, 11, 13),
            [], [], [], previous, [], [], quotes, "20261116", {}, True, [])

        self.assertIn("停牌 1 只", text)

    def test_quote_time_before_auction_marks_previous_limit_unmatched(self):
        previous = [{
            "code": "sz002261",
            "name": "拓维信息",
            "industry": "软件服务",
            "board_count": 1,
            "amount_yi": 3.0,
        }]
        quotes = {
            "002261": {
                "price": 0.0,
                "prev_close": 32.82,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume_hands": 0.0,
                "pct": 0.0,
                "quote_date": "20261119",
                "quote_time": "20261119092340",
            },
        }

        row = LIMIT_UP.prev_zt_today_rows(
            previous, quotes, set(), set(), "20261118")[0]

        self.assertEqual(row["open_status"], "未撮合")
        self.assertIsNone(row["today_open"])
        self.assertIsNone(row["gap_pct"])
        self.assertEqual(row["open_class"], "-")

    def test_zero_open_without_quote_time_never_computes_negative_hundred(self):
        previous = [{
            "code": "sz002261",
            "name": "拓维信息",
            "industry": "软件服务",
            "board_count": 1,
            "amount_yi": 3.0,
        }]
        quotes = {
            "002261": {
                "price": 0.0,
                "prev_close": 32.82,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume_hands": 0.0,
                "pct": 0.0,
                "quote_date": "20261119",
            },
        }

        row = LIMIT_UP.prev_zt_today_rows(
            previous, quotes, set(), set(), "20261118")[0]

        self.assertEqual(row["open_status"], "未撮合")
        self.assertIsNone(row["gap_pct"])

    def test_open_close_summary_nulls_counts_when_auction_is_unmatched(self):
        rows = [
            {"open_status": "未撮合", "open_class": "-", "gap_pct": None,
             "today_pct": None, "close_state": "-", "suspended": False},
            {"open_status": "未撮合", "open_class": "-", "gap_pct": None,
             "today_pct": None, "close_state": "-", "suspended": False},
        ]

        summary = LIMIT_UP.open_close_summary(rows)

        self.assertEqual(summary["status"], "未撮合")
        self.assertEqual(summary["unmatched"], 2)
        self.assertIsNone(summary["up_open"])
        self.assertIsNone(summary["flat_open"])
        self.assertIsNone(summary["down_open"])
        self.assertIsNone(summary["avg_gap_pct"])
        self.assertIsNone(summary["median_gap_pct"])

    def test_text_report_says_auction_not_matched(self):
        previous = [{
            "code": "sz002261",
            "name": "拓维信息",
            "industry": "软件服务",
            "board_count": 1,
            "amount_yi": 3.0,
        }]
        quotes = {
            "002261": {
                "price": 0.0,
                "prev_close": 32.82,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume_hands": 0.0,
                "pct": 0.0,
                "quote_date": "20261119",
                "quote_time": "20261119092340",
            },
        }

        text = LIMIT_UP.render_text(
            datetime(2026, 11, 18), datetime(2026, 11, 17),
            [], [], [], previous, [], [], quotes, "20261119", {}, True, [])

        self.assertIn("开盘价未撮合", text)
        self.assertNotIn("-100", text)


class SuspensionFeedbackTest(unittest.TestCase):
    def test_mode_feedback_excludes_suspended_from_denominator(self):
        previous = [
            {"code": "sh600001", "board_count": 1},
            {"code": "sh600002", "board_count": 1},
        ]
        current = {
            "yesterday_performance": {
                "rows": [
                    {"code": "sh600001", "gap_pct": 2.0, "today_pct": 10.0,
                     "continue_limit": True, "suspended": False},
                    {"code": "sh600002", "gap_pct": None, "today_pct": None,
                     "continue_limit": False, "suspended": True},
                ],
            },
        }

        feedback = PREPARE.mode_feedback(previous, current)["one_to_two"]

        self.assertEqual(feedback["sample"], 1)
        self.assertEqual(feedback["suspended"], 1)
        self.assertEqual(feedback["promoted"], 1)
        self.assertEqual(feedback["promotion_rate_pct"], 100.0)

    def test_highest_board_keeps_suspension_audit_fields(self):
        previous = [{
            "code": "sh605255",
            "name": "天普股份",
            "board_count": 4,
            "industry": "汽车零部",
            "themes": ["新能源车", "名称组:羊字辈"],
        }]
        current = {
            "yesterday_performance": {
                "rows": [{
                    "code": "sh605255",
                    "today_open": None,
                    "gap_pct": None,
                    "today_pct": None,
                    "close_state": "停牌",
                    "continue_limit": False,
                    "suspended": True,
                }],
            },
        }

        stock = PREPARE.find_yesterday_high(previous, current)["stocks"][0]

        self.assertEqual(stock["industry"], "汽车零部")
        self.assertEqual(stock["themes"], ["新能源车", "名称组:羊字辈"])
        self.assertTrue(stock["suspended"])
        self.assertEqual(stock["close_state"], "停牌")


class SuspensionFilterTest(unittest.TestCase):
    def test_filter_excludes_same_industry_or_precise_concept_only(self):
        suspended_high = {
            "board_count": 4,
            "stocks": [{
                "code": "sh605255",
                "name": "天普股份",
                "board_count": 4,
                "industry": "汽车零部",
                "themes": ["电池技术", "新能源车", "名称组:羊字辈"],
                "suspended": True,
            }],
        }
        candidates = [
            {"code": "sh603358", "name": "华达科技", "industry": "汽车零部",
             "themes": ["新能源车"]},
            {"code": "sh605378", "name": "野马电池", "industry": "电池",
             "themes": ["电池技术"]},
            {"code": "sh600001", "name": "芯片股份", "industry": "半导体",
             "themes": ["芯片概念"]},
        ]

        result = PREPARE.suspension_filter(candidates, suspended_high)
        excluded = {row["code"]: row for row in result["excluded"]}

        self.assertEqual(result["excluded_count"], 2)
        self.assertEqual(set(excluded), {"sh603358", "sh605378"})
        self.assertTrue(excluded["sh603358"]["same_industry"])
        self.assertEqual(excluded["sh603358"]["matched_themes"], ["新能源车"])
        self.assertFalse(excluded["sh605378"]["same_industry"])
        self.assertEqual(excluded["sh605378"]["matched_themes"], ["电池技术"])

    def test_name_group_alone_does_not_trigger_exclusion(self):
        suspended_high = {
            "stocks": [{
                "code": "sh605255",
                "name": "天普股份",
                "board_count": 4,
                "industry": "汽车零部",
                "themes": ["名称组:羊字辈"],
                "suspended": True,
            }],
        }
        candidates = [{
            "code": "sh600001",
            "name": "羊羊股份",
            "industry": "通信设备",
            "themes": ["名称组:羊字辈"],
        }]

        result = PREPARE.suspension_filter(candidates, suspended_high)

        self.assertEqual(result["excluded_count"], 0)
        self.assertEqual(result["themes"], [])

    def test_no_suspended_high_leaves_candidates_unchanged(self):
        candidates = [{
            "code": "sh600001",
            "name": "测试股份",
            "industry": "通信设备",
            "themes": ["芯片概念"],
        }]
        high = {
            "stocks": [{
                "code": "sh600002",
                "name": "正常高标",
                "board_count": 4,
                "industry": "汽车零部",
                "themes": ["新能源车"],
                "suspended": False,
            }],
        }

        result = PREPARE.suspension_filter(candidates, high)

        self.assertEqual(result["excluded_count"], 0)
        self.assertEqual(result["suspended_stocks"], [])
        self.assertEqual(result["industries"], [])

    def test_multiple_highest_boards_only_suspended_stock_filters(self):
        high = {
            "stocks": [
                {
                    "code": "sh605255",
                    "name": "停牌高标",
                    "board_count": 4,
                    "industry": "汽车零部",
                    "themes": ["电池技术"],
                    "suspended": True,
                },
                {
                    "code": "sh600001",
                    "name": "正常高标",
                    "board_count": 4,
                    "industry": "通信设备",
                    "themes": ["芯片概念"],
                    "suspended": False,
                },
            ],
        }
        candidates = [
            {"code": "sh600002", "name": "汽车候选", "industry": "汽车零部",
             "themes": ["芯片概念"]},
            {"code": "sh600003", "name": "芯片候选", "industry": "半导体",
             "themes": ["芯片概念"]},
        ]

        result = PREPARE.suspension_filter(candidates, high)

        self.assertEqual(result["industries"], ["汽车零部"])
        self.assertEqual(result["themes"], ["电池技术"])
        self.assertEqual(
            [row["code"] for row in result["excluded"]], ["sh600002"])

    def test_theme_candidate_codes_remove_suspended_filter_exclusions(self):
        current = [
            {"code": "sh600001", "board_count": 1, "themes": ["机器人"]},
            {"code": "sh600002", "board_count": 2, "themes": ["机器人"]},
        ]

        themes = PREPARE.theme_summary(
            current, [], {}, excluded_codes={"sh600001"})

        self.assertEqual(themes[0]["current_count"], 2)
        self.assertEqual(themes[0]["candidate_codes"], ["sh600002"])

    def test_build_payload_applies_permission_then_suspension_before_metrics(self):
        current = {
            "base_date": "20261116",
            "prev_date": "20261113",
            "quote_date": "20261116",
            "overview": {},
            "quote_time": "20261116 09:42:00",
            "one_word": {},
            "zt_rows": [
                {"code": "sh605255", "name": "停牌高标", "board_count": 5,
                 "industry": "汽车零部", "themes": ["电池技术"]},
                {"code": "sh603358", "name": "华达科技", "board_count": 1,
                 "industry": "汽车零部", "themes": ["电池技术"]},
                {"code": "sh600386", "name": "北巴传媒", "board_count": 1,
                 "industry": "通信设备", "themes": ["芯片概念"]},
                {"code": "sh600999", "name": "芯片高标", "board_count": 3,
                 "industry": "半导体", "themes": ["芯片概念"]},
                {"code": "sz300001", "name": "创业候选", "board_count": 1,
                 "industry": "电池", "themes": ["电池技术"]},
            ],
            "yesterday_performance": {
                "rows": [{
                    "code": "sh605255",
                    "today_open": None,
                    "gap_pct": None,
                    "today_pct": None,
                    "close_state": "停牌",
                    "continue_limit": False,
                    "suspended": True,
                }],
            },
        }
        calls = []

        def fake_run_json(script, *args):
            calls.append((script, args))
            if script == "fetch_limit_up.py":
                return current
            if script == "fetch_stock_metrics.py":
                return {"rows": []}
            raise AssertionError(script)

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                path = PREPARE.build_limit_pool_path("2026-11-13")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "data_date": "2026-11-13",
                    "summary": {},
                    "stocks": [{
                        "code": "sh605255",
                        "name": "停牌高标",
                        "board_count": 4,
                        "industry": "汽车零部",
                        "themes": ["电池技术"],
                        "amount_yi": 5.0,
                    }],
                }, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(PREPARE, "run_json", side_effect=fake_run_json):
                    payload = PREPARE.build_payload()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(payload["board_permission_excluded_count"], 1)
        self.assertEqual(
            payload["market_gate"]["promotion_sample_time"],
            "20261116 09:42:00",
        )
        self.assertEqual(payload["suspension_filter"]["excluded_count"], 1)
        self.assertEqual(
            [row["code"] for row in payload["candidates"]], ["sh600386"])
        metrics_call = next(call for call in calls if call[0] == "fetch_stock_metrics.py")
        self.assertIn("sh600386", metrics_call[1])
        self.assertNotIn("sh603358", metrics_call[1])
        theme = next(row for row in payload["themes"] if row["theme"] == "芯片概念")
        self.assertEqual(theme["candidate_codes"], ["sh600386"])


if __name__ == "__main__":
    unittest.main()
