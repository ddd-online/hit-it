#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于历史涨停池和日 K 的板块第二梯队结构代理回测。"""

import argparse
import collections
import sys
from datetime import datetime
from pathlib import Path


BACKTEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKTEST_DIR.parents[1]
sys.path.insert(0, str(BACKTEST_DIR))
sys.path.insert(0, str(REPO_ROOT / "limit-hit" / "scripts"))

import backtest_limit_hit as backtest  # noqa: E402
import fallback_sources  # noqa: E402
import fetch_sector_leaders as sector_leaders  # noqa: E402


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
        raw_code = str(
            item.get("code") or item.get("SECURITY_CODE") or "")
        code = (
            raw_code
            if raw_code.startswith(("sh", "sz", "bj"))
            else fallback_sources.infer_sec_code(raw_code)
        )
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
    closes = [
        bar.get("close") for bar in previous_window[-20:]
        if bar.get("close") is not None
    ]
    if len(closes) < 20:
        return None
    volumes = [
        bar.get("volume_hands") if bar.get("volume_hands") is not None
        else bar.get("volume")
        for bar in previous_window[-20:]
    ]
    previous_close = previous.get("close")
    if previous_close is None:
        return None
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    range20_low = min(bar["low"] for bar in previous_window[-20:])
    range20_high = max(bar["high"] for bar in previous_window[-20:])
    prior_volume_average = mean(volumes[-6:-1])
    return {
        "data_date": data_date,
        "previous_close": previous_close,
        "open": day.get("open"),
        "high": day.get("high"),
        "low": day.get("low"),
        "close": day.get("close"),
        "limit_price": round(previous_close * 1.10, 2),
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
            volumes[-1] / prior_volume_average, 3
        ) if len(volumes) >= 6 and prior_volume_average else None,
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
