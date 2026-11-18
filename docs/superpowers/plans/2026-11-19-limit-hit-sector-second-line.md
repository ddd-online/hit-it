# limit-hit 板块第二梯队回测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 2026 年历史结构代理中，按 09:40、10:00、10:30、11:00 四个时点分别评估板块打板股与未涨停第二梯队股，并输出独立资金曲线和候选明细。

**Architecture:** 新增一个位于 `backtest/limit-hit/` 的独立回测脚本。脚本复用历史涨停池、日 K、板块榜和 F10 成分股取数层；先构造时点可见题材梯队，再把打板组与第二梯队组分开筛选、模拟和报告。当前概念成分和历史分钟缺失会作为显式限制记录，不修改正式 SKILL 的“可打板”结论。

**Tech Stack:** Python 3 标准库、`unittest`、现有 `limit-hit/scripts` 数据层、Markdown 报告。

**Spec:** `docs/superpowers/specs/2026-11-19-limit-hit-sector-second-line-design.md`

## Global Constraints

- 固定执行时点为 `09:40,10:00,10:30,11:00`。
- 两组分别使用 10,000 元初始本金，不能合并资金。
- 打板组按 D 日涨停价买入；第二梯队组按 D 日开盘价买入。
- 第二梯队组每个时点都必须排除该时点前已首封的股票。
- 第二梯队选股只能使用 D-1 及以前结构、D 日开盘价和时点前首封状态。
- 当前概念成分不是历史快照，所有报告必须记录幸存者偏差和概念调整风险。
- 只使用 Python 标准库，不新增第三方依赖、外部数据目录、缓存文件或状态文件。
- 回测和模板位于 `backtest/limit-hit/`，不写入 SKILL 目录。
- 所有报告必须写“不构成投资建议”。
- 修改代码前先运行 `python -m unittest discover -s tests -v`。
- 完成后运行全量单元测试和 `python -m compileall -q backtest/limit-hit limit-hit/scripts`。

---

### Task 1: 时点、可见样本与题材梯队

**Files:**

- Create: `backtest/limit-hit/backtest_sector_second_line.py`
- Create: `tests/test_sector_second_line.py`

**Interfaces:**

- Consumes: `backtest_limit_hit.reason_tags(row)`, `theme_score(current_count, promoted_count, max_board)`.
- Produces: `parse_times(raw: str) -> tuple[str, ...]`; `visible_limit_rows(rows: list[dict], cutoff: str) -> list[dict]`; `theme_ladder(current_rows: list[dict], previous_rows: list[dict], min_score: int, max_themes: int) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_sector_second_line -v`
Expected: FAIL because `backtest_sector_second_line.py` or the functions do not exist.

- [ ] **Step 3: Implement minimal helpers**

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于历史涨停池和日 K 的板块第二梯队结构代理回测。"""

import argparse
import collections
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

BACKTEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKTEST_DIR.parents[1]
SKILL_SCRIPTS = REPO_ROOT / "limit-hit" / "scripts"
sys.path.insert(0, str(BACKTEST_DIR))
sys.path.insert(0, str(SKILL_SCRIPTS))

import backtest_limit_hit as backtest  # noqa: E402
import fallback_sources  # noqa: E402
import fetch_sector_boards as sector_boards  # noqa: E402
import fetch_sector_leaders as sector_leaders  # noqa: E402
import fetch_stock_structure  # noqa: E402
import http_util  # noqa: E402

ALLOWED_TIMES = ("09:40", "10:00", "10:30", "11:00")


def parse_times(raw):
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values:
        raise argparse.ArgumentTypeError("至少指定一个执行时点")
    parsed = []
    for value in values:
        try:
            clock = datetime.strptime(value, "%H:%M").strftime("%H:%M:%S")
        except ValueError:
            raise argparse.ArgumentTypeError("时间格式应为 HH:MM")
        if value not in ALLOWED_TIMES:
            raise argparse.ArgumentTypeError(
                "只支持 09:40,10:00,10:30,11:00")
        parsed.append(clock)
    return tuple(parsed)


def visible_limit_rows(rows, cutoff):
    return [
        row for row in rows
        if row.get("first_seal") and str(row["first_seal"]) <= cutoff
    ]


def theme_ladder(current_rows, previous_rows, min_score, max_themes):
    current_by_theme = collections.defaultdict(list)
    previous_by_theme = collections.defaultdict(list)
    for row in current_rows:
        for theme in backtest.reason_tags(row):
            current_by_theme[theme].append(row)
    for row in previous_rows:
        for theme in backtest.reason_tags(row):
            previous_by_theme[theme].append(row)
    themes = []
    for theme, rows in current_by_theme.items():
        previous = previous_by_theme.get(theme, [])
        promoted = len(
            {row["code"] for row in rows}
            & {row["code"] for row in previous}
        )
        if len(rows) < 2 and not promoted:
            continue
        max_board = max((row.get("board_count") or 0 for row in rows), default=0)
        score = backtest.theme_score(len(rows), promoted, max_board)
        if score >= min_score:
            themes.append({
                "theme": theme,
                "theme_score": score,
                "current_count": len(rows),
                "promoted_count": promoted,
                "max_board": max_board,
            })
    themes.sort(
        key=lambda item: (
            -item["theme_score"],
            -item["current_count"],
            -item["max_board"],
            item["theme"],
        )
    )
    return themes[:max_themes]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_sector_second_line -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py tests/test_sector_second_line.py
git commit -m "feat: add sector backtest time ladder"
```

---

### Task 2: 精确概念映射、成分股与跨题材去重

**Files:**

- Modify: `backtest/limit-hit/backtest_sector_second_line.py`
- Modify: `tests/test_sector_second_line.py`

**Interfaces:**

- Consumes: concept rows from `fetch_sector_boards`; `fetch_sector_leaders.fetch_f10_members_quotes(bk_code) -> tuple[str, list[dict]]`.
- Produces: `build_board_index(rows: list[dict]) -> dict[str, str]`; `member_rows(bk_code: str, fetch_members=...) -> list[dict]`; `dedupe_candidates(candidates: list[dict]) -> list[dict]`.

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_sector_second_line.MembershipTest -v`
Expected: FAIL because the membership helpers are undefined.

- [ ] **Step 3: Implement exact mapping and filtering**

```python
def build_board_index(rows):
    return {
        str(row.get("name") or "").strip(): row["code"]
        for row in rows
        if str(row.get("name") or "").strip() and row.get("code")
    }


def normalize_members(raw_members):
    rows = []
    seen = set()
    for item in raw_members:
        code = item.get("code") or fallback_sources.infer_sec_code(
            str(item.get("SECURITY_CODE") or ""))
        name = str(
            item.get("name")
            or item.get("SECURITY_NAME_ABBR")
            or item.get("SECURITY_NAME")
            or ""
        )
        if not code or code in seen:
            continue
        if not backtest.is_mainboard(code) or backtest.is_st(name):
            continue
        seen.add(code)
        rows.append({
            "code": code,
            "name": name,
            "float_cap_yi": item.get("float_cap_yi"),
        })
    return rows


def member_rows(bk_code, fetch_members=None):
    fetch_members = (
        fetch_members or sector_leaders.fetch_f10_members_quotes)
    _board_name, raw_members = fetch_members(bk_code)
    return normalize_members(raw_members)


def dedupe_candidates(candidates):
    best = {}
    for row in candidates:
        current = best.get(row["code"])
        if current is None or row["theme_score"] > current["theme_score"]:
            best[row["code"]] = row
    return sorted(
        best.values(),
        key=lambda row: (-row["theme_score"], row["code"]),
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_sector_second_line.MembershipTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py tests/test_sector_second_line.py
git commit -m "feat: map exact sectors and members"
```

---

### Task 3: D-1 结构特征与第二梯队条件

**Files:**

- Modify: `backtest/limit-hit/backtest_sector_second_line.py`
- Modify: `tests/test_sector_second_line.py`

**Interfaces:**

- Consumes: chronologically sorted daily bars with `date`, `open`, `high`, `low`, `close`, `volume_hands`.
- Produces: `bar_features(bars: list[dict], data_date: str) -> dict | None`; `second_line_eligible(feature: dict) -> bool`.

- [ ] **Step 1: Write failing tests**

```python
def make_bars():
    bars = []
    for index in range(25):
        close = 10.0 + index * 0.02
        bars.append({
            "date": "2026-01-{:02d}".format(index + 1),
            "open": close - 0.03,
            "high": close + 0.08,
            "low": close - 0.08,
            "close": close,
            "volume_hands": 1000.0,
        })
    return bars


class FeatureTest(unittest.TestCase):
    def test_features_use_previous_day_structure(self):
        bars = make_bars()
        day = bars[-1]
        day["open"] = bars[-2]["close"] * 1.02
        feature = MODULE.bar_features(bars, day["date"])
        self.assertEqual(feature["previous_close"], bars[-2]["close"])
        self.assertEqual(feature["open_gap_pct"], 2.0)

    def test_second_line_rejects_limit_open(self):
        bars = make_bars()
        bars[-1]["open"] = round(bars[-2]["close"] * 1.10, 2)
        feature = MODULE.bar_features(bars, bars[-1]["date"])
        self.assertFalse(MODULE.second_line_eligible(feature))

    def test_second_line_accepts_controlled_open(self):
        bars = make_bars()
        bars[-1]["open"] = round(bars[-2]["close"] * 1.02, 2)
        feature = MODULE.bar_features(bars, bars[-1]["date"])
        self.assertTrue(MODULE.second_line_eligible(feature))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_sector_second_line.FeatureTest -v`
Expected: FAIL because feature helpers are undefined.

- [ ] **Step 3: Implement feature and hard-condition functions**

```python
def pct_change(now, before):
    if now is None or not before:
        return None
    return round((now / before - 1.0) * 100.0, 4)


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def bar_features(bars, data_date):
    eligible = [
        bar for bar in bars
        if bar.get("date") and bar["date"] <= data_date
    ]
    if len(eligible) < 21 or eligible[-1]["date"] != data_date:
        return None
    day = eligible[-1]
    previous = eligible[-2]
    previous_window = eligible[:-1]
    closes = [bar.get("close") for bar in previous_window[-20:]
              if bar.get("close") is not None]
    if len(closes) < 20:
        return None
    volumes = [
        bar.get("volume_hands") if bar.get("volume_hands") is not None
        else bar.get("volume")
        for bar in previous_window[-20:]
    ]
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    range20_low = min(bar["low"] for bar in previous_window[-20:])
    range20_high = max(bar["high"] for bar in previous_window[-20:])
    previous_close = previous["close"]
    limit_price = round(previous_close * 1.10, 2)
    return {
        "data_date": data_date,
        "previous_close": previous_close,
        "open": day.get("open"),
        "high": day.get("high"),
        "low": day.get("low"),
        "close": day.get("close"),
        "limit_price": limit_price,
        "open_gap_pct": pct_change(day.get("open"), previous_close),
        "previous_ma5": round(ma5, 4),
        "previous_ma10": round(ma10, 4),
        "previous_return5_pct": pct_change(
            previous_close,
            previous_window[-6]["close"] if len(previous_window) >= 6 else None,
        ),
        "previous_position20_pct": round(
            (previous_close - range20_low)
            / (range20_high - range20_low) * 100.0,
            2,
        ) if range20_high > range20_low else None,
        "previous_volume_ratio5": round(
            volumes[-1] / mean(volumes[-6:-1]), 3
        ) if len(volumes) >= 6 and mean(volumes[-6:-1]) else None,
    }


def second_line_eligible(feature):
    if not feature:
        return False
    required = (
        feature.get("open"),
        feature.get("previous_close"),
        feature.get("previous_ma5"),
        feature.get("previous_ma10"),
        feature.get("open_gap_pct"),
        feature.get("previous_return5_pct"),
        feature.get("previous_position20_pct"),
    )
    if any(value is None for value in required):
        return False
    return bool(
        feature["open"] < feature["limit_price"] - 0.001
        and -3.0 <= feature["open_gap_pct"] <= 5.0
        and feature["previous_close"] >= feature["previous_ma5"]
        and feature["previous_close"] >= feature["previous_ma10"]
        and -3.0 <= feature["previous_return5_pct"] <= 15.0
        and 20.0 <= feature["previous_position20_pct"] <= 85.0
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_sector_second_line.FeatureTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py tests/test_sector_second_line.py
git commit -m "feat: add second-line feature filters"
```

---

### Task 4: 两组独立模拟与当日结果

**Files:**

- Modify: `backtest/limit-hit/backtest_sector_second_line.py`
- Modify: `tests/test_sector_second_line.py`

**Interfaces:**

- Consumes: `backtest.simulate_trade`, `backtest.summarize_trades`, selected candidates.
- Produces: `candidate_outcome(feature: dict, next_bar: dict) -> dict`; `run_group(signals: list[dict], initial_capital: float, fees: dict, signal_days: int | None = None) -> dict`.

- [ ] **Step 1: Write failing tests**

```python
class SimulationTest(unittest.TestCase):
    def test_candidate_outcome_marks_seal_and_premium(self):
        feature = {
            "data_date": "2026-01-05",
            "open": 10.0,
            "close": 11.0,
            "limit_price": 11.0,
            "previous_close": 10.0,
        }
        outcome = MODULE.candidate_outcome(feature, {"open": 11.3})
        self.assertTrue(outcome["sealed_close"])
        self.assertEqual(outcome["next_open_premium_pct"], 2.73)

    def test_open_limit_cannot_fill_second_line(self):
        feature = {
            "data_date": "2026-01-05",
            "open": 11.0,
            "close": 11.0,
            "limit_price": 11.0,
            "previous_close": 10.0,
        }
        self.assertFalse(MODULE.can_fill_second_line(feature))

    def test_group_capital_compounds_once_per_day(self):
        signals = [
            {"date": "2026-01-05", "entry_price": 10.0,
             "exit_price": 11.0, "theme": "机器人"},
            {"date": "2026-01-06", "entry_price": 11.0,
             "exit_price": 10.5, "theme": "机器人"},
        ]
        result = MODULE.run_group(
            signals, 10000.0,
            {"commission_rate": 0.00025, "min_commission": 5.0,
             "stamp_duty_rate": 0.0005, "transfer_rate": 0.00001},
        )
        self.assertEqual(result["summary"]["trade_count"], 2)
        self.assertLess(result["summary"]["ending_capital"], 10500.0)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_sector_second_line.SimulationTest -v`
Expected: FAIL because simulation helpers are undefined.

- [ ] **Step 3: Implement outcome and compounding**

```python
def candidate_outcome(feature, next_bar):
    close = feature.get("close")
    next_open = (next_bar or {}).get("open")
    return {
        "sealed_close": bool(
            close is not None
            and close >= feature["limit_price"] * 0.999
        ),
        "next_open": next_open,
        "next_open_premium_pct": pct_change(next_open, close),
    }


def can_fill_second_line(feature):
    open_price = feature.get("open")
    return bool(
        open_price is not None
        and open_price < feature["limit_price"] - 0.001
    )


def run_group(signals, initial_capital, fees, signal_days=None):
    capital = initial_capital
    trades = []
    seen_dates = set()
    for signal in sorted(signals, key=lambda row: row["date"]):
        if signal["date"] in seen_dates:
            continue
        trade = backtest.simulate_trade(
            signal, signal["entry_price"], signal["exit_price"],
            capital, fees,
        )
        if trade is None:
            continue
        seen_dates.add(signal["date"])
        capital = trade["capital_after"]
        trades.append({
            "signal_date": signal["date"],
            "theme": signal.get("theme"),
            "entry_price": signal["entry_price"],
            "exit_price": signal["exit_price"],
            **trade,
            **signal.get("outcome", {}),
        })
    summary = backtest.summarize_trades(
        trades,
        initial_capital,
        signal_days if signal_days is not None else len(trades),
    )
    return {"summary": summary, "trades": trades}
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_sector_second_line.SimulationTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py tests/test_sector_second_line.py
git commit -m "feat: simulate sector groups independently"
```

---

### Task 5: 报告模板、CLI 与横向比较

**Files:**

- Create: `backtest/limit-hit/sector-second-line-template.md`
- Modify: `backtest/limit-hit/backtest_sector_second_line.py`
- Modify: `tests/test_sector_second_line.py`

**Interfaces:**

- Consumes: per-time result dictionaries from Task 4.
- Produces: `report_paths(workspace: Path, start: str, end: str, times: tuple[str, ...]) -> dict[str, Path]`; `render_time_report(result: dict) -> str`; `render_comparison(results: list[dict]) -> str`; CLI arguments `--start`, `--end`, `--capital`, `--times`, `--workers`.

- [ ] **Step 1: Write failing tests**

```python
class ReportTest(unittest.TestCase):
    def test_time_report_contains_both_groups_and_limits(self):
        result = {
            "time": "09:40:00",
            "start": "20260101",
            "end": "20261119",
            "capital": 10000.0,
            "groups": {
                "limit": {"summary": {"ending_capital": 10000.0, "trade_count": 0}},
                "second_line": {"summary": {"ending_capital": 10000.0, "trade_count": 0}},
            },
            "limitations": ["当前概念成分不是历史快照"],
        }
        text = MODULE.render_time_report(result)
        self.assertIn("打板股组", text)
        self.assertIn("第二梯队组", text)
        self.assertIn("当前概念成分不是历史快照", text)
        self.assertIn("不构成投资建议", text)

    def test_comparison_has_four_rows(self):
        results = [
            {"time": time, "groups": {
                "limit": {"summary": {"ending_capital": 10000.0}},
                "second_line": {"summary": {"ending_capital": 10000.0}},
            }}
            for time in ("09:40:00", "10:00:00", "10:30:00", "11:00:00")
        ]
        text = MODULE.render_comparison(results)
        self.assertEqual(text.count("| 2026-"), 0)
        self.assertIn("| 09:40:00 |", text)
        self.assertIn("| 11:00:00 |", text)

    def test_report_paths_are_unique(self):
        paths = MODULE.report_paths(
            pathlib.Path("C:/work"),
            "20260101",
            "20261119",
            ("09:40:00", "10:00:00", "10:30:00", "11:00:00"),
        )
        self.assertEqual(len(set(paths.values())), 5)
        self.assertIn("09-40", str(paths["09:40:00"]))
        self.assertIn("时点对比", str(paths["comparison"]))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m unittest tests.test_sector_second_line.ReportTest -v`
Expected: FAIL because report helpers do not exist.

- [ ] **Step 3: Add a complete report template**

`backtest/limit-hit/sector-second-line-template.md` must contain:

```markdown
# 板块第二梯队结构代理 · {{time}}

> 当前概念成分不是历史快照；日 K 无法还原时点价格、量比和盘口。本报告只用于结构研究。

样本区间：{{period}}

初始本金：{{initial_capital}}

## 口径

{{rules}}

## 打板股组

{{limit_summary}}

{{limit_trades}}

## 第二梯队组

{{second_line_summary}}

{{second_line_trades}}

## 数据完整性与限制

{{limitations}}

不构成投资建议
```

- [ ] **Step 4: Implement renderer and CLI**

```python
def render_rules(result):
    return "\n".join([
        "- D 日涨停后买入，D+1 开盘退出。",
        "- 打板组按 D 日涨停价成交，且执行时点后必须开板回封。",
        "- 第二梯队组按 D 日开盘价成交，候选只使用 D-1 及以前结构和 D 日开盘。",
        "- 两组各以 {} 元独立计算。".format(
            backtest.money(result["capital"])),
    ])


def report_paths(workspace, start, end, times):
    report_dir = workspace / "report" / "回测"
    paths = {
        time: report_dir / "板块第二梯队_{}_{}_{}.md".format(
            time[:5].replace(":", "-"), start, end)
        for time in times
    }
    paths["comparison"] = (
        report_dir / "板块第二梯队_时点对比_{}_{}.md".format(start, end)
    )
    return paths


def render_summary(group):
    summary = group["summary"]
    return "\n".join([
        "| 指标 | 结果 |",
        "|---|---:|",
        "| 成交数 | {} |".format(summary["trade_count"]),
        "| 方向胜率 | {} |".format(
            backtest.rate(summary["direction_win_rate_pct"])),
        "| 净收益胜率 | {} |".format(
            backtest.rate(summary["net_win_rate_pct"])),
        "| 期末本金 | {} 元 |".format(
            backtest.money(summary["ending_capital"])),
        "| 净收益率 | {} |".format(
            backtest.pct(summary["net_return_pct"])),
        "| 最大回撤 | {:.2f}% |".format(
            summary["max_drawdown_pct"]),
    ])


def render_trades(trades):
    lines = [
        "| 信号日 | 退出日 | 题材 | 代码 | 名称 | 入场 | 退出 | 当日封板 | 次日溢价 | 净收益 | 期末本金 |",
        "|---|---|---|---|---|---:|---:|---|---:|---:|---:|",
    ]
    for trade in trades:
        lines.append(
            "| {signal_date} | {exit_date} | {theme} | `{code}` | {name} | "
            "{entry_price:.2f} | {exit_price:.2f} | {sealed} | {premium} | "
            "{net_pnl:+.2f}元 | {capital_after:,.2f}元 |".format(
                sealed="是" if trade.get("sealed_close") else "否",
                premium=backtest.pct(
                    trade.get("next_open_premium_pct")),
                **trade,
            )
        )
    if not trades:
        lines.append("| - | - | - | - | 无成交 | - | - | - | - | - | - |")
    return "\n".join(lines)


def render_time_report(result):
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "time": result["time"],
        "period": "{} 至 {}".format(result["start"], result["end"]),
        "initial_capital": backtest.money(result["capital"]),
        "rules": render_rules(result),
        "limit_summary": render_summary(result["groups"]["limit"]),
        "limit_trades": render_trades(result["groups"]["limit"]["trades"]),
        "second_line_summary": render_summary(
            result["groups"]["second_line"]),
        "second_line_trades": render_trades(
            result["groups"]["second_line"]["trades"]),
        "limitations": "\n".join("- " + item for item in result["limitations"]),
    }
    for key, value in replacements.items():
        text = text.replace("{{%s}}" % key, value)
    return text


def render_comparison(results):
    lines = [
        "# 板块第二梯队时点对比",
        "",
        "| 时点 | 打板成交 | 打板期末 | 第二梯队成交 | 第二梯队期末 |",
        "|---|---:|---:|---:|---:|",
    ]
    for result in results:
        limit = result["groups"]["limit"]["summary"]
        second = result["groups"]["second_line"]["summary"]
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                result["time"], limit["trade_count"],
                backtest.money(limit["ending_capital"]),
                second["trade_count"],
                backtest.money(second["ending_capital"]),
            )
        )
    lines.extend(["", "不构成投资建议"])
    return "\n".join(lines)
```

CLI must parse:

```python
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--capital", type=float, default=10000.0)
ap.add_argument("--times", type=parse_times,
                default=("09:40:00", "10:00:00", "10:30:00", "11:00:00"))
ap.add_argument("--workers", type=int, default=4)
```

- [ ] **Step 5: Add the executable entry point**

```python
def main():
    ap = argparse.ArgumentParser(description="板块第二梯队结构代理回测")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--capital", type=float, default=10000.0)
    ap.add_argument(
        "--times",
        type=parse_times,
        default=("09:40:00", "10:00:00", "10:30:00", "11:00:00"),
    )
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if args.capital <= 0:
        ap.error("--capital 必须大于 0")

    start_date = backtest.parse_date(args.start)
    end_date = backtest.parse_date(args.end)
    dates, warmup_dates, _index_bars = backtest.get_trading_dates(
        start_date, end_date)
    if not dates:
        raise RuntimeError("区间内没有交易日")
    all_dates = warmup_dates + dates
    pools, errors = backtest.load_pools(all_dates, args.workers)
    backtest.apply_consecutive_board_counts(pools, all_dates)
    board_index = load_board_catalog()
    fees = {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_duty_rate": 0.0005,
        "transfer_rate": 0.00001,
    }
    limitations = [
        "当前概念成分不是历史快照，存在概念调整和幸存者偏差。",
        "日 K 无法还原四个时点的真实价格、量比和盘口。",
        "D 日开盘价是第二梯队组指定的历史代理买入价。",
    ]
    results = []
    bars_by_code = {}
    members_cache = {}
    for time in args.times:
        result = run_time_point(
            time, pools, dates, board_index, args.capital, fees,
            bars_by_code, members_cache)
        result.update({
            "start": args.start,
            "end": args.end,
            "capital": args.capital,
            "limitations": limitations,
            "pool_errors": errors,
        })
        results.append(result)
        path = report_paths(
            Path.cwd(), args.start, args.end, args.times)[time]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_time_report(result), encoding="utf-8")
    comparison_path = report_paths(
        Path.cwd(), args.start, args.end, args.times)["comparison"]
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        render_comparison(results), encoding="utf-8")
    print("报告：{}".format(comparison_path))


if __name__ == "__main__":
    http_util.run(main)
```

- [ ] **Step 6: Run tests to verify pass**

Run: `python -m unittest tests.test_sector_second_line.ReportTest -v`
Expected: PASS.

- [ ] **Step 7: Verify CLI help**

Run: `python backtest/limit-hit/backtest_sector_second_line.py --help`
Expected: exit code 0 and all arguments shown.

- [ ] **Step 8: Commit**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py \
  backtest/limit-hit/sector-second-line-template.md \
  tests/test_sector_second_line.py
git commit -m "feat: report sector second-line backtest"
```

---

### Task 6: 历史数据集成、2026 实跑与回归

**Files:**

- Modify: `backtest/limit-hit/backtest_sector_second_line.py`
- Modify: `docs/superpowers/specs/2026-11-19-limit-hit-sector-second-line-design.md` only if execution reveals a real data limitation.

**Interfaces:**

- Consumes: all previous functions plus network-backed `fetch_limit_up_history`, `fetch_stock_structure.fetch_kline`, `fetch_sector_boards`, `fallback_sources.fetch_f10_board_members`.
- Produces: four per-time reports and one comparison report under `report/回测/`.

- [ ] **Step 1: Add integration functions with injectable fetchers**

```python
def load_board_catalog(fetch_boards=None):
    fetch_boards = fetch_boards or default_board_catalog
    return build_board_index(fetch_boards())


def candidate_feature(code, data_date, bars_by_code, load_bars=None):
    load_bars = load_bars or default_load_bars
    if code not in bars_by_code:
        bars_by_code[code] = load_bars(code)
    return bar_features(bars_by_code[code], data_date)


def bars_at(bars, data_date):
    for index, bar in enumerate(bars):
        if bar.get("date") == data_date:
            next_bar = bars[index + 1] if index + 1 < len(bars) else None
            return bar, next_bar
    return None, None


def signal_from_candidate(candidate, bars, group):
    day, next_bar = bars_at(bars, candidate["data_date"])
    if not day or not next_bar or day.get("close") is None or next_bar.get("open") is None:
        return None
    entry_price = day["close"] if group == "limit" else day["open"]
    if entry_price is None:
        return None
    outcome = candidate_outcome(candidate, next_bar)
    return {
        "date": candidate["data_date"],
        "exit_date": next_bar["date"],
        "theme": candidate["theme"],
        "theme_score": candidate["theme_score"],
        "code": candidate["code"],
        "name": candidate["name"],
        "group": group,
        "entry_price": entry_price,
        "exit_price": next_bar["open"],
        "outcome": outcome,
    }


def select_candidates_for_day(
        time, data_date, next_date, themes, current_rows, board_index,
        bars_by_code, load_bars, load_members, members_cache):
    signals = []
    skipped = []
    visible_codes = {row["code"] for row in current_rows}
    for theme_item in themes:
        theme = theme_item["theme"]
        limit_rows = [
            row for row in current_rows
            if theme in backtest.reason_tags(row)
            and backtest.candidate_eligible(row)
            and backtest.entry_visible(
                row, time, require_reseal_after=True)
        ]
        if limit_rows:
            selected = max(
                limit_rows,
                key=lambda row: (
                    backtest.candidate_score(row),
                    row.get("board_count") or 0,
                    -len(str(row.get("first_seal") or "")),
                ),
            )
            bars = bars_by_code.get(selected["code"])
            if bars is None:
                bars = load_bars(selected["code"])
                bars_by_code[selected["code"]] = bars
            feature = bar_features(bars, data_date)
            if feature:
                feature.update({
                    "theme": theme,
                    "theme_score": theme_item["theme_score"],
                    "code": selected["code"],
                    "name": selected.get("name"),
                    "limit_price": feature["close"],
                })
                signal = signal_from_candidate(feature, bars, "limit")
                if signal:
                    signals.append(signal)
                else:
                    skipped.append({
                        "date": data_date, "group": "limit",
                        "code": selected["code"], "reason": "D+1 开盘缺失",
                    })

        board_code = board_index.get(theme)
        if not board_code:
            skipped.append({
                "date": data_date, "group": "second_line",
                "theme": theme, "reason": "未找到同名精确概念板块",
            })
            continue
        members = members_cache.get(board_code)
        if members is None:
            members = load_members(board_code)
            members_cache[board_code] = members
        candidates = []
        for member in members:
            if member["code"] in visible_codes:
                continue
            bars = bars_by_code.get(member["code"])
            if bars is None:
                bars = load_bars(member["code"])
                bars_by_code[member["code"]] = bars
            feature = bar_features(bars, data_date)
            if not second_line_eligible(feature):
                continue
            if not can_fill_second_line(feature):
                continue
            candidates.append({
                **feature,
                "code": member["code"],
                "name": member.get("name"),
                "theme": theme,
                "theme_score": theme_item["theme_score"],
            })
        if candidates:
            selected = max(
                candidates,
                key=lambda row: (
                    row["theme_score"],
                    -abs(row["open_gap_pct"] - 3.0),
                    row.get("previous_volume_ratio5") or 0.0,
                    -len(row["code"]),
                ),
            )
            bars = bars_by_code[selected["code"]]
            signal = signal_from_candidate(selected, bars, "second_line")
            if signal:
                signals.append(signal)
            else:
                skipped.append({
                    "date": data_date, "group": "second_line",
                    "code": selected["code"], "reason": "D+1 开盘缺失",
                })
    return dedupe_candidates(signals), skipped


def default_board_catalog():
    args = SimpleNamespace(
        type="concept",
        source="eastmoney",
        sort="change",
        top=10000,
        min_up=0,
        search=[],
    )
    _source, rows, _meta = sector_boards.prepare_rows(args)
    return rows


def default_load_bars(code):
    bars, _source, _limitations = fetch_stock_structure.fetch_kline(
        code, days=400, cutoff=None)
    return bars


def run_time_point(
        time, pools, dates, board_index, capital, fees,
        bars_by_code=None, members_cache=None):
    bars_by_code = bars_by_code if bars_by_code is not None else {}
    members_cache = members_cache if members_cache is not None else {}
    selected = []
    skipped = []
    for index, data_date in enumerate(dates):
        current = visible_limit_rows(pools.get(data_date, []), time)
        previous = pools.get(dates[index - 1], []) if index else []
        themes = theme_ladder(current, previous, min_score=50, max_themes=3)
        day_selected, day_skipped = select_candidates_for_day(
            time,
            data_date,
            dates[index + 1] if index + 1 < len(dates) else None,
            themes,
            current,
            board_index,
            bars_by_code,
            default_load_bars,
            member_rows,
            members_cache,
        )
        selected.extend(day_selected)
        skipped.extend(day_skipped)
    limit_group = run_group(
        [row for row in selected if row["group"] == "limit"],
        capital, fees,
    )
    second_group = run_group(
        [row for row in selected if row["group"] == "second_line"],
        capital, fees,
    )
    return {
        "time": time,
        "groups": {"limit": limit_group, "second_line": second_group},
        "skipped": skipped,
    }
```

`select_candidates_for_day` picks at most one candidate per group per theme,
deduplicates by code, skips missing `D+1` open, and retains every skip reason.

- [ ] **Step 2: Run offline integration test**

Add a test with injected pool rows, board index, members, bars and next-open bars. Assert:

- 09:40 excludes a stock first sealed at 09:41 from the limit group;
- the same stock can enter the second-line group if its D open is below limit;
- 11:00 includes only leaders first sealed by 11:00;
- both groups start from the same capital independently;
- report files use different names for all four times.

Run: `python -m unittest tests.test_sector_second_line -v`
Expected: PASS.

- [ ] **Step 3: Run the real 2026 backtest**

```bash
python backtest/limit-hit/backtest_sector_second_line.py \
  --start 20260101 --end 20261119 --capital 10000 \
  --times 09:40,10:00,10:30,11:00 --workers 4
```

Expected reports:

```text
report/回测/板块第二梯队_09-40_20260101_20261119.md
report/回测/板块第二梯队_10-00_20260101_20261119.md
report/回测/板块第二梯队_10-30_20260101_20261119.md
report/回测/板块第二梯队_11-00_20260101_20261119.md
report/回测/板块第二梯队_时点对比_20260101_20261119.md
```

The command may emit progress to stderr but must not prompt.

- [ ] **Step 4: Audit the generated reports**

Confirm:

- all four times exist and differ;
- every report states current concept membership is not historical;
- group capital starts at 10,000 independently;
- no report claims exact intraday prices;
- missing theme mappings are listed;
- all reports contain “不构成投资建议”.

- [ ] **Step 5: Run full verification**

```bash
python -m unittest discover -s tests -v
python -m compileall -q backtest/limit-hit limit-hit/scripts
git diff --check -- backtest/limit-hit tests/test_sector_second_line.py
```

Expected: all tests pass, compile succeeds, no whitespace errors in new files.

- [ ] **Step 6: Commit implementation**

```bash
git add backtest/limit-hit/backtest_sector_second_line.py \
  backtest/limit-hit/sector-second-line-template.md \
  tests/test_sector_second_line.py
git commit -m "feat: add sector second-line 2026 backtest"
```

Generated reports remain in the current workspace and are not committed if ignored by Git.

## Self-Review

- Spec coverage: Tasks 1-6 cover all four times, theme ladder, exact concept mapping, second-line hard conditions, independent capital, two groups, reporting, and real-data audit.
- Placeholder scan: no TBD/TODO placeholders; every task includes concrete tests, commands, and interfaces.
- Type consistency: `parse_times`, `visible_limit_rows`, `theme_ladder`, `bar_features`, `second_line_eligible`, `candidate_outcome`, `run_group`, `render_time_report`, and `render_comparison` retain the same names and shapes across tasks.
