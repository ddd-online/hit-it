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

import backtest_limit_hit as backtest  # noqa: E402


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
