import importlib.util
import pathlib
import tempfile
import unittest
from datetime import datetime


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "limit-hit"
    / "scripts"
    / "prepare_candidates.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_candidates", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReportParsingTest(unittest.TestCase):
    def test_parses_previous_limit_find_report(self):
        text = """# 涨停板复盘 · 2026-11-10

- 主线：机器人、新能源车
- 阵型：轮动期
- 结构说明：崩在哪——无；抬在哪——机器人

## 涨停全名单（供次日竞价评估）

| 代码 | 名称 | 板数 | 行业 | 成交额(亿) | 首封时间 | 备注 |
|---|---|---|---|---:|---|---|
| sz002520 | 日发精机 | 1 | 通用设备 | 4.03 | 09:32:54 | - |
"""
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "2026-11-10.md"
            path.write_text(text, encoding="utf-8")
            payload = MODULE.parse_report(path)
        self.assertEqual(payload["data_date"], "2026-11-10")
        self.assertEqual(payload["rows"][0]["code"], "sz002520")
        self.assertEqual(payload["rows"][0]["board_count"], 1)
        self.assertIn("机器人", payload["mainline"])


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
            {"code": "sh600001", "board_count": 1},
            {"code": "sh600002", "board_count": 2},
        ]
        previous = [{"code": "sh600001", "board_count": 1}]
        concepts = {
            "sh600001": [{"board_name": "机器人", "is_precise": True, "security_name": "甲"}],
            "sh600002": [{"board_name": "机器人", "is_precise": True, "security_name": "乙"}],
        }
        themes = MODULE.theme_summary(current, previous, concepts)
        self.assertEqual(themes[0]["theme"], "机器人")
        self.assertEqual(themes[0]["current_count"], 2)
        self.assertEqual(themes[0]["promoted_count"], 1)
        self.assertEqual(themes[0]["max_board"], 2)


class SnapshotTest(unittest.TestCase):
    def test_minutes_since_first_seal(self):
        snapshot = datetime(2026, 11, 11, 10, 15, 0)
        self.assertEqual(MODULE.minutes_since(snapshot, "10:03:30"), 11)
        self.assertIsNone(MODULE.minutes_since(snapshot, "10:20:00"))

    def test_report_path_uses_data_date(self):
        path = MODULE.build_report_path("2026-11-11")
        self.assertEqual(path.name, "2026-11-11.md")
        self.assertEqual(path.parent.name, "打板候选")


class CompactPayloadTest(unittest.TestCase):
    def test_compact_payload_flattens_candidate_metrics(self):
        payload = {
            "generated_at": "2026-11-13T16:00:00+08:00",
            "data_date": "2026-11-13",
            "quote_date": "20261113",
            "previous_date": "2026-11-12",
            "previous_source": "limit-find-report",
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
