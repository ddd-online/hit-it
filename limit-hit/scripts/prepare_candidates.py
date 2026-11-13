#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 limit-hit 预计算当日候选上下文。

脚本只做数据装配、聚合和指标计算，不输出买卖结论：
- 当日涨停通过技能内取数脚本实拉；
- 昨日优先复用 limit-find 报告，缺失时按 prev_date 回拉；
- 批量聚合精确概念和名称情绪组；
- 批量计算日K趋势、洗盘路径、量比和筹码。
"""

import argparse
import collections
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CODE_RE = re.compile(r"\b(?:sh|sz|bj)\d{6}\b", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


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


def split_row(line):
    text = line.strip()
    if not text.startswith("|"):
        return None
    return [cell.strip() for cell in text.strip("|").split("|")]


def parse_report(path):
    text = path.read_text(encoding="utf-8")
    date_match = DATE_RE.search(text[:300])
    data_date = date_match.group(1) if date_match else path.stem[:10]
    lines = text.splitlines()
    start = next(
        (idx + 1 for idx, line in enumerate(lines)
         if line.startswith("## 涨停全名单")),
        None,
    )
    if start is None:
        raise ValueError("报告缺少涨停全名单")
    section = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        section.append(line)
    header_index = None
    header = None
    for idx, line in enumerate(section):
        cells = split_row(line)
        if cells and "代码" in cells and "板数" in cells:
            header_index, header = idx, cells
            break
    if header is None:
        raise ValueError("涨停全名单表头未识别")
    rows = []
    for line in section[header_index + 1:]:
        cells = split_row(line)
        if not cells:
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))
        item = dict(zip(header, cells))
        code_match = CODE_RE.search(item.get("代码", ""))
        if not code_match:
            continue
        try:
            board_count = int(item.get("板数") or 0)
        except ValueError:
            board_count = None
        rows.append({
            "code": code_match.group(0).lower(),
            "name": item.get("名称"),
            "board_count": board_count,
            "industry": item.get("行业"),
            "amount_yi": to_float(item.get("成交额(亿)")),
            "first_seal": item.get("首封时间") or None,
            "reason_tags": [],
        })
    mainline = re.search(r"^- 主线：(.+)$", text, re.MULTILINE)
    formation = re.search(r"^- 阵型：(.+)$", text, re.MULTILINE)
    structure = re.search(r"^- 结构说明：(.+)$", text, re.MULTILINE)
    return {
        "data_date": data_date,
        "rows": rows,
        "mainline": mainline.group(1).strip() if mainline else None,
        "formation": formation.group(1).strip() if formation else None,
        "formation_structure": structure.group(1).strip() if structure else None,
    }


def find_previous_report(data_date):
    report_dir = Path.cwd() / "report" / "涨停板复盘"
    if not report_dir.exists():
        return None
    candidates = []
    for path in report_dir.glob("*.md"):
        match = DATE_RE.fullmatch(path.stem)
        if match and path.stem < data_date:
            candidates.append((path.stem, path))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_date(day):
    return day.replace("-", "")


def build_report_path(data_date):
    return Path.cwd() / "report" / "打板候选" / "{}.md".format(data_date)


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


def theme_summary(current_rows, previous_rows, concepts):
    current_by_theme = collections.defaultdict(list)
    previous_by_theme = collections.defaultdict(list)
    for row in current_rows:
        for theme in themes_for(row["code"], concepts):
            current_by_theme[theme].append(row)
    for row in previous_rows:
        for theme in themes_for(row["code"], concepts):
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
                row["code"] for row in rows if row.get("board_count") in (1, 2)
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


def build_payload():
    snapshot = datetime.now().astimezone()
    current = run_json("fetch_limit_up.py", "--json")
    data_date = "{}-{}-{}".format(
        current["base_date"][:4], current["base_date"][4:6], current["base_date"][6:])
    previous_date = "{}-{}-{}".format(
        current["prev_date"][:4], current["prev_date"][4:6], current["prev_date"][6:])

    previous_report = find_previous_report(data_date)
    previous_source = "limit-find-report"
    previous_rows = []
    previous_context = {}
    if previous_report:
        try:
            parsed_report = parse_report(previous_report)
            if parsed_report["data_date"] == previous_date:
                previous_rows = parsed_report["rows"]
                previous_context = {
                    "mainline": parsed_report["mainline"],
                    "formation": parsed_report["formation"],
                    "formation_structure": parsed_report["formation_structure"],
                }
            else:
                previous_rows = []
        except Exception:  # noqa: BLE001 坏报告回退接口
            previous_rows = []
    if not previous_rows:
        previous_source = "limit-hit-history"
        previous_payload = run_json(
            "fetch_limit_up_history.py", "--date", compact_date(previous_date), "--json")
        previous_rows = previous_payload.get("rows") or []

    all_codes = sorted({
        row["code"] for row in current.get("zt_rows", []) + previous_rows
    })
    concepts = hydrate_concepts(all_codes)
    themes = theme_summary(current.get("zt_rows", []), previous_rows, concepts)
    yesterday_high = find_yesterday_high(previous_rows, current)

    candidates = [
        row for row in current.get("zt_rows", [])
        if row.get("board_count") in (1, 2)
    ]
    metric_payload = run_json(
        "fetch_stock_metrics.py",
        "--codes", ",".join(row["code"] for row in candidates),
        "--days", "120",
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
        row["themes"] = themes_for(row["code"], concepts)
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

    return {
        "generated_at": snapshot.isoformat(timespec="seconds"),
        "data_date": data_date,
        "report_path": str(build_report_path(data_date)),
        "previous_date": previous_date,
        "quote_date": current.get("quote_date"),
        "previous_source": previous_source,
        "previous_report": str(previous_report) if previous_report else None,
        "previous_context": previous_context,
        "market": current.get("overview"),
        "yesterday_open": (current.get("yesterday_open_close") or {}).get("overall"),
        "yesterday_highest_board": yesterday_high,
        "themes": themes,
        "candidates": candidates,
        "limitations": [
            "本脚本只计算，不做题材取舍或可打板判断",
            "题材来自当前 F10 精确概念，历史归属可能有变动",
            "筹码为日K+换手率推演值",
        ],
    }


def build_compact_payload(payload):
    """把完整预计算压缩为模型决策所需字段，保留原始 --json 供审计。"""
    def number(value, digits=2):
        value = to_float(value)
        return None if value is None else round(value, digits)

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
        for row in payload.get("themes") or []
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
            "metric_limitations": metric.get("limitations") or [],
            "warnings": row.get("warnings") or [],
        })
    return {
        "schema": "limit-hit.candidates.compact.v1",
        "generated_at": payload.get("generated_at"),
        "data_date": payload.get("data_date"),
        "quote_date": payload.get("quote_date"),
        "previous_date": payload.get("previous_date"),
        "report_path": payload.get("report_path"),
        "previous_source": payload.get("previous_source"),
        "previous_report": payload.get("previous_report"),
        "previous_context": payload.get("previous_context"),
        "market": payload.get("market"),
        "yesterday_open": payload.get("yesterday_open"),
        "yesterday_highest_board": payload.get("yesterday_highest_board"),
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
    print("=" * 112)
    print("昨日来源：{} {}".format(
        payload["previous_source"], payload.get("previous_report") or ""))
    previous_context = payload.get("previous_context") or {}
    if previous_context.get("mainline"):
        print("昨日主线：{}".format(previous_context["mainline"]))
    if previous_context.get("formation"):
        print("昨日阵型：{}".format(previous_context["formation"]))
    market = payload.get("market") or {}
    print("市场：涨停 {} / 跌停 {} / 炸板 {} / 封板率 {}%".format(
        market.get("zt_count"), market.get("dt_count"), market.get("zb_count"),
        market.get("seal_rate_pct")))
    opening = payload.get("yesterday_open") or {}
    print("昨日涨停今日开盘：高开 {} / 平开 {} / 低开 {} / 平均 {}%".format(
        opening.get("up_open"), opening.get("flat_open"),
        opening.get("down_open"), opening.get("avg_gap_pct")))
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
