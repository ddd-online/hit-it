import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "limit-hit" / "scripts"
PREPARE_PATH = SCRIPT_DIR / "prepare_candidates.py"
SPEC = importlib.util.spec_from_file_location("limit_hit_prepare_candidates", PREPARE_PATH)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)
HTTP_PATH = SCRIPT_DIR / "http_util.py"
HTTP_SPEC = importlib.util.spec_from_file_location("limit_hit_http_util", HTTP_PATH)
HTTP_UTIL = importlib.util.module_from_spec(HTTP_SPEC)
HTTP_SPEC.loader.exec_module(HTTP_UTIL)
LEADERS_PATH = SCRIPT_DIR / "fetch_sector_leaders.py"
LEADERS_SPEC = importlib.util.spec_from_file_location(
    "limit_hit_sector_leaders", LEADERS_PATH)
LEADERS = importlib.util.module_from_spec(LEADERS_SPEC)
LEADERS_SPEC.loader.exec_module(LEADERS)
BOARDS_PATH = SCRIPT_DIR / "fetch_sector_boards.py"
BOARDS_SPEC = importlib.util.spec_from_file_location(
    "limit_hit_sector_boards", BOARDS_PATH)
BOARDS = importlib.util.module_from_spec(BOARDS_SPEC)
BOARDS_SPEC.loader.exec_module(BOARDS)


class SelfContainedScriptsTest(unittest.TestCase):
    def test_skill_files_contain_no_absolute_paths(self):
        pattern = re.compile(
            r"(?:[A-Za-z]:\\|/(?:Users|home)/|file://)", re.IGNORECASE)
        for path in (
            ROOT / "limit-hit" / "SKILL.md",
            ROOT / "leader-hit" / "SKILL.md",
        ):
            with self.subTest(path=path):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_open_url_uses_direct_opener_only(self):
        response = object()
        with mock.patch.object(
                HTTP_UTIL._DIRECT_OPENER, "open", return_value=response) as direct_open:
            result = HTTP_UTIL.open_url("request", timeout=7)

        self.assertIs(result, response)
        direct_open.assert_called_once_with("request", timeout=7)

    def test_compact_sector_payload_summarizes_rows(self):
        board = {"code": "BK0001", "name": "测试板块"}
        rows = [
            {"code": "sh600001", "name": "甲", "change_pct": 10.0,
             "main_yuan": 100000000, "limit_up": True},
            {"code": "sh600002", "name": "乙", "change_pct": -2.0,
             "main_yuan": -50000000, "limit_up": False},
            {"code": "sh600003", "name": "丙", "change_pct": 3.0,
             "main_yuan": None, "limit_up": False},
        ]
        payload = LEADERS.build_compact_payload(
            board, rows, "change", 3, "2026-11-13 15:00:00", "eastmoney", None)

        self.assertEqual(payload["schema"], "limit-hit.sector-diffusion.compact.v1")
        self.assertEqual(payload["summary"]["sample"], 3)
        self.assertEqual(payload["summary"]["limit_up_count_approx"], 1)
        self.assertEqual(payload["summary"]["gain_ge3pct_count"], 2)
        self.assertEqual(payload["summary"]["main_inflow_missing"], 1)
        self.assertIsNone(payload["summary"]["main_inflow_yi_returned_rows"])
        self.assertEqual([row["code"] for row in payload["leaders"]],
                         ["sh600001", "sh600003", "sh600002"])
        self.assertNotIn("rows", payload)
        self.assertEqual(payload["source_board_code"], "BK0001")
        self.assertTrue(payload["downstream_supported"])

    def test_diffusion_counts_flat_and_excludes_limit_states(self):
        rows = [
            {"code": "sh600001", "change_pct": 10.0, "limit_up": True},
            {"code": "sh600002", "change_pct": 5.0, "limit_up": False,
             "limit_up_touched": True},
            {"code": "sh600003", "change_pct": -10.0, "limit_up": False,
             "limit_down": True},
            {"code": "sh600004", "change_pct": 3.0, "limit_up": False},
            {"code": "sh600005", "change_pct": 0.0, "limit_up": False},
            {"code": "sh600006", "change_pct": -2.0, "limit_up": False},
            {"code": "sh600007", "change_pct": 0.0, "limit_up": False,
             "suspended": True},
            {"code": "sh600008", "change_pct": None, "limit_up": False},
            {"code": "sh600009", "change_pct": -1.0, "limit_up": False},
        ]

        diffusion = LEADERS.compute_diffusion(rows)

        self.assertEqual(diffusion["total"], 9)
        self.assertEqual(diffusion["eligible"], 4)
        self.assertEqual(diffusion["up"], 1)
        self.assertEqual(diffusion["down"], 2)
        self.assertEqual(diffusion["flat"], 1)
        self.assertEqual(diffusion["excluded_limit_up"], 1)
        self.assertEqual(diffusion["excluded_limit_touched"], 1)
        self.assertEqual(diffusion["excluded_limit_down"], 1)
        self.assertEqual(diffusion["excluded_suspended"], 1)
        self.assertEqual(diffusion["excluded_unquoted"], 1)
        self.assertEqual(diffusion["rate_pct"], 25.0)

    def test_st_component_uses_five_percent_limit_threshold(self):
        self.assertTrue(LEADERS.is_limit_up_touched(
            5.3, 5.0, "600001", "*ST测试"))
        self.assertFalse(LEADERS.is_limit_up_touched(
            5.3, 5.0, "600001", "普通股份"))

    def test_tencent_board_marks_downstream_unsupported(self):
        self.assertEqual(
            BOARDS.source_capabilities("eastmoney", "BK1134"),
            {
                "source_board_code": "BK1134",
                "downstream_supported": True,
                "downstream_reason": None,
            },
        )
        capabilities = BOARDS.source_capabilities("tencent", "pt02GN2006")
        self.assertEqual(capabilities["source_board_code"], "pt02GN2006")
        self.assertFalse(capabilities["downstream_supported"])
        self.assertIn("东财 BK", capabilities["downstream_reason"])

    def test_multiple_supported_boards_keep_top_level_capability_true(self):
        rows = [{"code": "BK1134"}, {"code": "BK1792"}]

        capabilities = BOARDS.summarize_source_capabilities(
            "eastmoney", rows)

        self.assertIsNone(capabilities["source_board_code"])
        self.assertTrue(capabilities["downstream_supported"])
        self.assertIsNone(capabilities["downstream_reason"])

    def test_board_field_quality_distinguishes_exact_partial_and_unavailable(self):
        rows = [
            {"change5_pct": 1.2, "main_inflow_yi": 3.0},
            {"change5_pct": None, "main_inflow_yi": None},
            {"change5_pct": 2.1},
        ]

        quality = BOARDS.summarize_field_quality(
            rows, ("change5_pct", "main_inflow_yi"))

        self.assertEqual(quality["change5_pct"]["status"], "partial")
        self.assertEqual(quality["change5_pct"]["available"], 2)
        self.assertEqual(quality["change5_pct"]["missing"], 1)
        self.assertEqual(quality["main_inflow_yi"]["status"], "partial")
        self.assertEqual(quality["main_inflow_yi"]["available"], 1)
        self.assertEqual(
            BOARDS.summarize_field_quality(
                [{"change5_pct": 1.0}], ("change5_pct",)
            )["change5_pct"]["status"],
            "exact",
        )
        self.assertEqual(
            BOARDS.summarize_field_quality(
                [{"change5_pct": None}], ("change5_pct",)
            )["change5_pct"]["status"],
            "unavailable",
        )

    def test_full_member_fetch_does_not_stop_at_display_top(self):
        pages = {
            1: [{"f12": "{:06d}".format(index)} for index in range(100)],
            2: [{"f12": "{:06d}".format(index)} for index in range(100, 150)],
            3: [],
        }

        def fake_http_get(url):
            page = int(url.split("pn=", 1)[1].split("&", 1)[0])
            return json.dumps({
                "rc": 0,
                "data": {"diff": pages[page]},
            }).encode("utf-8")

        with mock.patch.object(LEADERS, "http_get", side_effect=fake_http_get):
            rows = LEADERS.fetch_stocks("BK0001", "change", None)

        self.assertEqual(len(rows), 150)

    def test_member_fetch_marks_partial_pagination_incomplete(self):
        first_page = [{"f12": "{:06d}".format(index)} for index in range(100)]

        def fake_http_get(url):
            page = int(url.split("pn=", 1)[1].split("&", 1)[0])
            if page == 1:
                return json.dumps({
                    "rc": 0,
                    "data": {"diff": first_page},
                }).encode("utf-8")
            raise OSError("pagination failed")

        with mock.patch.object(LEADERS, "http_get", side_effect=fake_http_get):
            rows, complete = LEADERS.fetch_stocks(
                "BK0001", "change", None, return_status=True)

        self.assertEqual(len(rows), 100)
        self.assertFalse(complete)

    def test_text_rendering_uses_complete_diffusion_counts(self):
        board = {
            "code": "BK0001",
            "name": "测试板块",
            "up_count": 1,
            "down_count": 2,
            "change_pct": 1.0,
            "change5_pct": 2.0,
            "change10_pct": None,
            "leader_name": "甲",
            "leader_change_pct": 3.0,
            "amount_yi": 1.0,
            "main_inflow_yi": 0.5,
        }
        diffusion = {
            "up": 1,
            "down": 2,
            "flat": 1,
            "excluded_limit_up": 1,
            "excluded_limit_touched": 0,
            "excluded_limit_down": 0,
            "excluded_suspended": 0,
            "excluded_unquoted": 0,
            "rate_pct": 25.0,
            "sample_complete": True,
        }

        text = LEADERS.render_text(
            board, [], "change", 3, "2026-11-19 10:00:00",
            "过滤：无", source="eastmoney", diffusion=diffusion)

        self.assertIn("扩散 D=25.0%", text)
        self.assertIn("完整样本：是", text)

    def test_prepare_runs_sibling_data_scripts(self):
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(PREPARE.subprocess, "run", return_value=completed) as run:
            payload = PREPARE.run_json("fetch_limit_up.py", "--json")

        command = run.call_args.args[0]
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(pathlib.Path(command[1]), SCRIPT_DIR / "fetch_limit_up.py")

    def test_data_scripts_import_without_market_data(self):
        scripts = (
            "prepare_candidates.py",
            "fetch_limit_up.py",
            "fetch_limit_up_history.py",
            "fetch_stock_structure.py",
            "fetch_stock_metrics.py",
            "fetch_sector_boards.py",
            "fetch_sector_leaders.py",
            "fetch_news.py",
            "fetch_capital_flow.py",
            "save_limit_pool.py",
        )
        for script in scripts:
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, str(SCRIPT_DIR / script), "--help"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
