#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低位主升候选的行情整理与结构判定。

两种用法：
    python prepare_leader_candidates.py --price-max 20 --compact
    python prepare_leader_candidates.py --codes 600000,000001 --compact

脚本只根据日 K、成交量和推演筹码给出结构状态，不替代题材、公告和消息核验，
也不产生交易指令。
"""

import argparse
import json
import math
import re
import statistics
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import market_data  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HOSTS = (
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "https://push2test.eastmoney.com",
)
UNIVERSE_FIELDS = (
    "f2,f3,f6,f8,f10,f12,f13,f14,f15,f20,f21,f62,f109,f124,f160"
)
MAINBOARD_RE = re.compile(
    r"^(?:sh(?:600|601|603|605)|sz(?:000|001|002|003))\d{3}$"
)
SHANGHAI_TZ = timezone(timedelta(hours=8))


def to_float(value):
    try:
        if isinstance(value, str):
            value = value.replace(",", "")
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_code(value):
    text = str(value).strip().lower()
    if re.match(r"^\d{6}$", text):
        text = ("sh" if text.startswith("6") else "sz") + text
    if not re.match(r"^(?:sh|sz)\d{6}$", text):
        raise ValueError("代码格式应为 6 位数字或 sh/sz + 6 位数字：{}".format(value))
    return text


def is_mainboard(code):
    return bool(MAINBOARD_RE.match(str(code or "").lower()))


def quote_date_from_ts(value):
    ts = to_float(value)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, SHANGHAI_TZ).strftime("%Y-%m-%d")


def pct_change(now, before):
    if now is None or not before:
        return None
    return round((now / before - 1.0) * 100.0, 2)


def moving_average(values, span):
    usable = [value for value in values[-span:] if value is not None]
    return sum(usable) / span if len(usable) == span else None


def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    })
    with market_data.open_url(req, timeout=20) as response:
        return response.read()


def decode_json(raw):
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        text = raw.decode("gb18030", errors="replace")
    return json.loads(text)


def infer_sec_code(market, digits):
    return ("sh" if str(market) == "1" else "sz") + digits


def normalize_universe_row(raw):
    digits = str(raw.get("f12") or "")
    if len(digits) != 6 or not digits.isdigit():
        return None
    code = infer_sec_code(raw.get("f13"), digits)
    name = str(raw.get("f14") or "")
    if not is_mainboard(code) or "ST" in name.upper() or "退" in name:
        return None
    return {
        "code": code,
        "name": name,
        "price": to_float(raw.get("f2")),
        "change_pct": to_float(raw.get("f3")),
        "amount_yuan": to_float(raw.get("f6")),
        "turnover_pct": to_float(raw.get("f8")),
        "volume_ratio": to_float(raw.get("f10")),
        "high": to_float(raw.get("f15")),
        "total_cap_yi": round(to_float(raw.get("f20")) / 1e8, 2)
        if to_float(raw.get("f20")) is not None else None,
        "float_cap_yi": round(to_float(raw.get("f21")) / 1e8, 2)
        if to_float(raw.get("f21")) is not None else None,
        "main_inflow_yuan": to_float(raw.get("f62")),
        "change5_pct": to_float(raw.get("f109")),
        "change10_pct": to_float(raw.get("f160")),
        "quote_date": quote_date_from_ts(raw.get("f124")),
    }


def fetch_mainboard_universe(price_max):
    """取沪深主板快照并只保留价格低于 price_max 的非 ST 股票。"""
    rows = []
    last_error = None
    for page_no in range(1, 81):
        page = None
        for host in HOSTS:
            url = (
                "{host}/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
                "&fltt=2&invt=2&fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                "&fields={fields}"
            ).format(host=host, page=page_no, fields=UNIVERSE_FIELDS)
            try:
                data = decode_json(http_get(url))
                if data.get("rc") != 0:
                    last_error = "接口无数据 rc={}".format(data.get("rc"))
                    continue
                page = (data.get("data") or {}).get("diff") or []
                break
            except Exception as exc:  # noqa: BLE001 单页切换节点
                last_error = str(exc)
        if page is None:
            raise RuntimeError("沪深主板行情拉取失败：{}".format(last_error))
        if not page:
            break
        for raw in page:
            row = normalize_universe_row(raw)
            if row and row["price"] is not None and row["price"] < price_max:
                rows.append(row)
        if len(page) < 100:
            break
    market_data.clear_duplicate_gain10(rows)
    return rows


def code_to_quote(code):
    quotes = market_data.fetch_tencent_quotes([code])
    quote = quotes.get(code)
    if not quote:
        raise RuntimeError("{} 未获取到行情".format(code))
    raw_date = str(quote.get("datetime") or "")
    quote_date = (
        "{}-{}-{}".format(raw_date[:4], raw_date[4:6], raw_date[6:8])
        if len(raw_date) >= 8 and raw_date[:8].isdigit() else None
    )
    return {
        "code": code,
        "name": quote.get("name"),
        "price": quote.get("price"),
        "change_pct": quote.get("change_pct"),
        "amount_yuan": quote.get("amount_yuan"),
        "turnover_pct": quote.get("turnover_pct"),
        "volume_ratio": quote.get("volume_ratio"),
        "high": quote.get("high"),
        "float_cap_yi": quote.get("float_cap_yi"),
        "total_cap_yi": quote.get("total_cap_yi"),
        "quote_date": quote_date,
    }


def historical_quote(code, name, bar):
    """用目标日收盘 bar 生成历史行情，禁止把当日实时价带入回放。"""
    return {
        "code": code,
        "name": name,
        "price": bar.get("close"),
        "change_pct": None,
        "amount_yuan": bar.get("amount_yuan"),
        "turnover_pct": bar.get("turnover_pct"),
        "volume_ratio": None,
        "high": bar.get("high"),
        "float_cap_yi": None,
        "total_cap_yi": None,
        "quote_date": bar.get("date"),
    }


def quote_prefilter_score(quote):
    """在拉历史行情前，用快照做低成本排序：温和波动、有量但不极热的优先。"""
    score = 0
    change5 = quote.get("change5_pct")
    change10 = quote.get("change10_pct")
    change_day = quote.get("change_pct")
    turnover = quote.get("turnover_pct")
    volume_ratio = quote.get("volume_ratio")
    amount = quote.get("amount_yuan") or 0
    if change5 is not None:
        score += 8 if -10 <= change5 <= 15 else -8 if change5 > 30 else -4 if change5 < -20 else 0
    if change10 is not None:
        score += 8 if -15 <= change10 <= 25 else -8 if change10 > 40 else -4 if change10 < -25 else 0
    if change_day is not None:
        score += 5 if -4 <= change_day <= 9 else -5 if abs(change_day) > 12 else 0
    if turnover is not None:
        score += 5 if 0.8 <= turnover <= 15 else -3 if turnover > 30 else 0
    if volume_ratio is not None:
        score += 3 if 0.5 <= volume_ratio <= 2.5 else -2 if volume_ratio > 4 else 0
    if amount >= 50_000_000:
        score += 3
    if (quote.get("main_inflow_yuan") or 0) > 0:
        score += 2
    return score


def preselect_quotes(quotes, limit):
    if limit <= 0:
        return quotes
    return sorted(
        quotes,
        key=lambda row: (quote_prefilter_score(row), row.get("amount_yuan") or 0),
        reverse=True,
    )[:limit]


def closing_return(bars, index, previous_index):
    if previous_index < 0:
        return None
    return pct_change(bars[index].get("close"), bars[previous_index].get("close"))


def bar_volume(bar):
    return bar.get("volume_hands")


def has_limit_up_chain(bars, start, stop):
    """窗口内连续两个一字涨停，按视频规则视为筹码断层风险。"""
    run = 0
    for index in range(max(start, 1), stop):
        change = closing_return(bars, index, index - 1)
        one_word = all(
            bars[index].get(key) == bars[index].get("close")
            for key in ("open", "high", "low")
        )
        if change is not None and change >= 9.4 and one_word:
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False


def build_features(quote, bars, cutoff=None):
    bars = market_data.filter_bars(bars, cutoff)
    bars = [
        bar for bar in bars[-120:]
        if all(bar.get(key) is not None for key in ("open", "high", "low", "close"))
    ]
    if len(bars) < 20:
        raise RuntimeError("日K不足 20 根，无法判断")

    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar_volume(bar) for bar in bars]
    current = quote.get("price") or closes[-1]
    day_return = pct_change(current, closes[-2] if len(closes) >= 2 else None)
    day_range = (
        bars[-1].get("high") - bars[-1].get("low")
        if bars[-1].get("high") is not None
        and bars[-1].get("low") is not None else None
    )
    close_location = (
        (bars[-1].get("close") - bars[-1].get("low")) / day_range
        if day_range and bars[-1].get("close") is not None else None
    )
    low120 = min(lows)
    high120 = max(highs)
    range_position = (
        (current - low120) / (high120 - low120) * 100.0
        if high120 > low120 else None
    )
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)
    mas = [value for value in (ma5, ma10, ma20) if value is not None]
    ma_spread = (
        (max(mas) - min(mas)) / statistics.mean(mas) * 100.0
        if len(mas) == 3 and statistics.mean(mas) else None
    )

    box = bars[-30:]
    box_high = max(bar["high"] for bar in box[:-1])
    box_low = min(bar["low"] for bar in box[:-1])
    box_range = pct_change(box_high, box_low)
    support20 = min(bar["low"] for bar in bars[-20:-1])
    prior_volumes = [value for value in volumes[-21:-1] if value]
    avg_prior_volume = statistics.mean(prior_volumes) if prior_volumes else None
    avg_volume5 = (
        statistics.mean([value for value in volumes[-5:] if value])
        if any(volumes[-5:]) else None
    )
    volume_ratio_20 = (
        avg_volume5 / avg_prior_volume
        if avg_volume5 and avg_prior_volume else None
    )
    day_volume_ratio = (
        volumes[-1] / avg_prior_volume
        if volumes[-1] and avg_prior_volume else None
    )

    limit_indices = []
    for index in range(max(1, len(bars) - 65), len(bars)):
        change = closing_return(bars, index, index - 1)
        if change is not None and change >= 9.4:
            limit_indices.append(index)
    left_limit_indices = [
        index for index in limit_indices
        if 5 <= len(bars) - 1 - index <= 60
    ]
    left_limit_indices_1_60 = [
        index for index in limit_indices
        if 1 <= len(bars) - 1 - index <= 60
    ]

    chips = market_data.compute_chip_distribution(bars, current)
    overhead_pct = (
        pct_change(chips.get("cost90_high"), current) if chips else None
    )
    profit_ratio = chips.get("profit_ratio_pct") if chips else None

    trigger = None
    if limit_indices:
        trigger_index = limit_indices[-1]
        trigger_volume = volumes[trigger_index]
        post_volumes = [value for value in volumes[trigger_index + 1:] if value]
        trigger = {
            "date": bars[trigger_index].get("date"),
            "bars_ago": len(bars) - 1 - trigger_index,
            "high": highs[trigger_index],
            "low": lows[trigger_index],
            "volume": trigger_volume,
            "post_avg_volume": statistics.mean(post_volumes) if post_volumes else None,
        }

    breakout_level = box_high
    breakout_source = "30日箱体上沿"
    if trigger and trigger.get("high") is not None:
        trigger_high = trigger["high"]
        if trigger_high < breakout_level:
            breakout_level = trigger_high
            breakout_source = "最近涨停压力位"
    breakout_margin = pct_change(current, breakout_level)

    return {
        "current_price": round(current, 3),
        "day_return_pct": round(day_return, 2)
        if day_return is not None else None,
        "close_location_pct": round(close_location * 100.0, 2)
        if close_location is not None else None,
        "range_position_pct": round(range_position, 2) if range_position is not None else None,
        "return5_pct": pct_change(current, closes[-6] if len(closes) >= 6 else None),
        "return20_pct": pct_change(current, closes[-21] if len(closes) >= 21 else None),
        "return60_pct": pct_change(current, closes[-61] if len(closes) >= 61 else None),
        "ma": {
            "5": round(ma5, 3) if ma5 else None,
            "10": round(ma10, 3) if ma10 else None,
            "20": round(ma20, 3) if ma20 else None,
        },
        "ma_spread_pct": round(ma_spread, 2) if ma_spread is not None else None,
        "box30": {
            "high": round(box_high, 3),
            "low": round(box_low, 3),
            "range_pct": box_range,
            "distance_to_high_pct": pct_change(box_high, current),
            "breakout_level": round(breakout_level, 3),
            "breakout_source": breakout_source,
            "breakout_margin_pct": round(breakout_margin, 2)
            if breakout_margin is not None else None,
        },
        "support20": round(support20, 3),
        "volume": {
            "ratio5_to_prior20": round(volume_ratio_20, 2)
            if volume_ratio_20 is not None else None,
            "today_to_prior20": round(day_volume_ratio, 2)
            if day_volume_ratio is not None else None,
        },
        "prior_limit_count_5_60": len(left_limit_indices),
        "limit_count_1_60": len(left_limit_indices_1_60),
        "latest_limit_trigger": trigger,
        "one_word_chain_recent": has_limit_up_chain(bars, len(bars) - 65, len(bars)),
        "chips": chips,
        "overhead_to_cost90_pct": round(overhead_pct, 2)
        if overhead_pct is not None else None,
        "profit_ratio_pct": profit_ratio,
        "bar_count": len(bars),
        "data_date": bars[-1].get("date"),
    }


def judging_helpers(features):
    chips = features.get("chips") or {}
    concentration70 = chips.get("concentration70_pct")
    concentration90 = chips.get("concentration90_pct")
    overhead = features.get("overhead_to_cost90_pct")
    profit = features.get("profit_ratio_pct")
    range_position = features.get("range_position_pct")
    return20 = features.get("return20_pct")
    return60 = features.get("return60_pct")
    day_return = features.get("day_return_pct")
    close_location = features.get("close_location_pct")
    trigger = features.get("latest_limit_trigger") or {}
    volume5 = (features.get("volume") or {}).get("ratio5_to_prior20")
    today_volume = (features.get("volume") or {}).get("today_to_prior20")
    box = features.get("box30") or {}

    chips_good = (
        concentration70 is not None and concentration70 <= 16
        and (concentration90 is None or concentration90 <= 32)
        and (overhead is None or overhead <= 25)
        and (profit is None or 35 <= profit <= 92)
    )
    wash_chips_good = (
        concentration70 is not None and concentration70 <= 16
        and (concentration90 is None or concentration90 <= 32)
        and (overhead is None or overhead <= 25)
        and (profit is None or profit <= 92)
    )
    shape_stable = (
        box.get("range_pct") is not None and box["range_pct"] <= 35
        and features.get("ma_spread_pct") is not None
        and features["ma_spread_pct"] <= 8
        and features.get("current_price", 0) >= features.get("support20", 0) * 0.97
    )
    volume_dry = volume5 is not None and volume5 <= 0.8
    breakout_level = box.get("breakout_level", box.get("high"))
    breakout_margin = box.get("breakout_margin_pct")
    breakout = (
        breakout_level is not None
        and features.get("current_price", 0) >= breakout_level
        and today_volume is not None and today_volume >= 1.4
        and (features.get("return5_pct") or 0) > 0
    )
    breakout_strength = (
        breakout
        and day_return is not None and day_return >= 3
        and close_location is not None and close_location >= 55
        and breakout_margin is not None and breakout_margin >= 0
    )
    shrinking_wash = (
        5 <= trigger.get("bars_ago", 999) <= 45
        and trigger.get("high") is not None
        and features.get("current_price", 0) >= trigger.get("low", math.inf) * 0.98
        and -18 <= pct_change(features.get("current_price"), trigger.get("high")) <= -1
        and volume5 is not None and volume5 <= 0.9
    )
    post_limit_hold = (
        1 <= trigger.get("bars_ago", 999) <= 4
        and trigger.get("high") is not None
        and features.get("current_price", 0) >= trigger.get("low", math.inf) * 0.98
        and (
            features.get("ma", {}).get("20") is None
            or features.get("current_price", 0) >= features["ma"]["20"]
        )
        and -18 <= pct_change(features.get("current_price"), trigger.get("high")) <= -1
    )
    wash = shrinking_wash or post_limit_hold
    high_position = (
        (range_position is not None and range_position >= 75)
        or (return20 is not None and return20 >= 35)
        or (return60 is not None and return60 >= 55)
        or (profit is not None and profit >= 92 and (return20 or 0) >= 20)
    )
    shrinking_decline = (
        (return20 or 0) <= -15
        and features.get("current_price", 0) < features.get("support20", 0)
        and volume5 is not None and volume5 <= 0.8
    )
    limit_count_1_60 = features.get("limit_count_1_60")
    if limit_count_1_60 is None:
        limit_count_1_60 = features.get("prior_limit_count_5_60", 0)
    no_left_limit = limit_count_1_60 == 0
    distance_to_high = box.get("distance_to_high_pct")
    early_watch = (
        no_left_limit
        and range_position is not None and range_position <= 55
        and box.get("range_pct") is not None and box["range_pct"] <= 15
        and features.get("ma_spread_pct") is not None
        and features["ma_spread_pct"] <= 3
        and volume5 is not None and volume5 <= 0.9
        and concentration70 is not None and concentration70 <= 16
        and (concentration90 is None or concentration90 <= 32)
        and (overhead is None or overhead <= 25)
        and (profit is None or profit <= 92)
        and (
            (distance_to_high is not None and distance_to_high <= 5)
            or (
                range_position <= 20
                and box.get("range_pct") is not None
                and box["range_pct"] <= 15
            )
        )
        and not high_position
        and not shrinking_decline
    )
    return {
        "chips_good": chips_good,
        "wash_chips_good": wash_chips_good,
        "shape_stable": shape_stable,
        "volume_dry": volume_dry,
        "breakout": breakout,
        "breakout_strength": breakout_strength,
        "wash": wash,
        "shrinking_wash": shrinking_wash,
        "post_limit_hold": post_limit_hold,
        "early_watch": early_watch,
        "high_position": high_position,
        "shrinking_decline": shrinking_decline,
    }


def classify(features):
    checks = judging_helpers(features)
    score = 0
    range_position = features.get("range_position_pct")
    if range_position is not None:
        score += 20 if range_position <= 35 else 12 if range_position <= 55 else 4
    chips = features.get("chips") or {}
    concentration70 = chips.get("concentration70_pct")
    overhead = features.get("overhead_to_cost90_pct")
    profit = features.get("profit_ratio_pct")
    score += 8 if concentration70 is not None and concentration70 <= 16 else 3
    score += 6 if overhead is not None and overhead <= 25 else 2
    score += 6 if profit is not None and 35 <= profit <= 92 else 2
    volume5 = (features.get("volume") or {}).get("ratio5_to_prior20")
    score += 10 if checks["volume_dry"] else 5 if volume5 is not None and volume5 <= 1.0 else 1
    score += 5 if checks["shape_stable"] else 2
    score += 5 if 1 <= features.get("prior_limit_count_5_60", 0) <= 4 else 1
    score += 10 if checks["breakout"] else 7 if checks["wash"] else 3
    score += 3 if features.get("ma", {}).get("20") and features.get("current_price", 0) >= features["ma"]["20"] else 0
    if checks["breakout_strength"]:
        day_return = features.get("day_return_pct")
        score += 15 if day_return is not None and day_return >= 9.4 else 10
        if (features.get("close_location_pct") or 0) >= 70:
            score += 4
        if (
            (features.get("box30") or {}).get("breakout_margin_pct") or 0
        ) >= 1:
            score += 3
    score = min(100, score)

    exclusions = []
    if features.get("bar_count", 0) < 80:
        exclusions.append("日K不足 80 根，时间沉淀不足")
    if (
        not (
            features.get("limit_count_1_60")
            if features.get("limit_count_1_60") is not None
            else features.get("prior_limit_count_5_60", 0)
        )
        and not checks["early_watch"]
    ):
        exclusions.append("近 5-60 个交易日缺少左侧涨停建仓证据")
    if features.get("one_word_chain_recent"):
        exclusions.append("近期出现连续一字板，存在筹码断层")
    if checks["high_position"]:
        exclusions.append("位置、阶段涨幅或获利盘已进入高位区")
    if checks["shrinking_decline"]:
        exclusions.append("缩量阴跌且跌破近 20 日支撑")
    if concentration70 is not None and concentration70 > 20:
        exclusions.append("筹码分散，未形成有效底部密集峰")

    if exclusions:
        status = "排除"
    elif checks["breakout_strength"]:
        status = "启动初段"
    elif checks["wash"] and checks["wash_chips_good"]:
        status = "洗盘中"
    elif checks["volume_dry"] and checks["shape_stable"] and checks["chips_good"]:
        status = "待启动"
    elif (
        checks["chips_good"]
        and features.get("prior_limit_count_5_60", 0) >= 2
        and (range_position or 100) <= 55
    ):
        status = "建仓完成"
    else:
        status = "只观察"

    if status in ("建仓完成", "待启动") and not checks["chips_good"]:
        status = "只观察"
    if checks["early_watch"]:
        status = "只观察"
        exclusions.append("早期观察通道：无左侧涨停，等待放量突破")
    if checks["breakout"] and not checks["breakout_strength"]:
        status = "只观察"
        exclusions.append("突破确认不足：当日涨幅、突破幅度或收盘位置未达启动门槛")
    return status, score, checks, exclusions


def analyze_row(quote, cutoff=None, bars=None, source=None, limitations=None):
    if bars is None:
        bars, source, limitations = market_data.fetch_kline(
            quote["code"], 500 if cutoff else 120, cutoff)
    features = build_features(quote, bars, cutoff)
    status, score, checks, exclusions = classify(features)
    if (
        quote.get("quote_date")
        and features.get("data_date")
        and quote["quote_date"] != features["data_date"]
    ):
        status = "只观察"
        exclusions.append("行情日期与日K日期不一致，不能给出启动结论")
    return {
        "code": quote["code"],
        "name": quote.get("name"),
        "price": round(quote["price"], 3) if quote.get("price") is not None else None,
        "quote_date": quote.get("quote_date"),
        "data_date": features.get("data_date"),
        "status": status,
        "technical_score": score,
        "checks": checks,
        "exclusions": exclusions,
        "features": features,
        "theme_confirmation": "未核验",
        "source": source,
        "limitations": limitations,
        "metric_quality": "degraded" if limitations else "exact",
    }


def scan_historical_universe(price_max, cutoff, workers, universe_only=False):
    """回放目标日的全市场低价股票，股票池和价格都不取当前快照。"""
    universe = fetch_mainboard_universe(1e12)
    rows = []
    eligible_count = 0

    def work(raw):
        bars, source, limitations = market_data.fetch_kline(
            raw["code"], 500, cutoff)
        bars = market_data.filter_bars(bars, cutoff)
        if not bars:
            raise RuntimeError("目标日前日K未获取")
        quote = historical_quote(raw["code"], raw.get("name"), bars[-1])
        if quote.get("price") is None or quote["price"] >= price_max:
            return None
        if universe_only:
            return {
                "code": quote["code"],
                "name": quote.get("name"),
                "price": quote["price"],
                "quote_date": quote.get("quote_date"),
                "data_date": quote.get("quote_date"),
                "status": "行情列表",
                "technical_score": None,
                "theme_confirmation": "未核验",
                "exclusions": [],
            }
        return analyze_row(
            quote, cutoff, bars=bars, source=source, limitations=limitations)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        futures = {
            executor.submit(work, raw): raw
            for raw in universe
        }
        for future in as_completed(futures):
            raw = futures[future]
            try:
                row = future.result()
                if row is not None:
                    rows.append(row)
                    eligible_count += 1
            except Exception as exc:  # noqa: BLE001 单票失败保留数据缺失
                rows.append({
                    "code": raw["code"],
                    "name": raw.get("name"),
                    "price": None,
                    "quote_date": None,
                    "data_date": None,
                    "status": "排除",
                    "technical_score": 0,
                    "exclusions": ["数据未获取：{}".format(exc)],
                    "theme_confirmation": "未核验",
                    "metric_quality": "unavailable",
                })
    status_order = {
        "启动初段": 0,
        "待启动": 1,
        "洗盘中": 2,
        "建仓完成": 3,
        "只观察": 4,
        "排除": 5,
        "行情列表": 6,
    }
    rows.sort(key=lambda row: (
        status_order.get(row.get("status"), 9),
        -(row.get("technical_score") or 0),
        row.get("code") or "",
    ))
    return rows, len(universe), eligible_count


def scan_rows(quotes, cutoff, scan_limit, workers):
    rows = []
    selected = quotes[:scan_limit] if scan_limit > 0 else quotes
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        futures = {
            executor.submit(analyze_row, quote, cutoff): quote
            for quote in selected
        }
        for future in as_completed(futures):
            quote = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # noqa: BLE001 单票失败保留数据缺失
                rows.append({
                    "code": quote["code"],
                    "name": quote.get("name"),
                    "price": quote.get("price"),
                    "quote_date": quote.get("quote_date"),
                    "data_date": None,
                    "status": "排除",
                    "technical_score": 0,
                    "exclusions": ["数据未获取：{}".format(exc)],
                    "theme_confirmation": "未核验",
                })
    status_order = {
        "启动初段": 0,
        "待启动": 1,
        "洗盘中": 2,
        "建仓完成": 3,
        "只观察": 4,
        "排除": 5,
    }
    rows.sort(key=lambda row: (
        status_order.get(row.get("status"), 9),
        -row.get("technical_score", 0),
        row.get("code") or "",
    ))
    return rows


def compact_payload(payload, limit):
    status_counts = {}
    for row in payload["rows"]:
        status_counts[row.get("status")] = status_counts.get(row.get("status"), 0) + 1
    compact_rows = []
    for row in payload["rows"][:limit]:
        features = row.get("features") or {}
        compact_rows.append({
            "code": row.get("code"),
            "name": row.get("name"),
            "price": row.get("price"),
            "status": row.get("status"),
            "technical_score": row.get("technical_score"),
            "quote_date": row.get("quote_date"),
            "data_date": row.get("data_date"),
            "day_return_pct": features.get("day_return_pct"),
            "close_location_pct": features.get("close_location_pct"),
            "range_position_pct": features.get("range_position_pct"),
            "return20_pct": features.get("return20_pct"),
            "return60_pct": features.get("return60_pct"),
            "concentration70_pct": (features.get("chips") or {}).get("concentration70_pct"),
            "profit_ratio_pct": features.get("profit_ratio_pct"),
            "overhead_to_cost90_pct": features.get("overhead_to_cost90_pct"),
            "volume5_to_prior20": (features.get("volume") or {}).get("ratio5_to_prior20"),
            "prior_limit_count_5_60": features.get("prior_limit_count_5_60"),
            "limit_count_1_60": features.get("limit_count_1_60"),
            "breakout_margin_pct": (
                features.get("box30") or {}).get("breakout_margin_pct"),
            "early_watch": (row.get("checks") or {}).get("early_watch"),
            "theme_confirmation": row.get("theme_confirmation"),
            "exclusions": row.get("exclusions") or [],
            "metric_quality": row.get("metric_quality"),
        })
    return {
        "schema": "leader-hit.compact.v1",
        "mode": payload["mode"],
        "data_date": payload["data_date"],
        "generated_at": payload["generated_at"],
        "price_max": payload["price_max"],
        "universe_count": payload.get("universe_count"),
        "scanned_count": payload.get("scanned_count"),
        "status_counts": status_counts,
        "rows": compact_rows,
        "returned": len(compact_rows),
        "total_rows": len(payload["rows"]),
        "report_path": payload["report_path"],
        "note": "题材与消息未核验时不得判为可执行；筹码为日K+换手率推演值。",
    }


def render_text(payload):
    print("leader-hit · 数据日期 {} · 价格上限 {} 元".format(
        payload["data_date"] or "-", payload["price_max"]))
    print("{:<10}{:<8}{:>8}{:>10}{:>8}{:>8}{:>8}{:>10}".format(
        "代码", "名称", "价格", "状态", "技术分", "20日%", "筹码70", "左侧涨停"))
    print("-" * 76)
    for row in payload["rows"]:
        features = row.get("features") or {}
        print("{:<10}{:<8}{:>8}{:>10}{:>8}{:>8}{:>8}{:>10}".format(
            row.get("code") or "-",
            str(row.get("name") or "-")[:8],
            "-" if row.get("price") is None else "{:.2f}".format(row["price"]),
            row.get("status") or "-",
            row.get("technical_score") or 0,
            "-" if features.get("return20_pct") is None else "{:+.2f}".format(features["return20_pct"]),
            "-" if (features.get("chips") or {}).get("concentration70_pct") is None
            else "{:.1f}".format(features["chips"]["concentration70_pct"]),
            features.get("prior_limit_count_5_60", "-"),
        ))
    print("\n注：左侧涨停=近5-60个交易日的涨停建仓证据；筹码为推演值。")
    print("注：题材、公告和消息的 theme_confirmation 默认未核验，不能据此直接执行。")


def main():
    parser = argparse.ArgumentParser(description="沪深主板低价主升结构筛选")
    parser.add_argument("--codes", nargs="+", help="指定代码，逗号或空格分隔")
    parser.add_argument("--price-max", type=float, default=20.0, help="价格上限，默认 20")
    parser.add_argument("--date", help="日K截止 YYYYMMDD")
    parser.add_argument("--scan-limit", type=int, default=120,
                        help="低价主板快照预排序后扫描前 N 只，0 为全部，默认 120")
    parser.add_argument("--display-limit", type=int, default=30, help="展示行数，默认 30")
    parser.add_argument("--workers", type=int, default=8, help="并发数，默认 8")
    parser.add_argument("--universe-only", action="store_true",
                        help="只列沪深主板低价股票，不做日K分类")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--compact", action="store_true")
    args = parser.parse_args()

    if args.price_max <= 0:
        parser.error("--price-max 必须大于 0")
    if args.display_limit <= 0:
        parser.error("--display-limit 必须大于 0")
    cutoff = None
    if args.date:
        if not re.match(r"^\d{8}$", args.date):
            parser.error("--date 格式应为 YYYYMMDD")
        cutoff = "{}-{}-{}".format(args.date[:4], args.date[4:6], args.date[6:])

    if args.codes:
        codes = list(dict.fromkeys(
            normalize_code(code)
            for value in args.codes
            for code in value.split(",")
            if code.strip()
        ))
        invalid = [code for code in codes if not is_mainboard(code)]
        if invalid:
            parser.error("仅支持沪深主板：{}".format(", ".join(invalid)))
        quotes = []
        direct_rows = []
        for code in codes:
            try:
                if cutoff:
                    bars, _source, _limitations = market_data.fetch_kline(
                        code, 500, cutoff)
                    bars = market_data.filter_bars(bars, cutoff)
                    if not bars:
                        raise RuntimeError("{} 未获取到目标日前日K".format(code))
                    current = market_data.fetch_tencent_quotes([code]).get(code) or {}
                    quote = historical_quote(
                        code, current.get("name") or code, bars[-1])
                    if (
                        quote.get("price") is not None
                        and quote["price"] < args.price_max
                    ):
                        quotes.append(quote)
                    else:
                        direct_rows.append({
                            "code": code,
                            "name": quote.get("name"),
                            "price": quote.get("price"),
                            "quote_date": quote.get("quote_date"),
                            "data_date": quote.get("quote_date"),
                            "status": "排除",
                            "technical_score": 0,
                            "exclusions": ["价格不在用户限定范围内"],
                            "theme_confirmation": "未核验",
                        })
                    continue
                quote = code_to_quote(code)
                if quote.get("price") is not None and quote["price"] < args.price_max:
                    quotes.append(quote)
                else:
                    direct_rows.append({
                        "code": code,
                        "name": quote.get("name"),
                        "price": quote.get("price"),
                        "quote_date": quote.get("quote_date"),
                        "data_date": None,
                        "status": "排除",
                        "technical_score": 0,
                        "exclusions": ["价格不在用户限定范围内"],
                        "theme_confirmation": "未核验",
                    })
            except Exception as exc:  # noqa: BLE001 单票失败交由分类行体现
                direct_rows.append({
                    "code": code,
                    "name": code,
                    "price": None,
                    "quote_date": None,
                    "data_date": None,
                    "status": "排除",
                    "technical_score": 0,
                    "exclusions": ["数据未获取：{}".format(exc)],
                    "theme_confirmation": "未核验",
                })
        rows = scan_rows(quotes, cutoff, 0, args.workers) + direct_rows
        rows.sort(key=lambda row: (
            {"只观察": 0, "排除": 1}.get(row.get("status"), 9),
            -row.get("technical_score", 0),
        ))
        mode = "codes"
        universe_count = len(codes)
        eligible_count = len(quotes)
        scan_quotes = quotes
    else:
        if cutoff:
            rows, universe_count, eligible_count = scan_historical_universe(
                args.price_max, cutoff, args.workers, args.universe_only)
            mode = "historical_universe"
            scan_quotes = rows
        else:
            quotes = fetch_mainboard_universe(args.price_max)
            mode = "universe"
            universe_count = len(quotes)
            eligible_count = len(quotes)
            if args.universe_only:
                rows = [{
                    "code": quote["code"],
                    "name": quote["name"],
                    "price": quote["price"],
                    "quote_date": quote["quote_date"],
                    "data_date": quote["quote_date"],
                    "status": "行情列表",
                    "technical_score": None,
                    "theme_confirmation": "未核验",
                    "exclusions": [],
                } for quote in quotes]
                rows = rows[:args.display_limit]
            else:
                scan_quotes = preselect_quotes(quotes, args.scan_limit)
                rows = scan_rows(scan_quotes, cutoff, 0, args.workers)

    dates = [row.get("data_date") for row in rows if row.get("data_date")]
    data_date = max(dates) if dates else (cutoff or None)
    generated_at = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    report_date = data_date or datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    report_path = "report/主升候选/{}.md".format(report_date)
    payload = {
        "mode": mode,
        "price_max": args.price_max,
        "data_date": data_date,
        "generated_at": generated_at,
        "universe_count": universe_count,
        "scanned_count": 0 if args.universe_only and not cutoff else len(scan_quotes),
        "eligible_count": eligible_count,
        "rows": rows,
        "report_path": report_path,
        "note": "只提供结构化行情证据；题材、公告与消息必须另行核验。",
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.compact:
        print(json.dumps(compact_payload(payload, args.display_limit),
                         ensure_ascii=False, separators=(",", ":")))
    else:
        render_text(payload)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("已中断。")
    except Exception as exc:  # noqa: BLE001 统一中文报错
        print("错误：{}".format(exc), file=sys.stderr)
        if "--json" in sys.argv:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
