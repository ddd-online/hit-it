import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class SkillBoundaryTest(unittest.TestCase):
    def test_backtest_assets_are_outside_skill_directories(self):
        assets = (
            (
                "backtest/limit-hit/backtest_limit_hit.py",
                "limit-hit/scripts/backtest_limit_hit.py",
            ),
            (
                "backtest/limit-hit/backtest-report-template.md",
                "limit-hit/references/backtest-report-template.md",
            ),
            (
                "backtest/leader-hit/backtest_leader_hit.py",
                "leader-hit/scripts/backtest_leader_hit.py",
            ),
            (
                "backtest/leader-hit/backtest-report-template.md",
                "leader-hit/references/backtest-report-template.md",
            ),
            (
                "backtest/leader-hit/review_leader_snapshot.py",
                "leader-hit/scripts/review_leader_snapshot.py",
            ),
        )
        for outside, inside in assets:
            with self.subTest(asset=outside):
                self.assertTrue((ROOT / outside).is_file())
                self.assertFalse((ROOT / inside).exists())


if __name__ == "__main__":
    unittest.main()
