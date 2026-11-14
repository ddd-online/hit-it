import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "limit-hit" / "scripts" / "save_limit_pool.py"
SPEC = importlib.util.spec_from_file_location("save_limit_pool", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SaveLimitPoolTest(unittest.TestCase):
    def test_saves_current_pool_in_current_workspace(self):
        rows = [
            {
                "code": "sh600001",
                "name": "测试股份",
                "board_count": 1,
                "industry": "测试行业",
            },
        ]
        context = {
            "data_date": "2026-11-13",
            "quote_date": "20261113",
            "rows": rows,
            "market": {"zt_count": 1},
            "one_word": {},
            "source": "eastmoney-push2ex",
            "source_name": "东方财富 push2ex 涨停池",
            "limitations": [],
        }
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                with mock.patch.object(
                        MODULE, "fetch_current_pool", return_value=context), \
                        mock.patch.object(
                            MODULE.pool_store, "hydrate_concepts",
                            return_value={}):
                    path, payload, created = MODULE.build_payload(True)
                    self.assertTrue(created)
                    self.assertEqual(payload["data_date"], "2026-11-13")
                    self.assertEqual(path.name, "2026-11-13.json")
                    self.assertTrue(path.exists())

                    _path, _payload, created_again = MODULE.build_payload(True)
                    self.assertFalse(created_again)
            finally:
                os.chdir(old_cwd)

    def test_dated_pool_falls_back_to_exact_history(self):
        mismatch = {
            "base_date": "20261112",
            "data_available": True,
            "zt_rows": [{"code": "sh600001"}],
        }
        history = {
            "rows": [{
                "code": "sh600002",
                "name": "历史股份",
                "board_count": 1,
                "reason_tags": ["测试题材"],
            }],
            "source": "同花顺公开历史涨停池",
            "limitations": ["仅涨停成功池"],
        }
        with mock.patch.object(
                MODULE.pool_store, "run_json",
                side_effect=[mismatch, history]):
            payload = MODULE.fetch_dated_pool("20261113")

        self.assertEqual(payload["data_date"], "2026-11-13")
        self.assertEqual(payload["source"], "10jqka-history")
        self.assertEqual(payload["rows"][0]["code"], "sh600002")


if __name__ == "__main__":
    unittest.main()
