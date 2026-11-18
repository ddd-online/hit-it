import importlib.util
import pathlib
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


if __name__ == "__main__":
    unittest.main()
