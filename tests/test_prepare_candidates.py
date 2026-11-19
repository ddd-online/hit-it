import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "limit-hit"
    / "scripts"
    / "prepare_candidates.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_candidates", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LimitPoolReportTest(unittest.TestCase):
    def test_builds_model_friendly_pool_summary(self):
        rows = [
            {
                "code": "sz002520",
                "name": "日发精机",
                "board_count": 1,
                "industry": "通用设备",
                "amount_yi": 4.03,
                "first_seal": "09:32:54",
                "themes": ["机器人概念"],
            },
            {
                "code": "sh600001",
                "name": "测试股份",
                "board_count": 2,
                "industry": "通用设备",
                "amount_yi": None,
                "first_seal": "09:25:00",
                "themes": ["机器人概念", "央国企改革"],
            },
        ]
        payload = MODULE.build_limit_pool_payload(
            data_date="2026-11-12",
            rows=rows,
            generated_at="2026-11-12T16:00:00+08:00",
            source="eastmoney-prev-pool",
            source_name="东方财富 push2ex 昨日涨停池",
            quote_date="20261112",
            market={"zt_count": 2},
            limitations=["测试限制"],
        )

        self.assertEqual(payload["schema"], "limit-hit.limit-pool.v1")
        self.assertEqual(payload["summary"]["count"], 2)
        self.assertEqual(payload["summary"]["max_board"], 2)
        self.assertEqual(payload["summary"]["themes"][0]["theme"], "机器人概念")
        self.assertEqual(payload["summary"]["themes"][0]["count"], 2)
        self.assertEqual(payload["market"], {"zt_count": 2})
        self.assertEqual(payload["stocks"][0]["pct"], None)

    def test_existing_pool_is_loaded_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "2026-11-12.json"
            payload = {
                "schema": "limit-hit.limit-pool.v1",
                "data_date": "2026-11-12",
                "stocks": [],
            }
            self.assertTrue(MODULE.write_limit_pool(path, payload))
            self.assertEqual(
                MODULE.load_limit_pool(path, "2026-11-12")["data_date"],
                "2026-11-12",
            )
            self.assertFalse(MODULE.write_limit_pool(path, {
                "data_date": "2026-11-12",
                "stocks": [{"code": "sh600000"}],
            }))
            self.assertEqual(MODULE.load_limit_pool(path, "2026-11-12")["stocks"], [])

    def test_previous_pool_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "2026-11-12.json"
            with self.assertRaisesRegex(RuntimeError, "save_limit_pool.py"):
                MODULE.require_limit_pool(path, "2026-11-12")

    def test_market_closed_requires_final_quote(self):
        self.assertTrue(MODULE.market_closed(
            datetime(2026, 11, 13, 15, 0), "2026-11-13", "20261113"))
        self.assertFalse(MODULE.market_closed(
            datetime(2026, 11, 13, 14, 59), "2026-11-13", "20261113"))
        self.assertFalse(MODULE.market_closed(
            datetime(2026, 11, 13, 16, 0), "2026-11-13", "20261112"))

    def test_runtime_fetches_current_pool_only(self):
        current = {
            "base_date": "20261113",
            "prev_date": "20261112",
            "quote_date": "20261113",
            "zt_rows": [],
            "overview": {},
        }
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                path = MODULE.build_limit_pool_path("2026-11-12")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "data_date": "2026-11-12",
                    "stocks": [],
                }), encoding="utf-8")
                with mock.patch.object(
                        MODULE, "run_json", return_value=current) as run:
                    payload = MODULE.build_payload()
                self.assertEqual(
                    run.call_args_list[0],
                    mock.call("fetch_limit_up.py", "--current-only", "--json"),
                )
                self.assertEqual(payload["data_date"], "2026-11-13")
                self.assertFalse(
                    MODULE.build_limit_pool_path("2026-11-13").exists())
            finally:
                os.chdir(old_cwd)


class ThemeAggregationTest(unittest.TestCase):
    def test_adds_name_emotion_groups(self):
        concepts = {
            "sh600123": [
                {"board_name": "机器人概念", "is_precise": True, "security_name": "东方精工"}
            ]
        }
        self.assertEqual(
            MODULE.themes_for("sh600123", concepts),
            ["机器人概念", "名称组:东方系"],
        )

    def test_theme_summary_counts_ladder(self):
        current = [
            {"code": "sh600001", "board_count": 1, "themes": ["机器人"]},
            {"code": "sh600002", "board_count": 2, "themes": ["机器人"]},
        ]
        previous = [
            {"code": "sh600001", "board_count": 1, "themes": ["机器人"]},
        ]
        themes = MODULE.theme_summary(current, previous, {})
        self.assertEqual(themes[0]["theme"], "机器人")
        self.assertEqual(themes[0]["current_count"], 2)
        self.assertEqual(themes[0]["promoted_count"], 1)
        self.assertEqual(themes[0]["max_board"], 2)


class SnapshotTest(unittest.TestCase):
    def test_minutes_since_first_seal(self):
        snapshot = datetime(2026, 11, 11, 10, 15, 0)
        self.assertEqual(MODULE.minutes_since(snapshot, "10:03:30"), 11)
        self.assertIsNone(MODULE.minutes_since(snapshot, "10:20:00"))

    def test_report_paths_use_data_date(self):
        path = MODULE.build_report_path("2026-11-11")
        pool_path = MODULE.build_limit_pool_path("2026-11-11")
        self.assertEqual(path.name, "2026-11-11.md")
        self.assertEqual(path.parent.name, "打板候选")
        self.assertEqual(pool_path.name, "2026-11-11.json")
        self.assertEqual(pool_path.parent.name, "涨停池")

    def test_stock_report_path_is_separate_from_batch_report(self):
        path = MODULE.build_stock_report_path("2026-11-11", "sz002520")
        self.assertEqual(path.name, "2026-11-11_sz002520.md")
        self.assertEqual(path.parent.name, "个股审视")


class StockReviewTest(unittest.TestCase):
    def test_normalizes_bare_and_suffixed_stock_codes(self):
        self.assertEqual(MODULE.normalize_stock_code("002520"), "sz002520")
        self.assertEqual(MODULE.normalize_stock_code("600001"), "sh600001")
        self.assertEqual(MODULE.normalize_stock_code("002520.SZ"), "sz002520")
        self.assertEqual(MODULE.normalize_stock_code("sh600001"), "sh600001")

    def test_resolves_exact_stock_name_without_guessing(self):
        suggestions = [
            {"code": "sz002520", "name": "日发精机"},
            {"code": "sh600001", "name": "日发精工"},
        ]
        result = MODULE.resolve_stock_query(
            "日发精机", suggestions_provider=lambda query: suggestions)
        self.assertEqual(result["code"], "sz002520")
        self.assertEqual(result["name"], "日发精机")

    def test_parses_unicode_escaped_tencent_suggestion(self):
        rows = MODULE.parse_stock_suggestions(
            'v_hint="sz~000001~\\u5e73\\u5b89\\u94f6\\u884c~payh~GP-A"')
        self.assertEqual(rows, [{"code": "sz000001", "name": "平安银行"}])

    def test_parses_eastmoney_suggestion_fallback(self):
        rows = MODULE.parse_eastmoney_suggestions(
            '{"QuotationCodeTable":{"Data":['
            '{"Code":"000001","Name":"平安银行"},'
            '{"Code":"600001","Name":"邯郸钢铁"}]}}'
        )
        self.assertEqual(rows, [
            {"code": "sz000001", "name": "平安银行"},
            {"code": "sh600001", "name": "邯郸钢铁"},
        ])

    def test_rejects_ambiguous_stock_name(self):
        suggestions = [
            {"code": "sz002520", "name": "日发精机"},
            {"code": "sh600001", "name": "日发精机"},
        ]
        with self.assertRaisesRegex(ValueError, "名称不唯一"):
            MODULE.resolve_stock_query(
                "日发精机", suggestions_provider=lambda query: suggestions)

    def test_builds_non_limit_stock_review_facts(self):
        snapshot = datetime(2026, 11, 17, 10, 30)
        current = {
            "quote_date": "20261117",
            "zt_rows": [],
            "zb_all_rows": [],
            "dt_rows": [],
            "yesterday_performance": {"rows": []},
        }
        quote_payload = {
            "quote": {
                "code": "sz002520",
                "name": "日发精机",
                "price": 21.0,
                "prev_close": 20.0,
                "open": 20.8,
                "high": 21.2,
                "low": 20.7,
                "change_pct": 5.0,
                "volume_hands": 600000,
                "turnover_pct": 9.0,
                "datetime": "20261117103000",
            },
            "kline": [
                {"date": "2026-11-14", "volume": 400000},
                {"date": "2026-11-17", "volume": 600000},
            ],
        }
        metrics = {
            "data_date": "2026-11-17",
            "last": {"close": 21.0, "amount_yuan": 126000000},
            "open_gap_pct": 4.0,
            "low_gap_pct": 3.5,
            "volume_ratio_5d": 1.5,
            "chips": {"profit_ratio_pct": 72.0},
            "quality": "exact",
        }
        review = MODULE.build_stock_review(
            snapshot=snapshot,
            data_date="2026-11-17",
            query="日发精机",
            stock={"code": "sz002520", "name": "日发精机"},
            current=current,
            previous_rows=[],
            concepts={},
            quote_payload=quote_payload,
            metrics=metrics,
            structure={"holders": [], "limitations": []},
        )
        self.assertFalse(review["in_current_limit_pool"])
        self.assertEqual(review["current_limit_status"], "上涨")
        self.assertTrue(review["intraday"]["is_partial_day"])
        self.assertEqual(review["intraday"]["volume_vs_prev_day"], 1.5)
        self.assertEqual(review["intraday"]["price_vs_open_pct"], 0.96)
        self.assertEqual(review["quote_date"], "2026-11-17")
        self.assertTrue(review["quote_consistent"])

    def test_zero_quote_volume_is_not_replaced_by_metric_volume(self):
        snapshot = datetime(2026, 11, 17, 10, 30)
        quote_payload = {
            "quote": {
                "price": 20.0,
                "prev_close": 20.0,
                "open": 0,
                "high": 0,
                "low": 0,
                "volume_hands": 0,
                "turnover_pct": 0,
                "datetime": "20261117103000",
            },
            "kline": [{"date": "2026-11-14", "volume": 400000}],
        }
        review = MODULE.build_stock_review(
            snapshot=snapshot,
            data_date="2026-11-17",
            query="停牌测试",
            stock={"code": "sh600001", "name": "停牌测试"},
            current={"zt_rows": [], "zb_all_rows": [], "dt_rows": []},
            previous_rows=[],
            concepts={},
            quote_payload=quote_payload,
            metrics={"last": {"volume": 999999, "turnover_pct": 12.0}},
            structure={},
        )
        self.assertEqual(review["current_limit_status"], "停牌")
        self.assertEqual(review["intraday"]["volume_vs_prev_day"], 0.0)
        self.assertEqual(review["intraday"]["turnover_pct"], 0)

    def test_stale_quote_makes_holding_data_inconsistent(self):
        snapshot = datetime(2026, 11, 17, 10, 30)
        review = MODULE.build_stock_review(
            snapshot=snapshot,
            data_date="2026-11-17",
            query="日发精机",
            stock={"code": "sz002520", "name": "日发精机"},
            current={"zt_rows": [], "zb_all_rows": [], "dt_rows": []},
            previous_rows=[],
            concepts={},
            quote_payload={"quote": {
                "price": 21.0,
                "open": 20.8,
                "high": 21.2,
                "low": 20.7,
                "volume_hands": 600000,
                "change_pct": 5.0,
                "datetime": "20261114150000",
            }, "kline": []},
            metrics={},
            structure={},
        )
        self.assertEqual(review["quote_date"], "2026-11-14")
        self.assertFalse(review["quote_consistent"])

    def test_stock_mode_fetches_target_outside_limit_pool(self):
        current = {
            "base_date": "20261117",
            "prev_date": "20261114",
            "quote_date": "20261117",
            "zt_rows": [],
            "zb_all_rows": [],
            "dt_rows": [],
            "yesterday_performance": {"rows": []},
            "overview": {},
            "yesterday_open_close": {"overall": {}},
        }
        quote_payload = {
            "quote": {
                "code": "sz002520",
                "name": "日发精机",
                "price": 21.0,
                "prev_close": 20.0,
                "open": 20.8,
                "high": 21.2,
                "low": 20.7,
                "change_pct": 5.0,
                "volume_hands": 600000,
                "turnover_pct": 9.0,
                "datetime": "20261117103000",
            },
            "kline": [{"date": "2026-11-14", "volume": 400000}],
        }
        metrics_payload = {
            "data_date": "2026-11-17",
            "rows": [{
                "code": "sz002520",
                "data_date": "2026-11-17",
                "last": {"close": 21.0, "volume": 600000},
                "volume_ratio_5d": 1.5,
                "chips": {"profit_ratio_pct": 72.0},
                "quality": "exact",
            }],
        }
        structure_payload = {
            "code": "sz002520",
            "name": "日发精机",
            "holders": [],
            "chips": {"profit_ratio_pct": 72.0},
            "concepts": [
                {"board_name": "机器人概念", "is_precise": True,
                 "security_name": "日发精机"}
            ],
            "limitations": [],
        }

        def fake_run_json(script, *args):
            if script == "fetch_limit_up.py":
                return current
            if script == "fetch_quote.py":
                return quote_payload
            if script == "fetch_stock_metrics.py":
                return metrics_payload
            if script == "fetch_stock_structure.py":
                return structure_payload
            raise AssertionError("unexpected script: {}".format(script))

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                path = MODULE.build_limit_pool_path("2026-11-14")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "data_date": "2026-11-14",
                    "stocks": [],
                    "summary": {},
                }), encoding="utf-8")
                with mock.patch.object(MODULE, "run_json", side_effect=fake_run_json):
                    payload = MODULE.build_payload(stock_query="002520")
                review = payload["stock_review"]
                self.assertFalse(review["in_current_limit_pool"])
                self.assertEqual(review["themes"], ["机器人概念"])
                self.assertEqual(
                    pathlib.Path(payload["report_path"]).parent.name, "个股审视")
                compact = MODULE.build_compact_payload(payload)
                self.assertEqual(
                    compact["schema"], "limit-hit.stock-review.compact.v1")
                self.assertEqual(compact["stock_review"]["code"], "sz002520")
                self.assertEqual(
                    compact["metric_quality_summary"], {"exact": 1})
            finally:
                os.chdir(old_cwd)

    def test_stock_cli_passes_name_query_to_builder(self):
        with mock.patch.object(
                MODULE, "build_payload", return_value={}) as build_payload, \
                mock.patch.object(
                    sys, "argv",
                    ["prepare_candidates.py", "--stock", "日发精机", "--json"]), \
                redirect_stdout(io.StringIO()):
            MODULE.main()
        build_payload.assert_called_once_with(
            stock_query="日发精机")

    def test_profile_cli_option_is_removed(self):
        with mock.patch.object(
                sys, "argv",
                ["prepare_candidates.py", "--profile", "legacy"]):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    MODULE.main()

        self.assertEqual(raised.exception.code, 2)

    def test_stock_review_rejects_non_mainboard_code(self):
        with self.assertRaisesRegex(RuntimeError, "仅支持沪深主板"):
            MODULE.build_payload(stock_query="300001")


class CompactPayloadTest(unittest.TestCase):
    def test_compact_payload_flattens_candidate_metrics(self):
        payload = {
            "generated_at": "2026-11-13T16:00:00+08:00",
            "data_date": "2026-11-13",
            "quote_date": "20261113",
            "previous_date": "2026-11-12",
            "previous_source": "涨停池报告",
            "themes": [{"theme": "机器人", "current_count": 2}],
            "candidates": [{
                "code": "sz002520",
                "name": "日发精机",
                "board_count": 1,
                "pct": 9.991234,
                "themes": ["机器人"],
                "metrics": {
                    "source": "东方财富日K（精确换手率）",
                    "returns_pct": {"5d": 12.345},
                    "ma": {"5": 10.1234},
                    "chips": {"profit_ratio_pct": 88.888},
                },
            }],
            "limitations": ["仅计算"],
        }
        compact = MODULE.build_compact_payload(payload)
        candidate = compact["candidates"][0]
        self.assertEqual(compact["schema"], "limit-hit.candidates.compact.v1")
        self.assertEqual(candidate["pct"], 9.99)
        self.assertEqual(candidate["returns_pct"]["5d"], 12.35)
        self.assertEqual(candidate["ma"]["5"], 10.123)
        self.assertEqual(candidate["chips"]["profit_ratio_pct"], 88.89)
        self.assertNotIn("metrics", candidate)

    def test_compact_payload_exposes_hardcoded_rules_and_market_gate(self):
        payload = {
            "quote_time": "2026-11-19 09:42:00",
            "rules": {"one_word_executable": True},
            "market_gate": {"state": "偏弱", "action": "max_one"},
            "themes": [],
            "candidates": [],
        }

        compact = MODULE.build_compact_payload(payload)

        self.assertTrue(compact["rules"]["one_word_executable"])
        self.assertEqual(compact["market_gate"]["action"], "max_one")
        self.assertEqual(compact["quote_time"], "2026-11-19 09:42:00")


class RulePipelineTest(unittest.TestCase):
    def test_one_word_filter_changes_candidates_and_theme_codes(self):
        current = {
            "base_date": "20261118",
            "prev_date": "20261117",
            "quote_date": "20261118",
            "overview": {
                "zt_count": 32,
                "dt_count": 21,
                "zb_count": 19,
                "seal_rate_pct": 62.7,
                "max_board": 4,
            },
            "one_word": {"sh600001": True},
            "zt_rows": [
                {"code": "sh600001", "name": "一字候选", "board_count": 1,
                 "amount_yi": 0.34, "seal_fund_yi": 1.29,
                 "float_cap_yi": 10.0, "themes": ["机器人"]},
                {"code": "sh600002", "name": "普通候选", "board_count": 1,
                 "amount_yi": 2.0, "seal_fund_yi": 0.4,
                 "float_cap_yi": 20.0, "themes": ["机器人"]},
            ],
            "yesterday_performance": {"rows": []},
        }

        def fake_run_json(script, *args):
            if script == "fetch_limit_up.py":
                return current
            if script == "fetch_stock_metrics.py":
                return {"rows": []}
            raise AssertionError(script)

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                path = MODULE.build_limit_pool_path("2026-11-17")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "data_date": "2026-11-17",
                    "summary": {},
                    "stocks": [],
                }), encoding="utf-8")
                with mock.patch.object(
                        MODULE, "run_json", side_effect=fake_run_json):
                    result = MODULE.build_payload()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(
            [row["code"] for row in result["candidates"]],
            ["sh600001", "sh600002"],
        )
        self.assertTrue(result["candidates"][0]["one_word"])
        self.assertEqual(
            result["themes"][0]["candidate_codes"],
            ["sh600001", "sh600002"],
        )
        self.assertEqual(result["market_gate"]["action"], "max_one")
        self.assertNotIn("candidates", result["one_word_filter"])


if __name__ == "__main__":
    unittest.main()
