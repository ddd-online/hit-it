import importlib.util
import pathlib
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


class SelfContainedScriptsTest(unittest.TestCase):
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
