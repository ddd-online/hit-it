#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 limit-hit 预计算当日候选上下文。

脚本只做数据装配、聚合和指标计算，不输出买卖结论：
- 当日涨停通过技能内取数脚本实拉；
- 昨日优先复用 report/涨停池 报告，缺失时用当日接口的昨日池或历史接口生成；
- 收盘后若今日涨停池不存在，保存今日报告供次日复用；
- 批量聚合精确概念和名称情绪组；
- 批量计算日K趋势、洗盘路径、量比和筹码。
"""

import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CN_TZ = timezone(timedelta(hours=8))
BOARD_PERMISSION = "沪深主板"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def run_json(script, *args):
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "{} 执行失败".format(script))
    return json.loads(completed.stdout)


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_date(day):
    return day.replace("-", "")


def build_report_path(data_date):
    return Path.cwd() / "report" / "打板候选" / "{}.md".format(data_date)


def build_limit_pool_path(data_date):
    return Path.cwd() / "report" / "涨停池" / "{}.json".format(data_date)


def is_mainboard_code(code):
    return str(code or "").startswith((
        "sh600", "sh601", "sh603", "sh605",
        "sz000", "sz001", "sz002", "sz003",
    ))


def select_candidates(rows):
    """固定板块权限：只保留当日一板、二板中的沪深主板股票。"""
    return [
        row for row in rows
        if row.get("board_count") in (1, 2)
        and is_mainboard_code(row.get("code"))
    ]


def load_limit_pool(path, data_date):
    """读取指定日期的涨停池；文件损坏或日期不符时直接报错，避免覆盖用户数据。"""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("涨停池文件读取失败：{}（{}）".format(path, exc))
    if payload.get("data_date") != data_date:
        raise RuntimeError("涨停池日期不匹配：{} 应为 {}".format(path, data_date))
    if not isinstance(payload.get("stocks"), list):
        raise RuntimeError("涨停池文件缺少 stocks：{}".format(path))
    return payload


def market_closed(snapshot, data_date, quote_date):
    """行情日期已结束且当日行情已取到时，才允许保存当日涨停池。"""
    if quote_date != compact_date(data_date):
        return False
    if snapshot.tzinfo is not None:
        snapshot = snapshot.astimezone(CN_TZ)
    if data_date < snapshot.date().isoformat():
        return True
    return data_date == snapshot.date().isoformat() and (snapshot.hour, snapshot.minute) >= (15, 0)


def normalize_pool_stock(row):
    def number(value, digits=2):
        value = to_float(value)
        return None if value is None else round(value, digits)

    seal_fund_yi = row.get("seal_fund_yi")
    if seal_fund_yi is None and row.get("seal_amount_yuan") is not None:
        seal_fund_yi = to_float(row["seal_amount_yuan"]) / 1e8
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "board_count": row.get("board_count"),
        "industry": row.get("industry"),
        "price": number(row.get("price")),
        "pct": number(row.get("pct") if row.get("pct") is not None else row.get("change_pct")),
        "amount_yi": number(row.get("amount_yi")),
        "float_cap_yi": number(row.get("float_cap_yi")),
        "total_cap_yi": number(row.get("total_cap_yi")),
        "turnover_pct": number(row.get("turnover_pct")),
        "first_seal": row.get("first_seal"),
        "last_seal": row.get("last_seal"),
        "break_count": row.get("break_count", row.get("open_count")),
        "seal_fund_yi": None if seal_fund_yi is None else round(seal_fund_yi, 2),
        "zt_stat": row.get("zt_stat"),
        "board_days": row.get("board_days"),
        "board_window_count": row.get("board_window_count"),
        "one_word": row.get("one_word"),
        "themes": row.get("themes") or [],
    }


def build_limit_pool_payload(data_date, rows, generated_at, source, source_name,
                             quote_date, market, limitations):
    """生成模型可直接复用的涨停池摘要与逐股事实。"""
    stocks = sorted(
        (normalize_pool_stock(row) for row in rows),
        key=lambda row: (
            -(row.get("board_count") or 0),
            row.get("first_seal") or "99:99:99",
            row.get("code") or "",
        ),
    )
    board_counts = collections.Counter()
    industry_counts = collections.Counter()
    theme_rows = collections.defaultdict(list)
    for row in stocks:
        board_count = row.get("board_count")
        board_counts[str(board_count) if board_count else "未获取"] += 1
        industry_counts[row.get("industry") or "未获取"] += 1
        for theme in row.get("themes") or []:
            theme_rows[theme].append(row)
    max_board = max((row.get("board_count") or 0 for row in stocks), default=None)
    max_stocks = [
        {"code": row["code"], "name": row["name"]}
        for row in stocks if max_board and row.get("board_count") == max_board
    ]
    themes = [
        {
            "theme": theme,
            "count": len(items),
            "max_board": max((row.get("board_count") or 0 for row in items), default=0),
            "codes": [row["code"] for row in items],
        }
        for theme, items in theme_rows.items()
        if len(items) >= 2
    ]
    themes.sort(key=lambda item: (-item["count"], -item["max_board"], item["theme"]))
    return {
        "schema": "limit-hit.limit-pool.v1",
        "data_date": data_date,
        "generated_at": generated_at,
        "quote_date": quote_date,
        "closed": True,
        "source": source,
        "source_name": source_name,
        "market": market,
        "summary": {
            "count": len(stocks),
            "max_board": max_board,
            "max_board_stocks": max_stocks,
            "board_counts": dict(sorted(board_counts.items())),
            "industries": [
                {"industry": industry, "count": count}
                for industry, count in industry_counts.most_common(10)
            ],
            "themes": themes,
        },
        "stocks": stocks,
        "limitations": limitations,
        "note": "本文件只保存涨停池事实和题材归属，不包含买卖判断；summary.themes 只列至少 2 只股票的题材，单票题材见 stocks。",
    }


def write_limit_pool(path, payload):
    """只在文件不存在时写入，已有涨停池始终复用。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def minutes_since(snapshot, clock):
    if not clock:
        return None
    try:
        hour, minute, second = [int(value) for value in clock.split(":")]
    except (TypeError, ValueError):
        return None
    seal = snapshot.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if seal > snapshot:
        return None
    return int((snapshot - seal).total_seconds() // 60)


def hydrate_concepts(codes):
    if not codes:
        return {}
    payload = run_json(
        "fetch_stock_structure.py", "--concepts-only", ",".join(codes), "--json")
    return payload.get("codes") or {}


def themes_for(code, concepts):
    rows = concepts.get(code, [])
    themes = [
        row.get("board_name")
        for row in rows
        if row.get("is_precise") and row.get("board_name")
    ]
    name = next(
        (row.get("security_name") for row in rows if row.get("security_name")), "")
    if "东方" in name:
        themes.append("名称组:东方系")
    if any(char in name for char in "羊洋阳扬"):
        themes.append("名称组:羊字辈")
    return list(dict.fromkeys(themes))


def row_themes(row, concepts):
    if "themes" in row:
        return row.get("themes") or []
    return themes_for(row.get("code"), concepts)


def theme_summary(current_rows, previous_rows, concepts):
    current_by_theme = collections.defaultdict(list)
    previous_by_theme = collections.defaultdict(list)
    for row in current_rows:
        for theme in row_themes(row, concepts):
            current_by_theme[theme].append(row)
    for row in previous_rows:
        for theme in row_themes(row, concepts):
            previous_by_theme[theme].append(row)
    previous_codes = {row["code"] for row in previous_rows}
    output = []
    for theme, rows in current_by_theme.items():
        if len(rows) < 2:
            continue
        boards = collections.Counter(row.get("board_count") or 0 for row in rows)
        output.append({
            "theme": theme,
            "current_count": len(rows),
            "previous_count": len(previous_by_theme.get(theme, [])),
            "promoted_count": sum(row["code"] in previous_codes for row in rows),
            "max_board": max((row.get("board_count") or 0 for row in rows), default=0),
            "board_counts": dict(sorted(boards.items())),
            "current_codes": [row["code"] for row in rows],
            "candidate_codes": [
                row["code"] for row in rows
                if row.get("board_count") in (1, 2)
                and is_mainboard_code(row.get("code"))
            ],
        })
    return sorted(
        output,
        key=lambda item: (
            item["current_count"], item["promoted_count"], item["max_board"]),
        reverse=True,
    )


def find_yesterday_high(previous_rows, current_data):
    max_board = max((row.get("board_count") or 0 for row in previous_rows), default=None)
    if not max_board:
        return None
    stocks = [row for row in previous_rows if row.get("board_count") == max_board]
    performance = {
        row["code"]: row
        for row in (current_data.get("yesterday_performance") or {}).get("rows", [])
    }
    output = []
    for stock in stocks:
        today = performance.get(stock["code"]) or {}
        output.append({
            "code": stock["code"],
            "name": stock.get("name"),
            "board_count": max_board,
            "today_open": today.get("today_open"),
            "gap_pct": today.get("gap_pct"),
            "today_pct": today.get("today_pct"),
            "close_state": today.get("close_state"),
            "continue_limit": today.get("continue_limit"),
        })
    return {"board_count": max_board, "stocks": output}


def mode_feedback(previous_rows, current_data):
    """按昨日板数计算一进二、二进三反馈，缺少板数的样本不猜测。"""
    performance = {
        row["code"]: row
        for row in (current_data.get("yesterday_performance") or {}).get("rows", [])
    }

    def summarize(mode_name, board_count):
        previous_group = [
            row for row in previous_rows
            if row.get("board_count") == board_count
        ]
        rows = [
            performance[row["code"]]
            for row in previous_group
            if row["code"] in performance
        ]
        gaps = [row["gap_pct"] for row in rows if row.get("gap_pct") is not None]
        closes = [
            row["today_pct"] for row in rows
            if row.get("today_pct") is not None
        ]
        promoted = sum(bool(row.get("continue_limit")) for row in rows)
        high_open = sum(value > 0 for value in gaps)
        return {
            "mode": mode_name,
            "sample": len(previous_group),
            "quoted": len(gaps),
            "matched_performance": len(rows),
            "promoted": promoted,
            "promotion_rate_pct": (
                round(promoted * 100.0 / len(previous_group), 2)
                if previous_group else None
            ),
            "high_open": high_open,
            "high_open_rate_pct": (
                round(high_open * 100.0 / len(gaps), 2) if gaps else None
            ),
            "avg_open_pct": (
                round(sum(gaps) / len(gaps), 2) if gaps else None
            ),
            "avg_close_pct": (
                round(sum(closes) / len(closes), 2) if closes else None
            ),
        }

    return {
        "one_to_two": summarize("一进二", 1),
        "two_to_three": summarize("二进三", 2),
    }


def build_payload():
    snapshot = datetime.now(CN_TZ)
    current = run_json("fetch_limit_up.py", "--json")
    data_date = "{}-{}-{}".format(
        current["base_date"][:4], current["base_date"][4:6], current["base_date"][6:])
    previous_date = "{}-{}-{}".format(
        current["prev_date"][:4], current["prev_date"][4:6], current["prev_date"][6:])

    current_rows = current.get("zt_rows") or []
    previous_path = build_limit_pool_path(previous_date)
    previous_pool = load_limit_pool(previous_path, previous_date)
    previous_generated = False
    if previous_pool:
        previous_rows = previous_pool["stocks"]
        previous_source = "涨停池报告：{}".format(
            previous_pool.get("source_name") or previous_pool.get("source") or "未获取")
    else:
        previous_rows = current.get("prev_zt_rows") or []
        previous_source_name = "东方财富 push2ex 昨日涨停池"
        previous_source = "eastmoney-prev-pool"
        previous_limitations = [
            "来自当日接口随带的昨日涨停池",
            "涨停池不含 ST 与科创板",
        ]
        if not previous_rows:
            previous_source = "10jqka-history"
            history = run_json(
                "fetch_limit_up_history.py",
                "--date", compact_date(previous_date),
                "--json",
            )
            previous_rows = history.get("rows") or []
            previous_source_name = history.get("source") or "同花顺公开历史涨停池"
            previous_limitations = history.get("limitations") or []
            previous_limitations.append(
                "同花顺历史池只提供窗口涨停次数，部分当前连板数未获取；"
                "未确认板数的样本不得进入一进二、二进三和最高板结论"
            )

    codes_to_hydrate = {
        row["code"] for row in current_rows
        if "themes" not in row
    } | {
        row["code"] for row in previous_rows
        if "themes" not in row
    }
    concepts = hydrate_concepts(sorted(codes_to_hydrate))
    for row in current_rows:
        if "themes" not in row:
            row["themes"] = themes_for(row["code"], concepts)
    for row in previous_rows:
        if "themes" not in row:
            row["themes"] = themes_for(row["code"], concepts)

    overview = current.get("overview") or {}
    if not previous_pool:
        previous_market = {
            "zt_count": overview.get("prev_zt_count"),
            "dt_count": overview.get("prev_dt_count"),
            "zb_count": overview.get("prev_zb_count"),
            "seal_rate_pct": overview.get("prev_seal_rate_pct"),
        }
        previous_pool = build_limit_pool_payload(
            data_date=previous_date,
            rows=previous_rows,
            generated_at=snapshot.isoformat(timespec="seconds"),
            source=previous_source,
            source_name=previous_source_name,
            quote_date=compact_date(previous_date),
            market=previous_market,
            limitations=previous_limitations + [
                "题材来自生成时 F10 精确概念，历史归属可能变动",
            ],
        )
        previous_generated = write_limit_pool(previous_path, previous_pool)

    current_path = build_limit_pool_path(data_date)
    current_generated = False
    if market_closed(snapshot, data_date, current.get("quote_date")):
        if current_path.exists():
            load_limit_pool(current_path, data_date)
        else:
            for row in current_rows:
                if row["code"] in (current.get("one_word") or {}):
                    row["one_word"] = current["one_word"][row["code"]]
            current_pool = build_limit_pool_payload(
                data_date=data_date,
                rows=current_rows,
                generated_at=snapshot.isoformat(timespec="seconds"),
                source="eastmoney-push2ex",
                source_name="东方财富 push2ex 涨停池",
                quote_date=current.get("quote_date"),
                market=overview,
                limitations=[
                    "涨停池不含 ST 与科创板",
                    "题材来自生成时 F10 精确概念，后续可能变动",
                ],
            )
            current_generated = write_limit_pool(current_path, current_pool)

    themes = theme_summary(current_rows, previous_rows, concepts)
    yesterday_high = find_yesterday_high(previous_rows, current)
    feedback = mode_feedback(previous_rows, current)

    one_two_candidates = [
        row for row in current_rows
        if row.get("board_count") in (1, 2)
    ]
    candidates = select_candidates(current_rows)
    board_permission_excluded = len(one_two_candidates) - len(candidates)
    metric_payload = run_json(
        "fetch_stock_metrics.py",
        "--codes", ",".join(row["code"] for row in candidates),
        "--days", "120",
        "--workers", "12",
        "--json",
    ) if candidates else {"rows": []}
    metrics = {row["code"]: row for row in metric_payload.get("rows", [])}

    for row in candidates:
        metric = metrics.get(row["code"]) or {}
        last = metric.get("last") or {}
        amount = last.get("amount_yuan")
        if not amount and row.get("amount_yi") is not None:
            amount = row["amount_yi"] * 1e8
        seal_amount = (row.get("seal_fund_yi") or 0) * 1e8 if row.get("seal_fund_yi") is not None else None
        row["metrics"] = metric
        row["minutes_since_first_seal"] = minutes_since(snapshot, row.get("first_seal"))
        row["minutes_since_last_seal"] = minutes_since(snapshot, row.get("last_seal"))
        row["is_reseal"] = bool(row.get("break_count") and row["break_count"] > 0)
        row["seal_fund_ratio_pct"] = round(seal_amount * 100.0 / amount, 2) if seal_amount is not None and amount else None
        warnings = []
        if current.get("one_word", {}).get(row["code"]):
            warnings.append("一字板")
        if row.get("break_count") is not None and row["break_count"] >= 5:
            warnings.append("开板次数>=5")
        if row.get("first_seal") and row["first_seal"] > "14:00:00":
            warnings.append("尾盘首封")
        if (row.get("turnover_pct") or 0) > 35:
            warnings.append("换手>35%")
        if not metric or metric.get("error"):
            warnings.append("日K或筹码未获取")
        row["warnings"] = warnings

    quality_summary = collections.Counter(
        (row.get("metrics") or {}).get("quality") or "unavailable"
        for row in candidates
    )
    limitations = [
        "本脚本只计算，不做题材取舍或可打板判断",
        "题材来自当前 F10 精确概念，历史归属可能有变动",
        "筹码为日K+换手率推演值",
    ]
    if quality_summary["degraded"]:
        limitations.append(
            "{} 只候选的换手率与筹码使用近似值，筹码结构只能作为降级证据".format(
                quality_summary["degraded"])
        )

    return {
        "generated_at": snapshot.isoformat(timespec="seconds"),
        "data_date": data_date,
        "report_path": str(build_report_path(data_date)),
        "previous_date": previous_date,
        "quote_date": current.get("quote_date"),
        "board_permission": BOARD_PERMISSION,
        "board_permission_excluded_count": board_permission_excluded,
        "previous_source": previous_source,
        "previous_pool_report": str(previous_path),
        "previous_pool_generated": previous_generated,
        "previous_context": previous_pool.get("summary"),
        "current_pool_report": str(current_path),
        "current_pool_generated": current_generated,
        "market": current.get("overview"),
        "yesterday_open": (current.get("yesterday_open_close") or {}).get("overall"),
        "yesterday_highest_board": yesterday_high,
        "mode_feedback": feedback,
        "metric_quality_summary": dict(sorted(quality_summary.items())),
        "themes": themes,
        "candidates": candidates,
        "limitations": limitations,
    }


def build_compact_payload(payload, theme_limit=30):
    """把完整预计算压缩为模型决策所需字段，保留原始 --json 供审计。"""
    def number(value, digits=2):
        value = to_float(value)
        return None if value is None else round(value, digits)

    all_themes = payload.get("themes") or []
    themes = [
        {
            "theme": row.get("theme"),
            "current_count": row.get("current_count"),
            "previous_count": row.get("previous_count"),
            "promoted_count": row.get("promoted_count"),
            "max_board": row.get("max_board"),
            "board_counts": row.get("board_counts"),
            "candidate_codes": row.get("candidate_codes"),
        }
        for row in all_themes[:max(0, theme_limit)]
    ]
    candidates = []
    for row in payload.get("candidates") or []:
        metric = row.get("metrics") or {}
        returns = metric.get("returns_pct") or {}
        ma = metric.get("ma") or {}
        chips = metric.get("chips") or {}
        candidates.append({
            "code": row.get("code"),
            "name": row.get("name"),
            "themes": row.get("themes") or [],
            "board_count": row.get("board_count"),
            "zt_stat": row.get("zt_stat"),
            "price": number(row.get("price")),
            "pct": number(row.get("pct")),
            "amount_yi": number(row.get("amount_yi")),
            "float_cap_yi": number(row.get("float_cap_yi")),
            "total_cap_yi": number(row.get("total_cap_yi")),
            "turnover_pct": number(row.get("turnover_pct")),
            "first_seal": row.get("first_seal"),
            "last_seal": row.get("last_seal"),
            "break_count": row.get("break_count"),
            "seal_fund_yi": number(row.get("seal_fund_yi")),
            "seal_fund_ratio_pct": number(row.get("seal_fund_ratio_pct")),
            "minutes_since_first_seal": row.get("minutes_since_first_seal"),
            "minutes_since_last_seal": row.get("minutes_since_last_seal"),
            "is_reseal": row.get("is_reseal"),
            "returns_pct": {
                "5d": number(returns.get("5d")),
                "10d": number(returns.get("10d")),
                "20d": number(returns.get("20d")),
            },
            "ma": {
                "5": number(ma.get("5"), 3),
                "10": number(ma.get("10"), 3),
                "20": number(ma.get("20"), 3),
            },
            "open_gap_pct": number(metric.get("open_gap_pct")),
            "low_gap_pct": number(metric.get("low_gap_pct")),
            "volume_ratio_5d": number(metric.get("volume_ratio_5d")),
            "one_word": metric.get("one_word"),
            "chips": {
                "avg_cost": number(chips.get("avg_cost"), 3),
                "peak_price": number(chips.get("peak_price"), 3),
                "cost70_low": number(chips.get("cost70_low"), 3),
                "cost70_high": number(chips.get("cost70_high"), 3),
                "cost90_low": number(chips.get("cost90_low"), 3),
                "cost90_high": number(chips.get("cost90_high"), 3),
                "concentration70_pct": number(chips.get("concentration70_pct")),
                "concentration90_pct": number(chips.get("concentration90_pct")),
                "profit_ratio_pct": number(chips.get("profit_ratio_pct")),
                "above_avg_pct": number(chips.get("above_avg_pct")),
            } if chips else None,
            "metric_source": metric.get("source"),
            "metric_quality": metric.get("quality"),
            "metric_limitations": metric.get("limitations") or [],
            "warnings": row.get("warnings") or [],
        })
    previous_context = payload.get("previous_context") or {}
    compact_previous_context = {
        "count": previous_context.get("count"),
        "max_board": previous_context.get("max_board"),
        "max_board_stocks": previous_context.get("max_board_stocks"),
        "board_counts": previous_context.get("board_counts"),
        "top_themes": (previous_context.get("themes") or [])[:10],
    }
    return {
        "schema": "limit-hit.candidates.compact.v1",
        "generated_at": payload.get("generated_at"),
        "data_date": payload.get("data_date"),
        "quote_date": payload.get("quote_date"),
        "previous_date": payload.get("previous_date"),
        "board_permission": payload.get("board_permission"),
        "board_permission_excluded_count": payload.get(
            "board_permission_excluded_count"),
        "report_path": payload.get("report_path"),
        "previous_source": payload.get("previous_source"),
        "previous_pool_report": payload.get("previous_pool_report"),
        "previous_pool_generated": payload.get("previous_pool_generated"),
        "previous_context": compact_previous_context,
        "current_pool_report": payload.get("current_pool_report"),
        "current_pool_generated": payload.get("current_pool_generated"),
        "market": payload.get("market"),
        "yesterday_open": payload.get("yesterday_open"),
        "yesterday_highest_board": payload.get("yesterday_highest_board"),
        "mode_feedback": payload.get("mode_feedback"),
        "metric_quality_summary": payload.get("metric_quality_summary"),
        "theme_total_count": len(all_themes),
        "themes_truncated": len(themes) < len(all_themes),
        "themes": themes,
        "candidates": candidates,
        "limitations": payload.get("limitations") or [],
        "note": "候选指标已扁平化和四舍五入；完整原始字段使用 --json",
    }


def print_text(payload):
    print("=" * 112)
    print("limit-hit 候选上下文 · {} · 行情 {} · 快照 {}".format(
        payload["data_date"], payload.get("quote_date") or "-",
        payload.get("generated_at") or "-"))
    print("报告文件：{}".format(payload.get("report_path") or "-"))
    print("板块权限：{}（权限外 1/2 板排除 {} 只）".format(
        payload.get("board_permission"),
        payload.get("board_permission_excluded_count")))
    print("=" * 112)
    print("昨日来源：{}".format(payload["previous_source"]))
    print("昨日涨停池：{}（新生成：{}）".format(
        payload.get("previous_pool_report") or "-",
        "是" if payload.get("previous_pool_generated") else "否"))
    print("今日涨停池：{}（新生成：{}）".format(
        payload.get("current_pool_report") or "-",
        "是" if payload.get("current_pool_generated") else "否"))
    market = payload.get("market") or {}
    print("市场：涨停 {} / 跌停 {} / 炸板 {} / 封板率 {}%".format(
        market.get("zt_count"), market.get("dt_count"), market.get("zb_count"),
        market.get("seal_rate_pct")))
    print("连板高度：最高 {} 板 / 2板及以上 {} 家 / 板数分布 {}".format(
        market.get("max_board"), market.get("two_plus_count"),
        json.dumps(market.get("board_counts") or {}, ensure_ascii=False)))
    opening = payload.get("yesterday_open") or {}
    print("昨日涨停今日开盘：高开 {} / 平开 {} / 低开 {} / 平均 {}%".format(
        opening.get("up_open"), opening.get("flat_open"),
        opening.get("down_open"), opening.get("avg_gap_pct")))
    feedback = payload.get("mode_feedback") or {}
    for key in ("one_to_two", "two_to_three"):
        row = feedback.get(key) or {}
        print("{}：样本 {} / 晋级率 {}% / 高开率 {}% / 平均开盘 {}%".format(
            row.get("mode"), row.get("sample"),
            row.get("promotion_rate_pct"), row.get("high_open_rate_pct"),
            row.get("avg_open_pct")))
    high = payload.get("yesterday_highest_board")
    if high:
        print("昨日最高板：{} 板 {}".format(high["board_count"], "、".join(
            "{}（{}）".format(item["name"], item["code"]) for item in high["stocks"])))
    print("\n题材聚合（仅计算，不判定强弱）")
    print("{:<30}{:>8}{:>8}{:>8}{:>8}".format("题材", "今日", "昨日", "晋级", "最高板"))
    for row in payload["themes"][:30]:
        print("{:<30}{:>8}{:>8}{:>8}{:>8}".format(
            row["theme"][:28], row["current_count"], row["previous_count"],
            row["promoted_count"], row["max_board"]))
    print("\n1/2 板候选（仅计算，不判定可打板）")
    print("{:<10} | {:<8} | {:>2} | {:>8} | {:>7} | {:>4} | {:>7} | {:>7} | {:>8} | {:>8}".format(
        "代码", "名称", "板", "首封", "距首封m", "开板", "换手%", "量比5", "获利盘%", "封单/额%"))
    for row in payload["candidates"]:
        metric = row.get("metrics") or {}
        chips = metric.get("chips") or {}
        print("{:<10} | {:<8} | {:>2} | {:>8} | {:>7} | {:>4} | {:>7} | {:>7} | {:>8} | {:>8}".format(
            row["code"],
            str(row.get("name") or "-")[:6],
            row.get("board_count") or "-",
            row.get("first_seal") or "-",
            row.get("minutes_since_first_seal") if row.get("minutes_since_first_seal") is not None else "-",
            row.get("break_count") if row.get("break_count") is not None else "-",
            "-" if row.get("turnover_pct") is None else "{:.2f}".format(row["turnover_pct"]),
            "-" if metric.get("volume_ratio_5d") is None else "{:.2f}".format(metric["volume_ratio_5d"]),
            "-" if chips.get("profit_ratio_pct") is None else "{:.2f}".format(chips["profit_ratio_pct"]),
            "-" if row.get("seal_fund_ratio_pct") is None else "{:.2f}".format(row["seal_fund_ratio_pct"]),
        ))
    print("\n" + "\n".join("注：{}".format(item) for item in payload["limitations"]))
    quality = payload.get("metric_quality_summary") or {}
    if quality:
        print("注：个股指标质量 {}".format(json.dumps(
            quality, ensure_ascii=False, sort_keys=True)))


def main():
    ap = argparse.ArgumentParser(description="limit-hit 候选上下文预计算")
    output = ap.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="输出完整 JSON，供审计和调试")
    output.add_argument("--compact", action="store_true", help="输出模型友好的单行紧凑 JSON")
    args = ap.parse_args()
    payload = build_payload()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.compact:
        print(json.dumps(
            build_compact_payload(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ))
    else:
        print_text(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.exit("错误：{}".format(exc))
