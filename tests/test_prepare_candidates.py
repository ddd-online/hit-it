import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
