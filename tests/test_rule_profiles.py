import importlib.util
import pathlib
import tempfile
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "limit-hit"
    / "scripts"
    / "rule_profiles.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("rule_profiles", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProfileConfigTest(unittest.TestCase):
    def test_default_profile_uses_theme_first_gates(self):
        module = load_module()

        self.assertEqual(
            module.load_rule_profile("default"),
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

    def test_theme_first_enables_both_flexible_gates(self):
        module = load_module()

        profile = module.load_rule_profile("theme-first")

        self.assertTrue(profile["one_word_executable"])
        self.assertFalse(profile["collapse_veto"])
        self.assertEqual(
            profile["weak_market_gate"]["override"]["min_score_by_mode"],
            {"one_to_two": 80, "two_to_three": 79},
        )

    def test_v14_profile_retains_legacy_gates(self):
        module = load_module()

        profile = module.load_rule_profile("v1.4")

        self.assertFalse(profile["one_word_executable"])
        self.assertTrue(profile["collapse_veto"])
        self.assertFalse(profile["weak_market_gate"]["override"]["enabled"])

    def test_v16_profile_keeps_legacy_second_line_and_score_behavior(self):
        module = load_module()

        profile = module.load_rule_profile("v1.6")

        self.assertEqual(profile["second_line"]["admission"], "legacy")
        self.assertEqual(profile["second_line"]["retreat_cap"], "no_list")
        self.assertEqual(profile["theme_score"]["missing_dimension"], "zero")
        self.assertEqual(profile["market_gate"]["retreat_time_gate"], "09:30")

    def test_unknown_profile_is_rejected(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "未知 profile"):
            module.load_rule_profile("missing")

    def test_missing_required_key_is_rejected(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "rules.yaml"
            path.write_text(
                "version: 1\n"
                "default:\n"
                "  one_word_executable: false\n"
                "  weak_market_gate:\n"
                "    max_picks: 1\n"
                "    min_score: 85\n"
                "    override:\n"
                "      enabled: false\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "collapse_veto"):
                module.load_rule_profile("default", path)


class OneWordGateTest(unittest.TestCase):
    def test_default_profile_keeps_tradable_one_word_candidate(self):
        module = load_module()
        candidate = {
            "code": "sh600001",
            "amount_yi": 0.34,
            "seal_fund_yi": 1.29,
            "float_cap_yi": 10.0,
        }

        result = module.filter_one_word_candidates(
            [candidate],
            {"sh600001": True},
            module.load_rule_profile("default"),
        )

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["excluded_count"], 0)

    def test_theme_first_keeps_tradable_one_word_with_evidence(self):
        module = load_module()
        candidate = {
            "code": "sh600001",
            "amount_yi": 0.34,
            "seal_fund_yi": 1.29,
            "float_cap_yi": 10.0,
        }

        result = module.filter_one_word_candidates(
            [candidate],
            {"sh600001": True},
            module.load_rule_profile("theme-first"),
        )

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
            [candidate],
            {"sh600001": True},
            module.load_rule_profile("theme-first"),
        )

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
        "yesterday_open": {"up_open": 10, "flat_open": 2, "down_open": 4,
                           "avg_gap_pct": 0.8},
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
    def test_default_treats_breadth_collapse_as_weak_warning(self):
        module = load_module()
        fixture = weak_market_fixture()

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("default"),
        )

        self.assertTrue(gate["breadth_collapse"])
        self.assertFalse(gate["veto"])
        self.assertEqual(gate["state"], "偏弱")
        self.assertEqual(gate["action"], "max_one")

    def test_theme_first_keeps_breadth_collapse_as_weak_warning(self):
        module = load_module()
        fixture = weak_market_fixture()

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("theme-first"),
        )

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

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertTrue(gate["leader_collapse"])
        self.assertEqual(gate["action"], "observe_only")

    def test_retreat_cannot_be_disabled_by_theme_first(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 10

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertTrue(gate["retreat"])
        self.assertEqual(gate["action"], "no_trade")

    def test_early_retreat_is_pending_until_confirmation_time(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 6.06

        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 09:42:00",
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertTrue(gate["retreat"])
        self.assertFalse(gate["retreat_confirmed"])
        self.assertEqual(gate["state"], "退潮待确认")
        self.assertEqual(gate["action"], "max_one")
        self.assertEqual(gate["max_picks"], 1)

    def test_before_auction_stops_market_conclusion(self):
        module = load_module()
        fixture = weak_market_fixture()

        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 09:23:40",
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertEqual(gate["state"], "开盘未撮合")
        self.assertEqual(gate["action"], "no_conclusion")
        self.assertTrue(gate["veto"])

    def test_retreat_is_confirmed_at_configured_time(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["market"]["max_board"] = 3
        fixture["previous_max_board"] = 4
        fixture["promotion_rate_pct"] = 6.06

        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 10:30:00",
            profile=module.load_rule_profile("theme-first"),
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
        profile = module.load_rule_profile("theme-first")
        gate = module.evaluate_market_gate(
            **fixture,
            promotion_sample_time="2026-11-19 09:42:00",
            profile=profile,
        )

        threshold = module.minimum_candidate_score(
            theme_score=87, mode="one_to_two",
            market_gate=gate, profile=profile)

        self.assertEqual(threshold, 85)

    def test_collective_low_open_caps_theme_first_at_one(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 2
        fixture["promotion_rate_pct"] = 30
        fixture["yesterday_open"] = {
            "up_open": 2, "flat_open": 1, "down_open": 8, "avg_gap_pct": -1.2,
        }

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertTrue(gate["collective_low_open"])
        self.assertEqual(gate["max_picks"], 1)

    def test_missing_market_counts_stop_conclusion(self):
        module = load_module()
        fixture = weak_market_fixture()
        fixture["market"] = {}

        gate = module.evaluate_market_gate(
            **fixture,
            profile=module.load_rule_profile("theme-first"),
        )

        self.assertEqual(gate["state"], "数据未获取")
        self.assertEqual(gate["action"], "no_conclusion")


class WeakMarketScoreGateTest(unittest.TestCase):
    def weak_fixture(self):
        fixture = weak_market_fixture()
        fixture["market"]["dt_count"] = 5
        return fixture

    def test_default_uses_theme_first_override(self):
        module = load_module()
        fixture = self.weak_fixture()
        profile = module.load_rule_profile("default")
        gate = module.evaluate_market_gate(**fixture, profile=profile)

        threshold = module.minimum_candidate_score(
            theme_score=87, mode="one_to_two", market_gate=gate, profile=profile)

        self.assertEqual(threshold, 80)

    def test_theme_first_uses_mode_threshold_for_strong_theme(self):
        module = load_module()
        fixture = self.weak_fixture()
        profile = module.load_rule_profile("theme-first")
        gate = module.evaluate_market_gate(**fixture, profile=profile)

        threshold = module.minimum_candidate_score(
            theme_score=87, mode="one_to_two", market_gate=gate, profile=profile)

        self.assertEqual(threshold, 80)

    def test_theme_first_does_not_lower_threshold_for_theme_below_80(self):
        module = load_module()
        fixture = self.weak_fixture()
        profile = module.load_rule_profile("theme-first")
        gate = module.evaluate_market_gate(**fixture, profile=profile)

        threshold = module.minimum_candidate_score(
            theme_score=72, mode="two_to_three", market_gate=gate, profile=profile)

        self.assertEqual(threshold, 85)


if __name__ == "__main__":
    unittest.main()
