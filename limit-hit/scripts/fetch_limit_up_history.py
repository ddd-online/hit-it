#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股历史涨停池报告（同花顺公开接口）。

用法:
    python fetch_limit_up_history.py --date YYYYMMDD [--json]

同花顺接口用于补东财涨停池约 30 个交易日覆盖之外的日期。它只返回最终
涨停成功池，不含炸板池、跌停池，也没有市场级封板率；不得据此伪造炸板率。
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fallback_sources  # noqa: E402
import http_util  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

API_URL = (
    "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    "?page={page}&limit={limit}&field=199112,10,9001,330323,330324,330325,"
    "9002,330329,133971,133970,1968584,3475914,9003,9004"
    "&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={date}"
)
REFERER = "https://data.10jqka.com.cn/"
_open = http_util.open_url
CN_TZ = timezone(timedelta(hours=8))


def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": REFERER,
    })
    with _open(req, timeout=25) as resp:
        return resp.read()


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fmt_time(epoch):
    value = to_int(epoch)
    if value is None:
        return None
    return datetime.fromtimestamp(value, CN_TZ).strftime("%H:%M:%S")


def decode_board(value):
    raw = to_int(value)
    if raw is None:
        return None, None
    # 实测编码为“窗口涨停次数 × 65536 + 窗口天数”，例如 11天6板。
    # 该高位不是当前连续板数，不能直接用于一进二/二进三判定。
    return raw & 0xFFFF, raw >> 16


def normalize_row(row):
    code = fallback_sources.infer_sec_code(row.get("code"))
    if not code:
        return None
    board_days, board_window_count = decode_board(row.get("high_days_value"))
    exact_board_count = (
        board_window_count
        if board_days and board_window_count
        and board_days == board_window_count
        else None
    )
    reason = str(row.get("reason_type") or "").strip()
    return {
        "code": code,
        "name": row.get("name"),
        "price": to_float(row.get("latest")),
        "change_pct": to_float(row.get("change_rate")),
        "turnover_pct": to_float(row.get("turnover_rate")),
        "market_type": row.get("market_type"),
        "limit_up_type": row.get("limit_up_type"),
        "board_days": board_days,
        "board_window_count": board_window_count,
        "board_count": exact_board_count,
        "board_window": row.get("high_days"),
        "open_count": to_int(row.get("open_num")),
        "first_seal": fmt_time(row.get("first_limit_up_time")),
        "last_seal": fmt_time(row.get("last_limit_up_time")),
        "seal_amount_yuan": to_float(row.get("order_amount")),
        "seal_volume": to_float(row.get("order_volume")),
        "market_cap_yuan": to_float(row.get("currency_value")),
        "reason_text": reason or None,
        "reason_tags": [tag.strip() for tag in reason.split("+") if tag.strip()],
        "change_tag": row.get("change_tag"),
    }


def fetch(date):
    rows = []
    total = None
    page = 1
    while page <= 20:
        url = API_URL.format(
            page=page,
            limit=200,
            date=urllib.parse.quote(date, safe=""),
        )
        data = json.loads(http_get(url).decode("utf-8", errors="strict"))
        if data.get("status_code") != 0:
            raise RuntimeError("同花顺接口返回 status_code={}".format(data.get("status_code")))
        page_info = (data.get("data") or {}).get("page") or {}
        total = to_int(page_info.get("total"))
        batch = (data.get("data") or {}).get("info") or []
        rows.extend(item for item in (normalize_row(row) for row in batch) if item)
        if not batch or (total is not None and len(rows) >= total):
            break
        page += 1
    return rows, total


def print_text(date, rows):
    print("=" * 105)
    print("A股历史涨停池 · {}（来源：同花顺公开接口）".format(date))
    print("=" * 105)
    if not rows:
        print("数据未获取")
        return
    print("{:<10}{:<10}{:>7}{:>7}{:>9}{:>12}{:>7}{:>12}  {}".format(
        "代码", "名称", "现价", "涨幅%", "换手%", "窗口统计", "开板", "封单(万元)", "题材原因"))
    print("-" * 105)
    for row in rows:
        print("{:<10}{:<10}{:>7.2f}{:>7.2f}{:>9.2f}{:>8}{:>9}{:>12.0f}  {}".format(
            row["code"],
            str(row.get("name") or "-")[:8],
            row.get("price") or 0,
            row.get("change_pct") or 0,
            row.get("turnover_pct") or 0,
            row.get("board_window") or "-",
            row.get("open_count") if row.get("open_count") is not None else "-",
            (row.get("seal_amount_yuan") or 0) / 1e4,
            row.get("reason_text") or "-",
        ))
    print("=" * 105)
    print("注：仅涨停成功池，不含炸板池和跌停池；窗口统计不等于当前连续板数。")


def main():
    ap = argparse.ArgumentParser(description="A股历史涨停池报告（同花顺公开接口）")
    ap.add_argument("--date", required=True, help="日期 YYYYMMDD")
    ap.add_argument("--json", action="store_true", help="输出JSON")
    args = ap.parse_args()
    if not re.match(r"^\d{8}$", args.date):
        ap.error("--date 格式应为 YYYYMMDD")

    rows, total = fetch(args.date)
    payload = {
        "date": args.date,
        "data_available": bool(rows),
        "rows": rows,
        "count": len(rows),
        "reported_total": total,
        "source": "同花顺公开历史涨停池",
        "limitations": [
            "仅涨停成功池，不含炸板池和跌停池",
            "历史结果不包含逐笔行情与分时",
            "high_days_value 表示窗口内涨停次数，仅天数等于板数时可确认当前连板",
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not rows:
            http_util.mark_json_emitted()
            sys.exit("错误：{} 未获取到历史涨停池数据".format(args.date))
        return
    print_text(args.date, rows)
    if not rows:
        sys.exit("错误：{} 未获取到历史涨停池数据".format(args.date))


if __name__ == "__main__":
    http_util.run(main)
