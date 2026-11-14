#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leader-hit 自带的标准库行情层。

只包含低位主升筛选需要的最小能力：沪深主板全市场快照、个股日 K、
腾讯备用行情，以及根据日 K 换手率推演的筹码分布。不依赖其他 skill。
"""

import json
import math
import urllib.request

USER_AGENT = "Mozilla/5.0"
TENCENT_REFERER = "https://gu.qq.com/"
EASTMONEY_REFERER = "https://data.eastmoney.com/"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
EASTMONEY_KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=1&lmt={days}&end={end}"
)
TENCENT_KLINE_URL = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    "?param={code},day,,,{days},qfq"
)
# 强制直连，避免本机系统代理影响公开行情接口。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_EASTMONEY_KLINE_UNAVAILABLE = False


def to_float(value):
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        return float(value)
    except (TypeError, ValueError):
        return None


def http_get(url, referer, timeout=20):
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Referer": referer,
    })
    with _OPENER.open(request, timeout=timeout) as response:
        return response.read()


def open_url(request, timeout=20):
    return _OPENER.open(request, timeout=timeout)


def decode_json(raw):
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = raw.decode("gb18030", errors="replace")
    return json.loads(text)


def em_secid(code):
    return ("1." if code.startswith("sh") else "0.") + code[2:]


def parse_tencent_quote(code, body):
    fields = body.split("~")
    if len(fields) < 50:
        return None
    return {
        "code": code,
        "name": fields[1],
        "price": to_float(fields[3]),
        "prev_close": to_float(fields[4]),
        "change_pct": to_float(fields[32]),
        "high": to_float(fields[33]),
        "low": to_float(fields[34]),
        "amount_yuan": (
            to_float(fields[37]) * 10000.0
            if to_float(fields[37]) is not None else None
        ),
        "turnover_pct": to_float(fields[38]),
        "amplitude_pct": to_float(fields[43]),
        "float_cap_yi": to_float(fields[44]),
        "total_cap_yi": to_float(fields[45]),
        "volume_ratio": to_float(fields[49]),
        "datetime": fields[30],
    }


def fetch_tencent_quotes(codes, chunk=50):
    quotes = {}
    codes = list(codes)
    for start in range(0, len(codes), chunk):
        batch = codes[start:start + chunk]
        try:
            raw = http_get(
                TENCENT_QUOTE_URL.format(codes=",".join(batch)),
                TENCENT_REFERER,
            ).decode("gbk", errors="ignore")
        except Exception:  # noqa: BLE001 单批失败不阻塞其他批次
            continue
        for line in raw.splitlines():
            if not line.startswith("v_"):
                continue
            code = line.split("=", 1)[0][2:].strip()
            try:
                body = line.split('"', 2)[1]
            except IndexError:
                continue
            quote = parse_tencent_quote(code, body)
            if quote:
                quotes[code] = quote
    return quotes


def fetch_tencent_kline(code, days):
    data = decode_json(http_get(
        TENCENT_KLINE_URL.format(code=code, days=days),
        TENCENT_REFERER,
    ))
    node = (data.get("data") or {}).get(code) or {}
    return node.get("qfqday") or node.get("day") or []


def parse_eastmoney_kline(lines):
    bars = []
    for line in lines:
        fields = str(line).split(",")
        if len(fields) < 11:
            continue
        bars.append({
            "date": fields[0],
            "open": to_float(fields[1]),
            "close": to_float(fields[2]),
            "high": to_float(fields[3]),
            "low": to_float(fields[4]),
            "volume_hands": to_float(fields[5]),
            "amount_yuan": to_float(fields[6]),
            "turnover_pct": to_float(fields[10]),
        })
    return bars


def fetch_kline(code, days, cutoff=None):
    """优先东财精确换手率；失败后回退腾讯，并明确降级限制。"""
    global _EASTMONEY_KLINE_UNAVAILABLE
    end = cutoff.replace("-", "") if cutoff else "20500101"
    if not _EASTMONEY_KLINE_UNAVAILABLE:
        try:
            data = decode_json(http_get(
                EASTMONEY_KLINE_URL.format(
                    secid=em_secid(code), days=max(days, 1), end=end),
                EASTMONEY_REFERER,
            ))
            bars = parse_eastmoney_kline(
                (data.get("data") or {}).get("klines") or [])
            if bars:
                return bars, "东方财富日K（精确换手率）", []
        except Exception:  # noqa: BLE001 接口失败后本轮切腾讯
            _EASTMONEY_KLINE_UNAVAILABLE = True

    rows = fetch_tencent_kline(code, max(days, 1))
    bars = []
    for row in rows:
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

    quote = fetch_tencent_quotes([code]).get(code) or {}
    price = to_float(quote.get("price"))
    float_cap_yi = to_float(quote.get("float_cap_yi"))
    float_shares = (
        float_cap_yi * 1e8 / price
        if price and float_cap_yi else None
    )
    limitations = []
    if float_shares:
        for bar in bars:
            if bar["volume_hands"] is not None:
                bar["turnover_pct"] = round(
                    bar["volume_hands"] * 10000.0 / float_shares, 4)
        limitations.append("东财日K不可用；换手率按当前流通股本近似，筹码为推演值")
    else:
        limitations.append("东财日K不可用且腾讯流通股本不足，换手率未获取")
    return bars, "腾讯日K（换手率≈）", limitations


def filter_bars(bars, cutoff):
    if not cutoff:
        return bars
    return [
        bar for bar in bars
        if bar.get("date") and bar["date"] <= cutoff
    ]


def clear_duplicate_gain10(rows):
    pairs = [
        (row.get("change5_pct"), row.get("change10_pct"))
        for row in rows
        if row.get("change5_pct") is not None
        and row.get("change10_pct") is not None
    ]
    if len(pairs) >= 2 and all(five == ten for five, ten in pairs):
        for row in rows:
            row["change10_pct"] = None
            row["change10_duplicate"] = True
        return True
    return False


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
    prices = [global_low + index * step for index in range(count)]
    weights = [0.0] * count

    for bar in usable:
        turnover = min(max(bar["turnover_pct"] / 100.0, 0.0), 1.0)
        if turnover <= 0:
            continue
        weights = [weight * (1.0 - turnover) for weight in weights]
        low, high = bar["low"], bar["high"]
        average = (
            (bar.get("open") or bar["close"])
            + bar["high"] + bar["low"] + bar["close"]
        ) / 4.0
        mode = min(max(average, low), high)
        if high <= low:
            index = min(count - 1, max(0, int(round((low - global_low) / step))))
            weights[index] += turnover
            continue
        start = max(0, int((low - global_low) / step))
        stop = min(count - 1, int(math.ceil((high - global_low) / step)))
        additions = []
        total = 0.0
        for index in range(start, stop + 1):
            value = triangular_weight(prices[index], low, mode, high)
            additions.append((index, value))
            total += value
        if total <= 0:
            continue
        for index, value in additions:
            weights[index] += turnover * value / total

    total_weight = sum(weights)
    if total_weight <= 0:
        return None
    weights = [weight / total_weight for weight in weights]
    price_now = current_price if current_price is not None else bars[-1].get("close")
    if price_now is None:
        return None

    p15 = weighted_quantile(prices, weights, 0.15)
    p5 = weighted_quantile(prices, weights, 0.05)
    p85 = weighted_quantile(prices, weights, 0.85)
    p95 = weighted_quantile(prices, weights, 0.95)
    average_cost = sum(
        price * weight for price, weight in zip(prices, weights))
    profit_ratio = sum(
        weight for price, weight in zip(prices, weights)
        if price <= price_now
    )
    peak_price = prices[max(range(count), key=weights.__getitem__)]
    return {
        "data_date": usable[-1]["date"],
        "sample_days": len(usable),
        "current_price": round(price_now, 3),
        "avg_cost": round(average_cost, 3),
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
        "above_avg_pct": round((price_now / average_cost - 1.0) * 100.0, 2)
        if average_cost else None,
        "note": "按日K换手率衰减并以三角分布推演，非券商原始筹码",
    }
