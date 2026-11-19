import importlib.util
import pathlib
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "limit-hit"
    / "scripts"
    / "prepare_candidates.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "limit_hit_prepare_rules", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuleConstantsTest(unittest.TestCase):
    def test_rules_are_embedded_in_prepare_script(self):
        module = load_module()

        self.assertEqual(
            module.RULES,
            {
                "one_word_executable": True,
                "collapse_veto": False,
                "weak_market_gate": {
                    "max_picks": 1,
                    "min_score": 85,
                    "override": {
                        "enabled": True,
                        "theme_score_threshold": 80,
                        "min_score_by_mode": {
                            "one_to_two": 80,
                            "two_to_three": 79,
                        },
                    },
                },
                "second_line": {
                    "admission": "strict",
                    "retreat_cap": "watch_only",
                    "collapse_cap": "watch_only",
                },
                "theme_score": {
                    "missing_dimension": "exact_only",
                },
                "market_gate": {
                    "retreat_time_gate": "10:30",
                },
            },
        )
        self.assertNotIn("name", module.RULES)


class OneWordGateTest(unittest.TestCase):
    def test_tradable_one_word_keeps_evidence(self):
        module = load_module()
        candidate = {
            "code": "sh600001",
            "amount_yi": 0.34,
            "seal_fund_yi": 1.29,
            "float_cap_yi": 10.0,
        }

        result = module.filter_one_word_candidates(
            [candidate], {"sh600001": True})

        row = result["candidates"][0]
        self.assertTrue(row["one_word"])
        self.assertEqual(row["board_amount_yi"], 0.34)
        self.assertEqual(row["seal_fund_ratio_pct"], 379.41)
        self.assertEqual(row["seal_fund_over_float_pct"], 12.9)
        self.assertIn(row["fill_probability"], ("高", "中", "低"))

    def test_zero_board_amount_remains_unexecutable(self):
        module = load_module()
        candidate = {
            "code": "sh600001",
            "amount_yi": 0.0,
            "seal_fund_yi": 1.29,
            "float_cap_yi": 10.0,
        }

        result = module.filter_one_word_candidates(
            [candidate], {"sh600001": True})

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["excluded"][0]["reason"], "板上成交额为0")


def weak_market_fixture():
    return {
        "market": {
            "zt_count": 32,
            "dt_count": 21,
            "zb_count": 19,
            "seal_rate_pct": 62.7,
            "max_board": 4,
        },
        "promotion_rate_pct": 19.35,
        "yesterday_open": {
            "up_open": 10,
            "flat_open": 2,
            "down_open": 4,
            "avg_gap_pct": 0.8,
        },
        "yesterday_high": {
            "stocks": [{
                "code": "sh600001",
                "suspended": False,
                "close_state": "涨停",
                "today_open": 10.0,
                "today_pct": 10.0,
            }],
        },
        "previous_max_board": 4,
    }


class MarketGateTest(unittest.TestCase):
    def test_breadth_collapse_is_weak_warning(self):
        module = load_module()

        gate = module.evaluate_market_gate(**weak_market_fixture())

        self.assertTrue(gate["breadth_collapse"])
        self.assertFalse(gate["veto"])
        self.assertEqual(gate["state"], "偏弱")
        self.assertEqual(gate["action"], "max_one")
        self.assertEqual(gate["max_picks"], 1)

    def test_leader_collapse_remains_observe_only(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 5
        fixture["yesterday_high"]["stocks"][0]["close_state"] = "跌停"

        gate = module.evaluate_market_gate(**fixture)

        self.assertTrue(gate["leader_collapse"])
        self.assertEqual(gate["action"], "observe_only")

    def test_retreat_produces_no_trade(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 10

        gate = module.evaluate_market_gate(**fixture)

        self.assertTrue(gate["retreat"])
        self.assertEqual(gate["action"], "no_trade")

    def test_early_retreat_is_pending(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 6.06

        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 09:42:00",
        )

        self.assertTrue(gate["retreat"])
        self.assertFalse(gate["retreat_confirmed"])
        self.assertEqual(gate["state"], "退潮待确认")
        self.assertEqual(gate["action"], "max_one")
        self.assertEqual(gate["max_picks"], 1)

    def test_before_auction_stops_market_conclusion(self):
        module = load_module()

        gate = module.evaluate_market_gate(
            **weak_market_fixture(),
            promotion_sample_time="2026-11-19 09:23:40",
        )

        self.assertEqual(gate["state"], "开盘未撮合")
        self.assertEqual(gate["action"], "no_conclusion")
        self.assertTrue(gate["veto"])

    def test_retreat_is_confirmed_at_1030(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 6.06

        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 10:30:00",
        )

        self.assertTrue(gate["retreat_confirmed"])
        self.assertEqual(gate["state"], "退潮")
        self.assertEqual(gate["action"], "no_trade")

    def test_pending_retreat_raises_candidate_threshold_to_85(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 6.06
        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 09:42:00",
        )

        threshold = module.minimum_candidate_score(
            theme_score=87, mode="one_to_two", market_gate=gate)

        self.assertEqual(threshold, 85)

    def test_collective_low_open_caps_at_one(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["promotion_rate_pct"] = 30
        fixture["yesterday_open"] = {
            "up_open": 2,
            "flat_open": 1,
            "down_open": 8,
            "avg_gap_pct": -1.2,
        }

        gate = module.evaluate_market_gate(**fixture)

        self.assertTrue(gate["collective_low_open"])
        self.assertEqual(gate["max_picks"], 1)

    def test_missing_market_counts_stop_conclusion(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"] = {}

        gate = module.evaluate_market_gate(**fixture)

        self.assertEqual(gate["state"], "数据未获取")
        self.assertEqual(gate["action"], "no_conclusion")


class WeakMarketScoreGateTest(unittest.TestCase):
    def weak_fixture(self):
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 5
        return fixture

    def test_strong_theme_uses_mode_threshold(self):
        module = load_module()
        gate = module.evaluate_market_gate(**self.weak_fixture())

        threshold = module.minimum_candidate_score(
            theme_score=87, mode="one_to_two", market_gate=gate)

        self.assertEqual(threshold, 80)

    def test_theme_below_80_keeps_85_threshold(self):
        module = load_module()
        gate = module.evaluate_market_gate(**self.weak_fixture())

        threshold = module.minimum_candidate_score(
            theme_score=72, mode="two_to_three", market_gate=gate)

        self.assertEqual(threshold, 85)


if __name__ == "__main__":
    unittest.main()
