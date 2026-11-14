#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收盘后保存涨停池，供下一交易日 limit-hit 复用。

默认保存最近已完成交易日的实时涨停池；指定 --date YYYYMMDD 时保存该日涨停池。
输出固定写入当前目录的 report/涨停池/YYYY-MM-DD.json，已有文件不覆盖。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import http_util  # noqa: E402
import prepare_candidates as pool_store  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

CN_TZ = timezone(timedelta(hours=8))


def iso_date(compact):
    return "{}-{}-{}".format(compact[:4], compact[4:6], compact[6:])


def fetch_current_pool():
    current = pool_store.run_json("fetch_limit_up.py", "--json")
    if not current.get("data_available") or not current.get("zt_rows"):
        raise RuntimeError("{} 未取到涨停池数据".format(current.get("base_date") or "-"))
    data_date = iso_date(current["base_date"])
    snapshot = datetime.now(CN_TZ)
    if not pool_store.market_closed(
            snapshot, data_date, current.get("quote_date")):
        raise RuntimeError("当日行情尚未收盘，不能保存涨停池")
    return {
        "data_date": data_date,
        "quote_date": current.get("quote_date"),
        "rows": current.get("zt_rows") or [],
        "market": current.get("overview") or {},
        "one_word": current.get("one_word") or {},
        "source": "eastmoney-push2ex",
        "source_name": "东方财富 push2ex 涨停池",
        "limitations": [
            "涨停池不含 ST 与科创板",
            "题材来自生成时 F10 精确概念，后续可能变动",
        ],
    }


def fetch_dated_pool(date_text):
    try:
        current = pool_store.run_json(
            "fetch_limit_up.py", "--date", date_text, "--json")
    except RuntimeError:
        current = None
    if (current and current.get("base_date") == date_text
            and current.get("data_available") and current.get("zt_rows")):
        return {
            "data_date": iso_date(date_text),
            "quote_date": current.get("quote_date"),
            "rows": current.get("zt_rows") or [],
            "market": current.get("overview") or {},
            "one_word": (
                current.get("one_word") or {}
                if current.get("quote_date") == date_text else {}
            ),
            "source": "eastmoney-push2ex",
            "source_name": "东方财富 push2ex 涨停池",
            "limitations": [
                "涨停池不含 ST 与科创板",
                "题材来自生成时 F10 精确概念，后续可能变动",
            ],
        }

    history = pool_store.run_json(
        "fetch_limit_up_history.py", "--date", date_text, "--json")
    rows = history.get("rows") or []
    if not rows:
        raise RuntimeError("{} 未获取到涨停池数据".format(date_text))
    return {
        "data_date": iso_date(date_text),
        "quote_date": date_text,
        "rows": rows,
        "market": {
            "zt_count": len(rows),
            "dt_count": None,
            "zb_count": None,
            "seal_rate_pct": None,
        },
        "one_word": {},
        "source": "10jqka-history",
        "source_name": history.get("source") or "同花顺公开历史涨停池",
        "limitations": (history.get("limitations") or []) + [
            "历史池只保存涨停成功池，不包含炸板池和跌停池",
            "同花顺窗口板数仅在窗口天数等于涨停次数时记为当前连板",
        ],
    }


def build_payload(use_current, date_text=None):
    context = fetch_current_pool() if use_current else fetch_dated_pool(date_text)
    rows = context["rows"]
    concepts = pool_store.hydrate_concepts(
        sorted({row.get("code") for row in rows if row.get("code")})
    )
    for row in rows:
        row["themes"] = pool_store.themes_for(row.get("code"), concepts)
        if context["one_word"].get(row.get("code")):
            row["one_word"] = True
    payload = pool_store.build_limit_pool_payload(
        data_date=context["data_date"],
        rows=rows,
        generated_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
        source=context["source"],
        source_name=context["source_name"],
        quote_date=context["quote_date"],
        market=context["market"],
        limitations=context["limitations"],
    )
    path = pool_store.build_limit_pool_path(context["data_date"])
    created = pool_store.write_limit_pool(path, payload)
    if not created:
        payload = pool_store.load_limit_pool(path, context["data_date"])
    return path, payload, created


def print_text(path, payload, created):
    summary = payload.get("summary") or {}
    print("涨停池{}：{}".format("已保存" if created else "已存在", path))
    print("数据日期：{}".format(payload.get("data_date")))
    print("来源：{}".format(payload.get("source_name")))
    print("涨停家数：{}；最高板：{}".format(
        summary.get("count"), summary.get("max_board") or "-"))


def main():
    ap = argparse.ArgumentParser(description="收盘后保存涨停池")
    ap.add_argument("--date", help="指定数据日期 YYYYMMDD")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    if args.date:
        try:
            datetime.strptime(args.date, "%Y%m%d")
        except ValueError:
            ap.error("--date 格式应为 YYYYMMDD")

    path, payload, created = build_payload(
        use_current=not args.date, date_text=args.date)
    if args.json:
        print(json.dumps({
            "created": created,
            "path": str(path),
            "data_date": payload.get("data_date"),
            "count": (payload.get("summary") or {}).get("count"),
            "source": payload.get("source"),
        }, ensure_ascii=False, indent=2))
    else:
        print_text(path, payload, created)


if __name__ == "__main__":
    http_util.run(main)
