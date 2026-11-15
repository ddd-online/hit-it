#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回看某个历史快照的结构判定与之后 N 个交易日表现。"""

import argparse
import collections
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backtest_leader_hit as backtest  # noqa: E402
import market_data  # noqa: E402
import prepare_leader_candidates as leader  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

STRICT_SIGNAL = {"启动初段", "待启动", "洗盘中"}
POOL_SIGNAL = STRICT_SIGNAL | {"建仓完成"}


def date_arg(value):
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("日期格式应为 YYYYMMDD")


def pct(now, before):
    if now is None or not before:
        return None
    return (now / before - 1.0) * 100.0


def future_stats(stock, target_date, horizon):
    bars = stock["bars"]
    try:
        index = next(
            index for index, bar in enumerate(bars)
            if bar.get("date") == target_date
        )
    except StopIteration:
        return None
    future = bars[index + 1:index + 1 + horizon]
    if len(future) < horizon:
        return None
    base = bars[index].get("close")
    closes = [bar.get("close") for bar in future if bar.get("close") is not None]
    if base is None or not closes:
        return None
    peak = max(closes)
    final = closes[-1]
    running_peak = base
    max_drawdown = 0.0
    for close in closes:
        running_peak = max(running_peak, close)
        max_drawdown = min(
            max_drawdown, (close / running_peak - 1.0) * 100.0)
    limit_ups = 0
    previous = base
    for bar in future:
        close = bar.get("close")
        if close is not None:
            if pct(close, previous) is not None and pct(close, previous) >= 9.4:
                limit_ups += 1
            previous = close
    return {
        "future_max_return_pct": round(pct(peak, base), 2),
        "future_final_return_pct": round(pct(final, base), 2),
        "future_max_drawdown_pct": round(max_drawdown, 2),
        "future_limit_up_count": limit_ups,
        "future_peak_date": future[closes.index(peak)].get("date"),
    }


def review_rows(stocks, target_date, price_max, horizon,
                min_rise, min_final, min_limit_ups, max_drawdown):
    rows = []
    for stock in stocks:
        target_bars = market_data.filter_bars(stock["bars"], target_date)
        if not target_bars or target_bars[-1].get("date") != target_date:
            continue
        price = target_bars[-1].get("close")
        if price is None or price >= price_max:
            continue
        outcome = future_stats(stock, target_date, horizon)
        if outcome is None:
            continue
        classification = backtest.classify_at(stock, target_date, price_max)
        features = classification.get("features") or {}
        target_bar = target_bars[-1]
        previous_close = (
            target_bars[-2].get("close") if len(target_bars) > 1 else None
        )
        day_return = pct(target_bar.get("close"), previous_close)
        day_range = (
            target_bar.get("high") - target_bar.get("low")
            if target_bar.get("high") is not None
            and target_bar.get("low") is not None else None
        )
        close_location = (
            (target_bar.get("close") - target_bar.get("low")) / day_range
            if day_range and target_bar.get("close") is not None else None
        )
        box = features.get("box30") or {}
        breakout_level = box.get("breakout_level")
        status = classification["status"]
        main_rise = (
            outcome["future_max_return_pct"] >= min_rise
            and outcome["future_final_return_pct"] >= min_final
            and outcome["future_limit_up_count"] >= min_limit_ups
            and outcome["future_max_drawdown_pct"] >= -max_drawdown
        )
        rows.append({
            "code": stock["code"],
            "name": stock["name"],
            "price": round(price, 3),
            "status": status,
            "technical_score": classification["technical_score"],
            "day_return_pct": round(day_return, 2)
            if day_return is not None else None,
            "close_location_pct": round(close_location * 100.0, 2)
            if close_location is not None else None,
            "breakout_margin_pct": round(
                pct(price, breakout_level), 2)
            if breakout_level else None,
            "strict_signal": status in STRICT_SIGNAL,
            "pool_signal": status in POOL_SIGNAL,
            "main_rise": main_rise,
            "exclusions": classification.get("exclusions") or [],
            "checks": classification.get("checks") or {},
            "range_position_pct": features.get("range_position_pct"),
            "return20_pct": features.get("return20_pct"),
            "return60_pct": features.get("return60_pct"),
            "concentration70_pct": (
                features.get("chips") or {}).get("concentration70_pct"),
            "profit_ratio_pct": features.get("profit_ratio_pct"),
            "overhead_to_cost90_pct": features.get(
                "overhead_to_cost90_pct"),
            "volume5_to_prior20": (
                features.get("volume") or {}).get("ratio5_to_prior20"),
            "ma_spread_pct": features.get("ma_spread_pct"),
            "box_range_pct": box.get("range_pct"),
            "prior_limit_count_5_60": features.get(
                "prior_limit_count_5_60"),
            **outcome,
        })
    rows.sort(key=lambda row: (
        not row["main_rise"],
        not row["pool_signal"],
        -row["future_max_return_pct"],
        row["code"],
    ))
    return rows


def count_reasons(rows):
    return collections.Counter(
        reason for row in rows for reason in row.get("exclusions") or []
    )


def count_failed_checks(rows):
    result = collections.Counter()
    for row in rows:
        for key, failed in (row.get("checks") or {}).items():
            if failed is False and key in {
                "chips_good", "shape_stable", "volume_dry",
                "breakout", "high_position", "shrinking_decline",
            }:
                result[key] += 1
    return result


def summarize(rows):
    total = len(rows)
    main_rows = [row for row in rows if row["main_rise"]]
    strict_false_negative = [
        row for row in main_rows if not row["strict_signal"]
    ]
    pool_false_negative = [
        row for row in main_rows if not row["pool_signal"]
    ]
    strict_false_positive = [
        row for row in rows if row["strict_signal"] and not row["main_rise"]
    ]
    pool_false_positive = [
        row for row in rows if row["pool_signal"] and not row["main_rise"]
    ]
    return {
        "total": total,
        "main_rise_count": len(main_rows),
        "status_counts": dict(collections.Counter(
            row["status"] for row in rows).most_common()),
        "strict_signal_count": sum(row["strict_signal"] for row in rows),
        "strict_true_positive": len(main_rows) - len(strict_false_negative),
        "strict_false_positive": len(strict_false_positive),
        "strict_false_negative": len(strict_false_negative),
        "pool_signal_count": sum(row["pool_signal"] for row in rows),
        "pool_true_positive": len(main_rows) - len(pool_false_negative),
        "pool_false_positive": len(pool_false_positive),
        "pool_false_negative": len(pool_false_negative),
        "strict_recall_pct": round(
            (len(main_rows) - len(strict_false_negative)) * 100.0
            / len(main_rows), 2) if main_rows else None,
        "strict_precision_pct": round(
            (len(main_rows) - len(strict_false_negative)) * 100.0
            / sum(row["strict_signal"] for row in rows), 2)
        if any(row["strict_signal"] for row in rows) else None,
        "pool_recall_pct": round(
            (len(main_rows) - len(pool_false_negative)) * 100.0
            / len(main_rows), 2) if main_rows else None,
        "pool_precision_pct": round(
            (len(main_rows) - len(pool_false_negative)) * 100.0
            / sum(row["pool_signal"] for row in rows), 2)
        if any(row["pool_signal"] for row in rows) else None,
        "main_future_max_median_pct": round(statistics.median(
            row["future_max_return_pct"] for row in rows), 2),
        "miss_reasons": count_reasons(pool_false_negative).most_common(),
        "miss_failed_checks": count_failed_checks(
            pool_false_negative).most_common(),
    }


def display_top(rows, limit=5):
    status_order = {
        "启动初段": 0,
        "待启动": 1,
        "洗盘中": 2,
        "建仓完成": 3,
        "只观察": 4,
        "排除": 5,
    }
    ranked = sorted(rows, key=lambda row: (
        status_order.get(row["status"], 9),
        -(row.get("technical_score") or 0),
        row["code"],
    ))
    return ranked[:limit]


def fmt(value, suffix=""):
    return "-" if value is None else "{:.2f}{}".format(value, suffix)


def render_report(payload):
    summary = payload["summary"]
    lines = [
        "# leader-hit 全样本回看 · {}".format(payload["date"]),
        "",
        "生成时间：{}".format(payload["generated_at"]),
        "",
        "口径：{} 元以下沪深主板；回看之后 {} 个交易日；主升定义为最大涨幅"
        "至少 {}%、期末涨幅至少 {}%、至少 {} 次涨停、最大回撤不超过 {}%。".format(
            payload["price_max"], payload["horizon"],
            payload["min_rise_pct"], payload["min_final_pct"],
            payload["min_limit_ups"], payload["max_drawdown_pct"]),
        "",
        "> 本报告使用未来走势做事后诊断，只用于反馈优化，不能作为当日可见信息。",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        "| 回看股票数 | {} |".format(summary["total"]),
        "| 事后主升股票 | {} |".format(summary["main_rise_count"]),
        "| 严格结构信号 | {} |".format(summary["strict_signal_count"]),
        "| 严格信号真阳性 | {} |".format(summary["strict_true_positive"]),
        "| 严格信号假阳性 | {} |".format(summary["strict_false_positive"]),
        "| 严格信号漏选 | {} |".format(summary["strict_false_negative"]),
        "| 严格信号召回率 | {}% |".format(summary["strict_recall_pct"]),
        "| 严格信号精确率 | {}% |".format(summary["strict_precision_pct"]),
        "| 股票池信号召回率 | {}% |".format(summary["pool_recall_pct"]),
        "| 股票池信号精确率 | {}% |".format(summary["pool_precision_pct"]),
        "",
        "严格信号 = 启动初段 / 待启动 / 洗盘中；股票池信号额外包含建仓完成。",
        "",
        "## 状态分布",
        "",
        "| 结构状态 | 数量 |",
        "|---|---:|",
    ]
    lines.extend(
        "| {} | {} |".format(status, count)
        for status, count in summary["status_counts"].items()
    )
    lines.extend([
        "",
        "## 漏选主升票",
        "",
        "| 代码 | 名称 | 价格 | 结构状态 | 技术分 | 20日最大% | 20日收盘% | "
        "最大回撤% | 涨停 | 主要漏选原因 |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---|",
    ])
    misses = [
        row for row in payload["rows"]
        if row["main_rise"] and not row["pool_signal"]
    ]
    if not misses:
        lines.append("| - | - | - | - | - | - | - | - | - | 无 |")
    for row in misses:
        reasons = "；".join(row.get("exclusions") or []) or "仅观察条件未组合"
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row["code"], row["name"], fmt(row["price"]), row["status"],
                row["technical_score"],
                fmt(row["future_max_return_pct"]),
                fmt(row["future_final_return_pct"]),
                fmt(row["future_max_drawdown_pct"]),
                row["future_limit_up_count"], reasons,
            ))
    lines.extend([
        "",
        "## 漏选原因统计",
        "",
        "| 原因 | 次数 |",
        "|---|---:|",
    ])
    if summary["miss_reasons"]:
        lines.extend(
            "| {} | {} |".format(reason, count)
            for reason, count in summary["miss_reasons"]
        )
    else:
        lines.append("| 无 | 0 |")
    lines.extend([
        "",
        "## 结构展示前 5",
        "",
        "| 代码 | 名称 | 价格 | 状态 | 技术分 | 当日% | 突破幅度% | 收盘位置% |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ])
    for row in payload["display_top"]:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                row["code"], row["name"], fmt(row["price"]),
                row["status"], row["technical_score"],
                fmt(row["day_return_pct"]),
                fmt(row["breakout_margin_pct"]),
                fmt(row["close_location_pct"]),
            ))
    lines.extend([
        "",
        "## 严格信号命中",
        "",
        "| 代码 | 名称 | 结构状态 | 20日最大% | 20日收盘% | 最大回撤% | 涨停 |",
        "|---|---|---|---:|---:|---:|---:|",
    ])
    true_positives = [
        row for row in payload["rows"]
        if row["strict_signal"] and row["main_rise"]
    ]
    for row in true_positives:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} |".format(
                row["code"], row["name"], row["status"],
                fmt(row["future_max_return_pct"]),
                fmt(row["future_final_return_pct"]),
                fmt(row["future_max_drawdown_pct"]),
                row["future_limit_up_count"],
            ))
    lines.extend([
        "",
        "## 严格信号误报",
        "",
        "| 代码 | 名称 | 结构状态 | 20日最大% | 20日收盘% | 最大回撤% |",
        "|---|---|---|---:|---:|---:|",
    ])
    false_positives = [
        row for row in payload["rows"]
        if row["strict_signal"] and not row["main_rise"]
    ]
    for row in false_positives:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} |".format(
                row["code"], row["name"], row["status"],
                fmt(row["future_max_return_pct"]),
                fmt(row["future_final_return_pct"]),
                fmt(row["future_max_drawdown_pct"]),
            ))
    lines.extend([
        "",
        "不构成投资建议",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="leader-hit 全样本历史回看")
    parser.add_argument("--date", required=True, type=date_arg)
    parser.add_argument("--price-max", type=float, default=15.0)
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--min-rise-pct", type=float, default=30.0)
    parser.add_argument("--min-final-pct", type=float, default=15.0)
    parser.add_argument("--min-limit-ups", type=int, default=2)
    parser.add_argument("--max-drawdown-pct", type=float, default=18.0)
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--json-out")
    parser.add_argument("--report-dir", default="report/主升候选")
    parser.add_argument("--report-tag", default="")
    args = parser.parse_args()

    stocks, failed = backtest.load_universe(args.days, args.workers)
    rows = review_rows(
        stocks, args.date, args.price_max, args.horizon,
        args.min_rise_pct, args.min_final_pct,
        args.min_limit_ups, args.max_drawdown_pct,
    )
    payload = {
        "schema": "leader-hit.review.v1",
        "date": args.date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "price_max": args.price_max,
        "horizon": args.horizon,
        "min_rise_pct": args.min_rise_pct,
        "min_final_pct": args.min_final_pct,
        "min_limit_ups": args.min_limit_ups,
        "max_drawdown_pct": args.max_drawdown_pct,
        "universe_count": len(stocks) + len(failed),
        "loaded_count": len(stocks),
        "failed_count": len(failed),
        "summary": summarize(rows),
        "display_top": display_top(rows, 5),
        "rows": rows,
    }
    report_dir = Path.cwd() / args.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_{}".format(args.report_tag) if args.report_tag else ""
    report_path = report_dir / "{}_全样本回看{}.md".format(
        args.date, suffix)
    report_path.write_text(render_report(payload), encoding="utf-8")
    payload["report_path"] = str(report_path)
    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    summary = payload["summary"]
    print("回看 {} · {} 只 · 主升 {} 只".format(
        args.date, summary["total"], summary["main_rise_count"]))
    print("严格信号：真阳性 {} / 假阳性 {} / 漏选 {} / 精确率 {}% / 召回率 {}%".format(
        summary["strict_true_positive"], summary["strict_false_positive"],
        summary["strict_false_negative"], summary["strict_precision_pct"],
        summary["strict_recall_pct"]))
    print("报告：{}".format(report_path))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("已中断。")
    except Exception as exc:  # noqa: BLE001
        print("错误：{}".format(exc), file=sys.stderr)
        raise SystemExit(1)
