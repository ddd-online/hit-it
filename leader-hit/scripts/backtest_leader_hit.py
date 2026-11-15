#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader-hit 年度主升样本与时点回放回测。

正样本定义只使用启动日之后的数据；技能判定只使用启动日及之前的数据。
腾讯前复权日 K 不含真实换手率，本脚本按当前流通股本近似，结果标为降级。

    python backtest_leader_hit.py --start 20260101 --end 20260630
    python backtest_leader_hit.py --start 20260101 --end 20261113
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import market_data  # noqa: E402
import prepare_leader_candidates as leader  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PRE_HIT = {"洗盘中", "待启动"}
START_HIT = {"启动初段"}
POOL_HIT = {"建仓完成", "洗盘中", "待启动", "启动初段"}


def parse_date_arg(value):
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("日期格式应为 YYYYMMDD")


def pct(now, before):
    if now is None or not before:
        return None
    return (now / before - 1.0) * 100.0


def parse_tencent_bars(rows, float_shares):
    bars = []
    for row in rows:
        if len(row) < 6:
            continue
        volume = leader.to_float(row[5])
        bars.append({
            "date": str(row[0]),
            "open": leader.to_float(row[1]),
            "close": leader.to_float(row[2]),
            "high": leader.to_float(row[3]),
            "low": leader.to_float(row[4]),
            "volume_hands": volume,
            "amount_yuan": None,
            "turnover_pct": (
                round(volume * 10000.0 / float_shares, 4)
                if volume is not None and float_shares else None
            ),
        })
    return bars


def load_stock(row, days):
    price = leader.to_float(row.get("price"))
    float_cap_yi = leader.to_float(row.get("float_cap_yi"))
    float_shares = (
        float_cap_yi * 1e8 / price
        if price and float_cap_yi else None
    )
    rows = market_data.fetch_tencent_kline(row["code"], days)
    bars = parse_tencent_bars(rows, float_shares)
    if not bars:
        raise RuntimeError("日K未获取")
    return {
        "code": row["code"],
        "name": row.get("name") or row["code"],
        "bars": bars,
        "metric_quality": "degraded" if float_shares else "unavailable",
        "limitations": (
            ["腾讯前复权日K；换手率按当前流通股本近似，筹码为推演值"]
            if float_shares else ["换手率未获取，筹码不可用"]
        ),
    }


def load_universe(days, workers, scan_limit=0):
    rows = leader.fetch_mainboard_universe(1e12)
    if scan_limit > 0:
        rows = rows[:scan_limit]
    stocks = []
    failed = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 24))) as executor:
        futures = {
            executor.submit(load_stock, row, days): row
            for row in rows
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                stocks.append(future.result())
            except Exception as exc:  # noqa: BLE001 单票失败进入缺失统计
                failed.append({
                    "code": row["code"],
                    "name": row.get("name"),
                    "error": str(exc),
                })
    stocks.sort(key=lambda item: item["code"])
    return stocks, failed


def intraday_launch(bars, index):
    if index < 1:
        return False
    day_return = pct(bars[index]["close"], bars[index - 1]["close"])
    if day_return is None:
        return False
    if day_return >= 9.4:
        return True
    if index < 3:
        return False
    three_day = pct(bars[index]["close"], bars[index - 3]["close"])
    prior = [
        bar["volume_hands"] for bar in bars[index - 20:index]
        if bar.get("volume_hands")
    ]
    avg_prior = statistics.mean(prior) if prior else None
    volume_ratio = (
        bars[index]["volume_hands"] / avg_prior
        if bars[index].get("volume_hands") and avg_prior else None
    )
    return (
        day_return >= 5.0
        and three_day is not None and three_day >= 12.0
        and volume_ratio is not None and volume_ratio >= 1.4
    )


def rally_window(bars, start_index, horizon, min_rise, min_final,
                 min_limit_ups, max_drawdown_pct):
    before = start_index - 1
    if before < 0:
        return None
    future = bars[start_index:start_index + horizon]
    if len(future) < horizon:
        return None
    base = bars[before].get("close")
    max_close = max(bar.get("close") or 0 for bar in future)
    final_close = future[-1].get("close")
    max_return = pct(max_close, base)
    final_return = pct(final_close, base)
    if (
        max_return is None or max_return < min_rise
        or final_return is None or final_return < min_final
    ):
        return None
    limit_ups = sum(
        1 for index in range(start_index, start_index + horizon)
        if (leader.closing_return(bars, index, index - 1) or 0) >= 9.4
    )
    if limit_ups < min_limit_ups:
        return None
    running_peak = base
    max_drawdown = 0.0
    for bar in future:
        close = bar.get("close")
        if close is None:
            continue
        running_peak = max(running_peak, close)
        if running_peak:
            max_drawdown = min(
                max_drawdown, (close / running_peak - 1.0) * 100.0)
    if max_drawdown < -max_drawdown_pct:
        return None
    return {
        "max_return_pct": round(max_return, 2),
        "final_return_pct": round(final_return, 2),
        "limit_up_count": limit_ups,
        "max_close_drawdown_pct": round(max_drawdown, 2),
    }


def detect_cases(stock, start, end, horizon, min_rise, min_final,
                 min_limit_ups, max_drawdown_pct, price_max):
    bars = stock["bars"]
    last_case_index = -1
    cases = []
    for index in range(1, len(bars) - horizon + 1):
        date = bars[index].get("date")
        if date is None or date < start or date > end:
            continue
        if index <= last_case_index:
            continue
        decision_bar = bars[index - 1]
        price = decision_bar.get("close")
        if price is None or price >= price_max:
            continue
        if not intraday_launch(bars, index):
            continue
        window = rally_window(
            bars, index, horizon, min_rise, min_final, min_limit_ups,
            max_drawdown_pct)
        if not window:
            continue
        cases.append({
            "code": stock["code"],
            "name": stock["name"],
            "prestart_date": decision_bar["date"],
            "start_date": date,
            "prestart_price": price,
            **window,
        })
        last_case_index = index + horizon
    return cases


def classify_at(stock, target_date, price_max):
    bars = market_data.filter_bars(stock["bars"], target_date)
    if not bars:
        raise RuntimeError("目标日前无日K")
    bar = bars[-1]
    if bar.get("close") is None or bar["close"] >= price_max:
        return {
            "status": "排除",
            "technical_score": 0,
            "checks": {},
            "exclusions": ["价格不在用户限定范围内"],
            "features": {},
            "metric_quality": stock["metric_quality"],
        }
    quote = leader.historical_quote(stock["code"], stock["name"], bar)
    features = leader.build_features(quote, bars, target_date)
    status, score, checks, exclusions = leader.classify(features)
    return {
        "status": status,
        "technical_score": score,
        "checks": checks,
        "exclusions": exclusions,
        "features": features,
        "metric_quality": stock["metric_quality"],
    }


def miss_reasons(pre, start):
    reasons = list(pre.get("exclusions") or [])
    if not reasons:
        checks = pre.get("checks") or {}
        for key, text in (
            ("chips_good", "筹码结构未通过"),
            ("shape_stable", "箱体或均线未收敛"),
            ("volume_dry", "缩量条件未满足"),
            ("breakout", "启动日未形成放量突破"),
        ):
            if checks.get(key) is False:
                reasons.append(text)
    if start.get("status") not in START_HIT:
        if start.get("exclusions"):
            reasons.extend(start["exclusions"])
        checks = start.get("checks") or {}
        if not checks.get("breakout"):
            reasons.append("启动确认不足")
    return list(dict.fromkeys(reasons)) or ["结构条件未组合成重点状态"]


def classify_checkpoints(stock, start_date, offsets, price_max):
    dates = [bar.get("date") for bar in stock["bars"]]
    try:
        start_index = dates.index(start_date)
    except ValueError:
        return []
    checkpoints = []
    for offset in offsets:
        index = start_index - offset
        if index < 0:
            continue
        result = classify_at(stock, dates[index], price_max)
        checkpoints.append({
            "offset": offset,
            "date": dates[index],
            **result,
        })
    return checkpoints


def evaluate_cases(cases, by_code, price_max, offsets):
    rows = []
    for case in cases:
        stock = by_code[case["code"]]
        try:
            checkpoints = classify_checkpoints(
                stock, case["start_date"], offsets, price_max)
            pre = (
                next((item for item in checkpoints if item["offset"] == 1), None)
                or (checkpoints[-1] if checkpoints else {
                    "status": "数据未获取", "technical_score": 0,
                    "checks": {}, "exclusions": ["启动前检查点不足"],
                })
            )
            start = classify_at(stock, case["start_date"], price_max)
            pre_hits = [
                item for item in checkpoints if item["status"] in PRE_HIT
            ]
            pre_hit = bool(pre_hits)
            start_hit = start["status"] in START_HIT
            pool_hit = (
                any(item["status"] in POOL_HIT for item in checkpoints)
                or start_hit
            )
            hit_item = pre_hits[0] if pre_hits else None
            rows.append({
                **case,
                "prestart_status": pre["status"],
                "prestart_score": pre["technical_score"],
                "start_status": start["status"],
                "start_score": start["technical_score"],
                "prestart_hit": pre_hit,
                "start_hit": start_hit,
                "hit": pre_hit or start_hit,
                "hit_date": hit_item["date"] if hit_item
                else case["start_date"] if start_hit else None,
                "hit_status": hit_item["status"] if hit_item
                else start["status"] if start_hit else None,
                "pool_hit": pool_hit,
                "miss_reasons": [] if pre_hit or start_hit
                else miss_reasons(pre, start),
                "checkpoints": checkpoints,
                "metric_quality": stock["metric_quality"],
                "prestart_checks": pre.get("checks") or {},
                "start_checks": start.get("checks") or {},
                "prestart_exclusions": pre.get("exclusions") or [],
                "start_exclusions": start.get("exclusions") or [],
            })
        except Exception as exc:  # noqa: BLE001 单例失败保留原因
            rows.append({
                **case,
                "prestart_status": "数据未获取",
                "start_status": "数据未获取",
                "hit": False,
                "pool_hit": False,
                "miss_reasons": ["回放失败：{}".format(exc)],
                "metric_quality": stock["metric_quality"],
            })
    return rows


def summarize(rows):
    total = len(rows)
    hit = sum(bool(row.get("hit")) for row in rows)
    start_hit = sum(bool(row.get("start_hit")) for row in rows)
    pre_hit = sum(bool(row.get("prestart_hit")) for row in rows)
    pool_hit = sum(bool(row.get("pool_hit")) for row in rows)
    left_evidence_rows = [
        row for row in rows
        if not any(
            any("左侧涨停" in reason for reason in item.get("exclusions") or [])
            for item in row.get("checkpoints") or []
        )
        and not any(
            "左侧涨停" in reason
            for reason in row.get("start_exclusions") or []
        )
    ]
    left_evidence_hit = sum(
        bool(row.get("hit")) for row in left_evidence_rows)
    reasons = Counter(
        reason for row in rows for reason in row.get("miss_reasons") or []
    )
    pre_statuses = Counter(row.get("prestart_status") for row in rows)
    start_statuses = Counter(row.get("start_status") for row in rows)
    missed_checks = Counter()
    for row in rows:
        if row.get("hit"):
            continue
        for key in (
            "chips_good", "shape_stable", "volume_dry",
            "breakout", "high_position",
        ):
            checks = row.get("prestart_checks") or {}
            if checks.get(key) is False:
                missed_checks[key] += 1
    return {
        "case_count": total,
        "strict_hit_count": hit,
        "strict_hit_rate_pct": round(hit * 100.0 / total, 2) if total else None,
        "prestart_hit_count": pre_hit,
        "start_hit_count": start_hit,
        "pool_hit_count": pool_hit,
        "pool_hit_rate_pct": round(pool_hit * 100.0 / total, 2) if total else None,
        "left_evidence_case_count": len(left_evidence_rows),
        "left_evidence_hit_count": left_evidence_hit,
        "left_evidence_hit_rate_pct": (
            round(left_evidence_hit * 100.0 / len(left_evidence_rows), 2)
            if left_evidence_rows else None
        ),
        "miss_reasons": reasons.most_common(),
        "prestart_statuses": dict(pre_statuses.most_common()),
        "start_statuses": dict(start_statuses.most_common()),
        "missed_checks": dict(missed_checks.most_common()),
    }


def render_report(payload):
    lines = [
        "# leader-hit 主升样本回测 · {} 至 {}".format(
            payload["start"], payload["end"]),
        "",
        "> 本报告是结构代理回测，不是完整 SKILL 实盘胜率。历史题材、公告、"
        "板块扩散和真实换手率未完整复现；筹码为降级推演值。",
        "",
        "数据截至：{}".format(payload["data_date"] or "-"),
        "",
        "执行时间：{}".format(payload["generated_at"]),
        "",
        "口径：沪深主板、启动前收盘价低于 {} 元；启动后 {} 个交易日内"
        "收盘最大涨幅至少 {}%、期末涨幅至少 {}%、至少 {} 次涨停，"
        "启动后收盘最大回撤不超过 {}%。".format(
            payload["price_max"], payload["horizon"],
            payload["min_rise_pct"], payload["min_final_pct"],
            payload["min_limit_ups"], payload["max_drawdown_pct"]),
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        "| 股票池 | {} |".format(payload["universe_count"]),
        "| 成功取数 | {} |".format(payload["stock_count"]),
        "| 取数失败 | {} |".format(payload["failed_count"]),
        "| 主升正样本 | {} |".format(payload["summary"]["case_count"]),
        "| 严格命中 | {}（{}%） |".format(
            payload["summary"]["strict_hit_count"],
            payload["summary"]["strict_hit_rate_pct"]),
        "| 启动前结构命中 | {} |".format(
            payload["summary"]["prestart_hit_count"]),
        "| 启动日确认命中 | {} |".format(
            payload["summary"]["start_hit_count"]),
        "| 股票池命中 | {}（{}%） |".format(
            payload["summary"]["pool_hit_count"],
            payload["summary"]["pool_hit_rate_pct"]),
        "| 有左侧涨停证据样本命中 | {} / {}（{}%） |".format(
            payload["summary"]["left_evidence_hit_count"],
            payload["summary"]["left_evidence_case_count"],
            payload["summary"]["left_evidence_hit_rate_pct"]),
        "",
        "严格命中 = 启动前为“洗盘中/待启动”，或启动日为“启动初段”。"
        "启动前检查点按 T-{} 日回放；股票池命中额外包含“建仓完成”。".format(
            "/T-".join(str(value) for value in payload["prestart_offsets"])),
        "",
        "| 阶段状态 | 数量 |",
        "|---|---:|",
    ]
    for status, count in payload["summary"]["prestart_statuses"].items():
        lines.append("| 启动前 · {} | {} |".format(status, count))
    for status, count in payload["summary"]["start_statuses"].items():
        lines.append("| 启动日 · {} | {} |".format(status, count))
    lines.extend([
        "",
        "## 漏判原因",
        "",
        "| 原因 | 次数 |",
        "|---|---:|",
    ])
    if payload["summary"]["miss_reasons"]:
        lines.extend(
            "| {} | {} |".format(reason, count)
            for reason, count in payload["summary"]["miss_reasons"]
        )
    else:
        lines.append("| 无 | 0 |")
    lines.extend([
        "",
        "## 样本明细",
        "",
        "| 代码 | 名称 | 启动前 | 启动日 | 前一日状态 | 启动日状态 | "
        "20日最大% | 20日收盘% | 涨停 | 结论 |",
        "|---|---|---|---|---|---|---:|---:|---:|---|",
    ])
    for row in payload["cases"]:
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {:.2f} | {:.2f} | {} | {} |".format(
                row["code"], row["name"], row["prestart_date"],
                row["start_date"], row["prestart_status"], row["start_status"],
                row["max_return_pct"], row["final_return_pct"],
                row["limit_up_count"],
                "命中" if row.get("hit") else "未命中",
            ))
    lines.extend([
        "",
        "## 数据限制",
        "",
        "- 股票池来自回测执行时的沪深主板名单，存在退市与改名幸存者偏差；ST 仅按当前名称过滤。",
        "- 腾讯前复权日 K 不含真实逐日换手率，筹码按当前流通股本近似，只能作辅助证据。",
        "- 正样本用未来数据定义，技能状态只用目标日及以前数据，二者未混用。",
        "- 历史题材、公告、减持与板块扩散无法完整回溯，未把消息缺失算作技能命中。",
        "- 本回测统计的是主升正样本召回率，不是全市场候选精确率；不能据此推导收益率。",
        "",
        "不构成投资建议",
    ])
    return "\n".join(lines)


def run(args):
    start = parse_date_arg(args.start)
    end = parse_date_arg(args.end)
    if start > end:
        raise RuntimeError("--start 不能晚于 --end")
    stocks, failed = load_universe(args.days, args.workers, args.scan_limit)
    cases = []
    for stock in stocks:
        cases.extend(detect_cases(
            stock, start, end, args.horizon, args.min_rise_pct,
            args.min_final_pct, args.min_limit_ups,
            args.max_drawdown_pct, args.price_max))
    cases.sort(key=lambda row: (row["start_date"], row["code"]))
    by_code = {stock["code"]: stock for stock in stocks}
    offsets = [
        int(value) for value in args.prestart_offsets.split(",")
        if value.strip()
    ]
    offsets = sorted(set(offsets), reverse=True)
    if not offsets or any(value <= 0 or value >= args.horizon for value in offsets):
        raise RuntimeError("--prestart-offsets 必须为正整数且小于 horizon")
    rows = evaluate_cases(cases, by_code, args.price_max, offsets)
    dates = [
        stock["bars"][-1]["date"] for stock in stocks
        if stock.get("bars")
    ]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "schema": "leader-hit.backtest.v1",
        "start": start,
        "end": end,
        "data_date": max(dates) if dates else None,
        "generated_at": generated_at,
        "price_max": args.price_max,
        "horizon": args.horizon,
        "min_rise_pct": args.min_rise_pct,
        "min_final_pct": args.min_final_pct,
        "min_limit_ups": args.min_limit_ups,
        "max_drawdown_pct": args.max_drawdown_pct,
        "prestart_offsets": offsets,
        "universe_count": len(stocks) + len(failed),
        "stock_count": len(stocks),
        "failed_count": len(failed),
        "failed": failed[:30],
        "summary": summarize(rows),
        "cases": rows,
        "note": "结构代理回测；历史消息与真实换手率未完整复现。",
    }
    return payload


def main():
    parser = argparse.ArgumentParser(description="leader-hit 主升样本回测")
    parser.add_argument("--start", required=True, help="样本启动日开始 YYYYMMDD")
    parser.add_argument("--end", required=True, help="样本启动日结束 YYYYMMDD")
    parser.add_argument("--price-max", type=float, default=20.0)
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--min-rise-pct", type=float, default=30.0)
    parser.add_argument("--min-final-pct", type=float, default=15.0)
    parser.add_argument("--min-limit-ups", type=int, default=2)
    parser.add_argument("--max-drawdown-pct", type=float, default=18.0,
                        help="启动后按收盘价计算的最大回撤上限，默认 18")
    parser.add_argument("--prestart-offsets", default="5,3,1",
                        help="启动前回放交易日偏移，默认 5,3,1")
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--scan-limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-out", help="同时写入 JSON 结果")
    parser.add_argument("--report-tag", default="", help="报告文件名附加标签")
    parser.add_argument("--report-dir", default="report/回测")
    args = parser.parse_args()
    if args.scan_limit < 0:
        parser.error("--scan-limit 不能小于 0")
    payload = run(args)
    report_dir = Path.cwd() / args.report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_{}".format(args.report_tag) if args.report_tag else ""
    report_path = report_dir / "低位主升_{}_{}{}.md".format(
        args.start, args.end, suffix)
    report_path.write_text(render_report(payload), encoding="utf-8")
    payload["report_path"] = str(report_path)
    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["summary"]
        print("leader-hit 回测 {} 至 {}".format(payload["start"], payload["end"]))
        print("样本 {} · 严格命中 {}（{}%）· 股票池命中 {}（{}%）".format(
            summary["case_count"], summary["strict_hit_count"],
            summary["strict_hit_rate_pct"], summary["pool_hit_count"],
            summary["pool_hit_rate_pct"]))
        print("报告：{}".format(report_path))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("已中断。")
    except Exception as exc:  # noqa: BLE001 统一中文报错
        print("错误：{}".format(exc), file=sys.stderr)
        raise SystemExit(1)
