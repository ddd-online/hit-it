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


class SelfContainedScriptsTest(unittest.TestCase):
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
