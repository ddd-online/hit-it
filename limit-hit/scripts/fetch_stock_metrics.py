#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量计算 A 股日K、量能、洗盘路径与筹码指标。

用法:
    python fetch_stock_metrics.py --codes sh600410,sz002491 [--date YYYYMMDD]

只输出数值与数据限制，不判断好坏、不生成买卖结论。
"""

import argparse
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_stock_structure  # noqa: E402
import http_util  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def pct_change(now, before):
    if now is None or not before:
        return None
    return round((now / before - 1.0) * 100.0, 2)


def moving_average(values, span):
    if len(values) < span:
        return None
    return round(sum(values[-span:]) / span, 3)


def build_metrics(code, cutoff, days):
    bars, source, limitations = fetch_stock_structure.fetch_kline(code, days, cutoff)
    bars = fetch_stock_structure.filter_bars(bars, cutoff)
    if not bars:
        raise RuntimeError("未获取到日K")
    day = bars[-1]
    previous = bars[-2] if len(bars) > 1 else None
    closes = [bar.get("close") for bar in bars if bar.get("close") is not None]
    prior_volumes = [
        bar.get("volume_hands") if bar.get("volume_hands") is not None else bar.get("volume")
        for bar in bars[-6:-1]
        if bar.get("volume_hands") is not None or bar.get("volume") is not None
    ]
    day_volume = (
        day.get("volume_hands")
        if day.get("volume_hands") is not None
        else day.get("volume")
    )
    avg_volume = statistics.mean(prior_volumes) if prior_volumes else None
    chips = fetch_stock_structure.compute_chip_distribution(bars[-120:], day.get("close"))
    one_word = all(day.get(key) == day.get("close")
                   for key in ("open", "high", "low")) and day.get("close") is not None
    return {
        "code": code,
        "data_date": day.get("date"),
        "last": {
            "open": day.get("open"),
            "high": day.get("high"),
            "low": day.get("low"),
            "close": day.get("close"),
            "volume": day_volume,
            "amount_yuan": day.get("amount_yuan"),
            "turnover_pct": day.get("turnover_pct"),
        },
        "returns_pct": {
            "5d": pct_change(day.get("close"), closes[-6] if len(closes) >= 6 else None),
            "10d": pct_change(day.get("close"), closes[-11] if len(closes) >= 11 else None),
            "20d": pct_change(day.get("close"), closes[-21] if len(closes) >= 21 else None),
        },
        "ma": {
            "5": moving_average(closes, 5),
            "10": moving_average(closes, 10),
            "20": moving_average(closes, 20),
        },
        "open_gap_pct": pct_change(day.get("open"), previous.get("close") if previous else None),
        "low_gap_pct": pct_change(day.get("low"), previous.get("close") if previous else None),
        "volume_ratio_5d": round(day_volume / avg_volume, 2)
        if day_volume and avg_volume else None,
        "one_word": one_word,
        "chips": chips,
        "source": source,
        "limitations": limitations,
    }


def parse_codes(raw_codes):
    codes = [
        code.strip().lower()
        for value in raw_codes
        for code in value.split(",")
        if code.strip()
    ]
    invalid = [code for code in codes if not fetch_stock_structure.CODE_RE.match(code)]
    if invalid:
        raise ValueError("无效代码：{}".format(", ".join(invalid)))
    return list(dict.fromkeys(codes))


def print_text(payload):
    print("=" * 112)
    print("批量个股指标 · 数据日期 {}".format(payload["data_date"] or "-"))
    print("=" * 112)
    print("{:<10}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>10}".format(
        "代码", "现价", "5日%", "10日%", "20日%", "MA20", "量比5", "开盘差%", "获利盘%"))
    print("-" * 112)
    for row in payload["rows"]:
        if row.get("error"):
            print("{:<10}  {}".format(row["code"], row["error"]))
            continue
        last = row["last"]
        returns = row["returns_pct"]
        chips = row.get("chips") or {}
        print("{:<10}{:>8.2f}{:>8}{:>8}{:>8}{:>8}{:>8}{:>8}{:>10}".format(
            row["code"],
            last.get("close") or 0,
            "-" if returns.get("5d") is None else "{:+.2f}".format(returns["5d"]),
            "-" if returns.get("10d") is None else "{:+.2f}".format(returns["10d"]),
            "-" if returns.get("20d") is None else "{:+.2f}".format(returns["20d"]),
            "-" if row["ma"].get("20") is None else "{:.2f}".format(row["ma"]["20"]),
            "-" if row.get("volume_ratio_5d") is None else "{:.2f}".format(row["volume_ratio_5d"]),
            "-" if row.get("open_gap_pct") is None else "{:+.2f}".format(row["open_gap_pct"]),
            "-" if chips.get("profit_ratio_pct") is None else "{:.2f}".format(chips["profit_ratio_pct"]),
        ))
    print("注：筹码为日K+换手率推演值；指标不包含买卖判断。")


def main():
    ap = argparse.ArgumentParser(description="批量 A 股日K、量能、洗盘与筹码指标")
    ap.add_argument("--codes", nargs="+", required=True, help="代码，支持逗号或空格分隔")
    ap.add_argument("--date", help="截止日期 YYYYMMDD；缺省取最新交易日")
    ap.add_argument("--days", type=int, default=120, help="计算筹码的日K根数，默认120")
    ap.add_argument("--workers", type=int, default=8, help="并发数，默认8")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.days < 20:
        ap.error("--days 至少为 20")

    cutoff = None
    if args.date:
        if len(args.date) != 8 or not args.date.isdigit():
            ap.error("--date 格式应为 YYYYMMDD")
        cutoff = "{}-{}-{}".format(args.date[:4], args.date[4:6], args.date[6:])
    codes = parse_codes(args.codes)
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as executor:
        futures = {
            executor.submit(build_metrics, code, cutoff, args.days): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001 单票失败保留 未获取
                rows.append({"code": code, "error": str(exc)})
    rows.sort(key=lambda row: codes.index(row["code"]))
    payload = {
        "requested_date": cutoff,
        "data_date": next(
            (row.get("data_date") for row in rows if row.get("data_date")), None),
        "rows": rows,
        "count": len(rows),
        "note": "只提供指标，不判断好坏；筹码为推演值。",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)


if __name__ == "__main__":
    http_util.run(main)
