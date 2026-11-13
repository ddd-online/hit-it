#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股 股东户数、筹码分布与题材归属报告。

用法:
    python fetch_stock_structure.py sh600410 [--date YYYYMMDD] [--days 120]
    python fetch_stock_structure.py sh600410 sz000001 --concepts-only [--json]

股东户数取东方财富 F10 披露值；筹码分布取东方财富日K的换手率，
按历史筹码衰减、当日筹码三角分布本地推演，不是券商终端原始筹码。
东财日K不可用时回退腾讯日K，并用当前流通股本近似换手率，报告会标注。
"""

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fallback_sources  # noqa: E402
import http_util  # noqa: E402 与脚本同目录的直连网络层

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

EASTMONEY_REFERER = "https://data.eastmoney.com/"
EASTMONEY_KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=1&lmt={days}&end={end}"
)
HOLDER_URL = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax"
    "?code={code}"
)
BOARD_FILTER_REPORT = "RPT_F10_CORETHEME_BOARDTYPE"
BOARD_COLUMNS = (
    "SECURITY_CODE,SECURITY_NAME_ABBR,BOARD_CODE,BOARD_NAME,"
    "NEW_BOARD_CODE,IS_PRECISE,BOARD_RANK"
)
BOARD_URL = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?reportName={report}&columns={columns}&pageSize=500&pageNumber={page}"
    "&filter={filter}"
)

CODE_RE = re.compile(r"^(sh|sz|bj)\d{6}$")

_open = http_util.open_url


def http_get(url, referer=EASTMONEY_REFERER, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
    })
    with _open(req, timeout=timeout) as resp:
        return resp.read()


def decode_json(raw):
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = raw.decode("gb18030", errors="replace")
    return json.loads(text)


def normalize_code(value):
    code = str(value).strip().lower()
    if not CODE_RE.match(code):
        raise ValueError("代码格式应为 sh/sz/bj + 6位数字：{}".format(value))
    return code


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_num(value, digits=2):
    if value is None:
        return "-"
    return "{:.{}f}".format(value, digits)


def fmt_int(value):
    if value is None:
        return "-"
    return format(int(value), ",")


def em_secid(code):
    digits = code[2:]
    return ("1." if code.startswith("sh") else "0.") + digits


def clean_date(value):
    text = str(value or "")
    return text[:10] if len(text) >= 10 else None


def parse_em_kline(lines):
    bars = []
    for line in lines:
        f = str(line).split(",")
        if len(f) < 11:
            continue
        bars.append({
            "date": f[0],
            "open": to_float(f[1]),
            "close": to_float(f[2]),
            "high": to_float(f[3]),
            "low": to_float(f[4]),
            "volume_hands": to_float(f[5]),
            "amount_yuan": to_float(f[6]),
            "turnover_pct": to_float(f[10]),
        })
    return bars


def fetch_kline(code, days, cutoff):
    end = cutoff.replace("-", "") if cutoff else "20500101"
    url = EASTMONEY_KLINE_URL.format(
        secid=em_secid(code), days=max(days, 1), end=end)
    try:
        data = decode_json(http_get(url))
        node = data.get("data") or {}
        bars = parse_em_kline(node.get("klines") or [])
        if bars:
            return bars, "东方财富日K（精确换手率）", []
    except Exception:  # noqa: BLE001 东财失败后按约定切腾讯日K
        pass

    bars = []
    for row in fallback_sources.fetch_tencent_kline(code, days=max(days, 1)):
        if len(row) < 6:
            continue
        bars.append({
            "date": str(row[0]),
            "open": to_float(row[1]),
            "close": to_float(row[2]),
            "high": to_float(row[3]),
            "low": to_float(row[4]),
            "volume_hands": to_float(row[5]),
            "amount_yuan": None,
            "turnover_pct": None,
        })

    quotes = fallback_sources.fetch_tencent_quotes([code])
    quote = quotes.get(code) or {}
    price = to_float(quote.get("price"))
    float_cap_yi = to_float(quote.get("float_cap_yi"))
    float_shares = (float_cap_yi * 1e8 / price
                    if price and float_cap_yi else None)
    limitations = []
    if float_shares:
        for bar in bars:
            if bar["volume_hands"] is not None:
                bar["turnover_pct"] = round(
                    bar["volume_hands"] * 100.0 * 100.0 / float_shares, 4)
        limitations.append("东财日K不可用；换手率按当前流通股本近似，筹码为推演值")
    else:
        limitations.append("东财日K不可用且腾讯流通股本不足，换手率未获取")
    return bars, "腾讯日K（换手率≈）", limitations


def filter_bars(bars, cutoff):
    if not cutoff:
        return bars
    return [bar for bar in bars if bar.get("date") and bar["date"] <= cutoff]


def fetch_holders(code, cutoff):
    url = HOLDER_URL.format(code=code.upper())
    data = decode_json(http_get(url))
    rows = []
    for item in data.get("gdrs") or []:
        end_date = clean_date(item.get("END_DATE"))
        if cutoff and (not end_date or end_date > cutoff):
            continue
        rows.append({
            "end_date": end_date,
            "holder_count": item.get("HOLDER_TOTAL_NUM"),
            "holder_change_pct": to_float(item.get("TOTAL_NUM_RATIO")),
            "avg_free_shares": to_float(item.get("AVG_FREE_SHARES")),
            "hold_focus": item.get("HOLD_FOCUS"),
        })
    return rows


def parse_concept_row(row):
    return {
        "security_name": row.get("SECURITY_NAME_ABBR"),
        "board_code": row.get("NEW_BOARD_CODE") or row.get("BOARD_CODE"),
        "board_name": row.get("BOARD_NAME"),
        "is_precise": str(row.get("IS_PRECISE")) == "1",
        "board_rank": row.get("BOARD_RANK"),
    }


def fetch_concepts(codes):
    digits = [code[2:] for code in codes]
    quoted = ",".join('"{}"'.format(code) for code in digits)
    filter_expr = urllib.parse.quote(
        "(SECURITY_CODE in ({}))".format(quoted), safe="()")
    result = {code: [] for code in codes}
    for page in range(1, 21):
        url = BOARD_URL.format(
            report=BOARD_FILTER_REPORT,
            columns=BOARD_COLUMNS,
            page=page,
            filter=filter_expr,
        )
        data = decode_json(http_get(url))
        rows = (data.get("result") or {}).get("data") or []
        for row in rows:
            code = fallback_sources.infer_sec_code(row.get("SECURITY_CODE"))
            if code in result:
                result[code].append(parse_concept_row(row))
        if len(rows) < 500:
            break
    return result


def triangular_weight(price, low, mode, high):
    price = min(max(price, low), high)
    if high <= low:
        return 1.0
    if mode <= low:
        return 2.0 * (high - price) / ((high - low) ** 2)
    if mode >= high:
        return 2.0 * (price - low) / ((high - low) ** 2)
    if price <= mode:
        return 2.0 * (price - low) / ((high - low) * (mode - low))
    return 2.0 * (high - price) / ((high - low) * (high - mode))


def weighted_quantile(prices, weights, ratio):
    target = sum(weights) * ratio
    cumulative = 0.0
    for price, weight in zip(prices, weights):
        cumulative += weight
        if cumulative >= target:
            return price
    return prices[-1] if prices else None


def compute_chip_distribution(bars, current_price=None):
    usable = [
        bar for bar in bars
        if all(bar.get(key) is not None for key in ("low", "high", "turnover_pct"))
        and bar["high"] >= bar["low"] > 0
        and bar["turnover_pct"] > 0
    ]
    if not usable:
        return None

    global_low = min(bar["low"] for bar in usable)
    global_high = max(bar["high"] for bar in usable)
    step = max(0.01, (global_high - global_low) / 2000.0)
    count = max(1, int(math.ceil((global_high - global_low) / step)) + 1)
    prices = [global_low + i * step for i in range(count)]
    weights = [0.0] * count

    for bar in usable:
        turnover = min(max(bar["turnover_pct"] / 100.0, 0.0), 1.0)
        if turnover <= 0:
            continue
        weights = [weight * (1.0 - turnover) for weight in weights]
        low, high = bar["low"], bar["high"]
        avg = ((bar.get("open") or bar["close"])
               + bar["high"] + bar["low"] + bar["close"]) / 4.0
        mode = min(max(avg, low), high)
        if high <= low:
            index = min(count - 1, max(0, int(round((low - global_low) / step))))
            weights[index] += turnover
            continue
        start = max(0, int((low - global_low) / step))
        stop = min(count - 1, int(math.ceil((high - global_low) / step)))
        new_weights = []
        total = 0.0
        for index in range(start, stop + 1):
            value = triangular_weight(prices[index], low, mode, high)
            new_weights.append((index, value))
            total += value
        if total <= 0:
            continue
        for index, value in new_weights:
            weights[index] += turnover * value / total

    total_weight = sum(weights)
    if total_weight <= 0:
        return None
    weights = [weight / total_weight for weight in weights]
    last_close = bars[-1].get("close")
    price_now = current_price if current_price is not None else last_close
    if price_now is None:
        return None

    p15 = weighted_quantile(prices, weights, 0.15)
    p5 = weighted_quantile(prices, weights, 0.05)
    p85 = weighted_quantile(prices, weights, 0.85)
    p95 = weighted_quantile(prices, weights, 0.95)
    avg_cost = sum(price * weight for price, weight in zip(prices, weights))
    profit_ratio = sum(
        weight for price, weight in zip(prices, weights) if price <= price_now)
    peak_price = prices[max(range(count), key=weights.__getitem__)]

    return {
        "data_date": usable[-1]["date"],
        "sample_days": len(usable),
        "current_price": round(price_now, 3),
        "avg_cost": round(avg_cost, 3),
        "peak_price": round(peak_price, 3),
        "cost70_low": round(p15, 3),
        "cost70_high": round(p85, 3),
        "cost90_low": round(p5, 3),
        "cost90_high": round(p95, 3),
        "concentration70_pct": round(
            (p85 - p15) * 100.0 / (p85 + p15), 2) if p85 + p15 else None,
        "concentration90_pct": round(
            (p95 - p5) * 100.0 / (p95 + p5), 2) if p95 + p5 else None,
        "profit_ratio_pct": round(profit_ratio * 100.0, 2),
        "above_avg_pct": round((price_now / avg_cost - 1.0) * 100.0, 2)
        if avg_cost else None,
        "note": "按日K换手率衰减并以三角分布推演，非券商原始筹码",
    }


def build_payload(code, days, cutoff):
    bars, source, limitations = fetch_kline(code, days, cutoff)
    bars = filter_bars(bars, cutoff)
    if not bars:
        raise RuntimeError("{} 未获取到截止 {} 的日K".format(code, cutoff or "最新交易日"))
    if len(bars) < min(days, 120):
        limitations.append("日K仅 {} 根，少于请求的 {} 根".format(
            len(bars), days))

    holders = []
    try:
        holders = fetch_holders(code, cutoff)
        if cutoff:
            limitations.append(
                "股东户数按截止日截断；F10未提供披露日，历史数据仍可能含公告滞后")
    except Exception as exc:  # noqa: BLE001 单段失败不丢整份报告
        limitations.append("股东户数未获取：{}".format(exc))

    concepts = []
    try:
        concepts = fetch_concepts([code]).get(code, [])
        if cutoff:
            limitations.append("题材归属取当前 F10，历史数据可能含事后归属偏差")
    except Exception as exc:  # noqa: BLE001
        limitations.append("题材归属未获取：{}".format(exc))

    name = next(
        (row.get("security_name") for row in concepts
         if row.get("security_name")), None)
    payload = {
        "code": code,
        "name": name or code,
        "data_date": bars[-1]["date"],
        "holders": holders,
        "chips": compute_chip_distribution(bars, bars[-1].get("close")),
        "concepts": concepts,
        "source": {
            "holders": "东方财富 F10 股东研究",
            "chips": source,
            "concepts": "东方财富 F10 所属板块",
        },
        "limitations": limitations,
    }
    return payload


def print_text(payload):
    print("=" * 88)
    print("{} ({}) · 数据日期 {}".format(
        payload["name"], payload["code"], payload["data_date"]))
    print("=" * 88)
    print("股东户数（东方财富 F10）")
    print("{:<12}{:>14}{:>12}{:>16}{:>12}".format(
        "截止日", "股东户数", "较前期%", "户均自由流通股", "筹码状态"))
    if payload["holders"]:
        for row in payload["holders"][:8]:
            print("{:<12}{:>14}{:>12}{:>16}{:>12}".format(
                str(row.get("end_date") or "-"),
                fmt_int(row.get("holder_count")),
                fmt_num(row.get("holder_change_pct")),
                fmt_num(row.get("avg_free_shares"), 0),
                str(row.get("hold_focus") or "-"),
            ))
    else:
        print("未获取")

    print("-" * 88)
    print("筹码分布（{}）".format(payload["source"]["chips"]))
    chip = payload["chips"]
    if chip:
        print("平均成本 {:.3f}    筹码峰 {:.3f}    现价 {:.3f}（高于均价 {:.2f}%）".format(
            chip["avg_cost"], chip["peak_price"], chip["current_price"],
            chip["above_avg_pct"] or 0))
        print("70%成本 {:.3f}-{:.3f}（集中度 {:.2f}%）    "
              "90%成本 {:.3f}-{:.3f}（集中度 {:.2f}%）".format(
                  chip["cost70_low"], chip["cost70_high"],
                  chip["concentration70_pct"],
                  chip["cost90_low"], chip["cost90_high"],
                  chip["concentration90_pct"]))
        print("获利盘 {:.2f}%    样本 {} 个交易日".format(
            chip["profit_ratio_pct"], chip["sample_days"]))
    else:
        print("未获取（换手率或日K不足）")

    print("-" * 88)
    print("题材归属（精确概念优先，代码求交）")
    concepts = [row for row in payload["concepts"] if row.get("is_precise")]
    if concepts:
        for row in concepts[:30]:
            print("{} {}（{}）".format(
                row.get("board_code") or "-",
                row.get("board_name") or "-",
                "精确" if row.get("is_precise") else "宽口径",
            ))
    else:
        print("未获取")
    print("=" * 88)
    for limitation in payload.get("limitations") or []:
        print("注：{}".format(limitation))
    print("注：筹码为公开日K+换手率推演值，不是券商原始筹码；推演值不可当作精确持仓成本。")


def main():
    ap = argparse.ArgumentParser(description="A股 股东户数、筹码分布与题材归属报告")
    ap.add_argument("codes", nargs="+", help="一个代码做完整报告；多个代码须加 --concepts-only")
    ap.add_argument("--date", help="历史数据截止日期 YYYYMMDD")
    ap.add_argument("--days", type=int, default=120, help="筹码推演日K根数，默认120")
    ap.add_argument("--concepts-only", action="store_true",
                    help="只输出题材归属，可一次传多个代码")
    ap.add_argument("--json", action="store_true", help="输出JSON")
    args = ap.parse_args()

    codes = [
        normalize_code(code)
        for value in args.codes
        for code in value.split(",")
        if code.strip()
    ]
    cutoff = None
    if args.date:
        if not re.match(r"^\d{8}$", args.date):
            ap.error("--date 格式应为 YYYYMMDD")
        cutoff = "{}-{}-{}".format(args.date[:4], args.date[4:6], args.date[6:])

    if args.concepts_only:
        payload = {
            "codes": fetch_concepts(codes),
            "source": "东方财富 F10 所属板块",
            "note": "题材归属为当前 F10 口径；历史数据可能含事后归属偏差。",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        for code in codes:
            print("{}:".format(code))
            for row in payload["codes"].get(code, []):
                print("  {} {}（{}）".format(
                    row.get("board_code") or "-",
                    row.get("board_name") or "-",
                    "精确" if row.get("is_precise") else "宽口径",
                ))
        return

    if len(codes) != 1:
        ap.error("完整报告一次只接受一个代码；多个代码请加 --concepts-only")
    if args.days < 20:
        ap.error("--days 至少为 20，否则筹码样本无效")
    payload = build_payload(codes[0], args.days, cutoff)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)


if __name__ == "__main__":
    http_util.run(main)
