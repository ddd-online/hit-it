#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 limit-hit 预计算打板候选、第二梯队初筛或单股审视的基础数据。

脚本只做数据装配、聚合和指标计算，不输出买卖结论：
- 当日涨停通过技能内取数脚本实拉；
- 昨日涨停池必须由 save_limit_pool.py 预先保存到 report/涨停池，缺失即停止；
- 运行时只拉当日实时涨停池，不自动生成或覆盖涨停池文件；
- 批量聚合精确概念和名称情绪组；
- 批量计算日K趋势、洗盘路径、量比和筹码；
- 单股模式可按名称或代码获取报价、量价、筹码和题材，不要求当日涨停。
"""

import argparse
import collections
import copy
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CN_TZ = timezone(timedelta(hours=8))
BOARD_PERMISSION = "沪深主板"
RULES = {
    "one_word_executable": True,
    "collapse_veto": False,
    "weak_market_gate": {
        "max_picks": 1,
        "min_score": 85,
        "override": {
            "enabled": True,
            "theme_score_threshold": 80,
            "min_score_by_mode": {
                "one_to_two": 80,
                "two_to_three": 79,
            },
        },
    },
    "second_line": {
        "admission": "strict",
        "retreat_cap": "watch_only",
        "collapse_cap": "watch_only",
    },
    "theme_score": {
        "missing_dimension": "exact_only",
    },
    "market_gate": {
        "retreat_time_gate": "10:30",
    },
}
TENCENT_SUGGEST_URL = "https://smartbox.gtimg.cn/s3/?q={query}&t=all"
EASTMONEY_SUGGEST_URL = (
    "https://searchapi.eastmoney.com/api/suggest/get"
    "?input={query}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10"
)
STOCK_CODE_RE = re.compile(
    r"^(?:(sh|sz|bj))?(\d{6})(?:\.(sh|sz|bj))?$", re.IGNORECASE)

sys.path.insert(0, str(SCRIPT_DIR))

import http_util  # noqa: E402 与脚本同目录的直连网络层

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


def build_stock_report_path(data_date, code):
    return Path.cwd() / "report" / "个股审视" / "{}_{}.md".format(data_date, code)


def build_limit_pool_path(data_date):
    return Path.cwd() / "report" / "涨停池" / "{}.json".format(data_date)


def normalize_stock_code(value):
    """把裸代码、交易所前缀或后缀统一为 sh/sz/bj + 6 位数字。"""
    text = str(value or "").strip().lower()
    match = STOCK_CODE_RE.match(text)
    if not match:
        raise ValueError("代码格式应为 6 位数字，或带 sh/sz/bj 前缀：{}".format(value))
    prefix, digits, suffix = match.groups()
    exchange = prefix or suffix
    if prefix and suffix and prefix != suffix:
        raise ValueError("股票代码前后交易所不一致：{}".format(value))
    if not exchange:
        if digits.startswith("6"):
            exchange = "sh"
        elif digits.startswith(("0", "1", "2", "3")):
            exchange = "sz"
        elif digits.startswith(("4", "8", "9")):
            exchange = "bj"
        else:
            raise ValueError("无法识别股票代码交易所：{}".format(value))
    return "{}{}".format(exchange, digits)


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


def parse_stock_suggestions(raw):
    """解析腾讯证券检索结果，只保留 A 股。"""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    match = re.search(r'v_hint="([^"]*)"', text)
    if not match or not match.group(1):
        return []
    output = []
    seen = set()
    for item in match.group(1).split("^"):
        fields = item.split("~")
        if len(fields) < 5 or fields[4] != "GP-A":
            continue
        try:
            code = normalize_stock_code("{}{}".format(fields[0], fields[1]))
        except ValueError:
            continue
        if code in seen:
            continue
        seen.add(code)
        name = fields[2].strip()
        try:
            name = json.loads('"{}"'.format(name))
        except ValueError:
            pass
        output.append({"code": code, "name": name})
    return output


def parse_eastmoney_suggestions(raw):
    """解析东方财富证券检索回退结果。"""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
        if "\ufffd" in text:
            text = raw.decode("gb18030", errors="replace")
    else:
        text = str(raw)
    payload = json.loads(text)
    rows = ((payload.get("QuotationCodeTable") or {}).get("Data") or [])
    output = []
    seen = set()
    for row in rows:
        try:
            code = normalize_stock_code(row.get("Code"))
        except ValueError:
            continue
        if code in seen:
            continue
        seen.add(code)
        output.append({"code": code, "name": str(row.get("Name") or "").strip()})
    return output


def fetch_stock_suggestions(query):
    encoded = urllib.parse.quote(str(query).strip())
    sources = (
        (TENCENT_SUGGEST_URL.format(query=encoded), "https://gu.qq.com/",
         parse_stock_suggestions),
        (EASTMONEY_SUGGEST_URL.format(query=encoded), "https://quote.eastmoney.com/",
         parse_eastmoney_suggestions),
    )
    errors = []
    for url, referer, parser in sources:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
            })
            with http_util.open_url(req, timeout=15) as resp:
                rows = parser(resp.read())
            if rows:
                return rows
        except Exception as exc:  # noqa: BLE001 一个检索源失败后继续回退
            errors.append(str(exc))
    if errors:
        raise RuntimeError("股票名称检索失败：{}".format("；".join(errors)))
    return []


def resolve_stock_query(query, suggestions_provider=None):
    """代码直接规范化；名称要求唯一精确匹配，歧义时停止。"""
    text = str(query or "").strip()
    if not text:
        raise ValueError("股票名称或代码不能为空")
    try:
        code = normalize_stock_code(text)
    except ValueError:
        code = None
    if code:
        return {"query": text, "code": code, "name": None}

    provider = suggestions_provider or fetch_stock_suggestions
    rows = provider(text)
    if not rows:
        raise ValueError("未找到股票：{}".format(text))
    exact = [row for row in rows if row.get("name") == text]
    if len(exact) == 1:
        return {"query": text, **exact[0]}
    if len(exact) > 1:
        labels = "、".join("{}（{}）".format(row["name"], row["code"]) for row in exact)
        raise ValueError("股票名称不唯一：{}；请输入代码".format(labels))
    if len(rows) == 1:
        return {"query": text, **rows[0]}
    labels = "、".join("{}（{}）".format(row["name"], row["code"]) for row in rows[:8])
    raise ValueError("股票名称不唯一或不够精确：{}；请输入代码".format(labels))


def _pct_change(now, before):
    if now is None or not before:
        return None
    return round((now / before - 1.0) * 100.0, 2)


def _quote_date(quote):
    value = str((quote or {}).get("datetime") or "")
    return "{}-{}-{}".format(value[:4], value[4:6], value[6:8]) if len(value) >= 8 else None


def build_stock_review(snapshot, data_date, query, stock, current, previous_rows,
                       concepts, quote_payload, metrics, structure):
    """装配单股事实，不输出持有或退出结论。"""
    code = stock["code"]
    quote = (quote_payload or {}).get("quote") or {}
    kline = (quote_payload or {}).get("kline") or []
    current_rows = current.get("zt_rows") or []
    current_row = next((row for row in current_rows if row.get("code") == code), None)
    previous_row = next((row for row in previous_rows if row.get("code") == code), None)
    performance = {
        row.get("code"): row
        for row in (current.get("yesterday_performance") or {}).get("rows", [])
    }.get(code)
    broken_codes = {row.get("code") for row in current.get("zb_all_rows") or []}
    down_codes = {row.get("code") for row in current.get("dt_rows") or []}

    pct = quote.get("change_pct")
    suspended = (
        _quote_date(quote) == data_date
        and all(quote.get(key) == 0 for key in ("open", "high", "low", "volume_hands"))
    )
    if current_row:
        limit_status = "涨停"
    elif code in broken_codes:
        limit_status = "炸板"
    elif code in down_codes:
        limit_status = "跌停"
    elif suspended:
        limit_status = "停牌"
    elif pct is None:
        limit_status = "未获取"
    elif pct > 0:
        limit_status = "上涨"
    elif pct < 0:
        limit_status = "下跌"
    else:
        limit_status = "平"

    quote_day = _quote_date(quote)
    previous_volume = next(
        (bar.get("volume") for bar in reversed(kline)
         if bar.get("date") and quote_day and bar["date"] < quote_day),
        None,
    )
    current_volume = quote.get("volume_hands")
    if current_volume is None:
        current_volume = (metrics or {}).get("last", {}).get("volume")
    is_partial_day = bool(
        quote_day == data_date
        and (snapshot.hour, snapshot.minute) < (15, 0)
    )
    themes = (
        current_row.get("themes") if current_row and "themes" in current_row
        else previous_row.get("themes") if previous_row and "themes" in previous_row
        else themes_for(code, concepts)
    ) or []
    limitations = list((metrics or {}).get("limitations") or [])
    limitations.extend((structure or {}).get("limitations") or [])
    return {
        "query": query,
        "code": code,
        "name": quote.get("name") or stock.get("name") or code,
        "themes": themes,
        "quote_date": quote_day,
        "quote_consistent": quote_day == data_date,
        "current_limit_status": limit_status,
        "in_current_limit_pool": current_row is not None,
        "current_board_count": current_row.get("board_count") if current_row else None,
        "in_previous_limit_pool": previous_row is not None,
        "previous_board_count": previous_row.get("board_count") if previous_row else None,
        "yesterday_performance": performance,
        "quote": quote,
        "metrics": metrics or {},
        "structure": {
            "holders": (structure or {}).get("holders") or [],
            "chips": (structure or {}).get("chips") or (metrics or {}).get("chips"),
            "sources": (structure or {}).get("source") or {},
        },
        "intraday": {
            "is_partial_day": is_partial_day,
            "price_vs_open_pct": _pct_change(quote.get("price"), quote.get("open")),
            "price_from_high_pct": _pct_change(quote.get("price"), quote.get("high")),
            "open_gap_pct": (metrics or {}).get("open_gap_pct"),
            "low_gap_pct": (metrics or {}).get("low_gap_pct"),
            "volume_ratio_5d": (metrics or {}).get("volume_ratio_5d"),
            "volume_ratio_scope": "盘中部分量" if is_partial_day else "全天量",
            "volume_vs_prev_day": round(current_volume / previous_volume, 2)
            if current_volume is not None and previous_volume else None,
            "turnover_pct": (
                quote.get("turnover_pct")
                if quote.get("turnover_pct") is not None
                else (metrics or {}).get("last", {}).get("turnover_pct")
            ),
            "amount_yuan": (metrics or {}).get("last", {}).get("amount_yuan"),
        },
        "limitations": list(dict.fromkeys(limitations)),
    }


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


def require_limit_pool(path, data_date):
    """运行时只接受预先保存的昨日涨停池，缺失时明确要求先执行保存脚本。"""
    payload = load_limit_pool(path, data_date)
    if payload is None:
        raise RuntimeError(
            "昨日涨停池文件不存在：{}；请先运行 "
            "save_limit_pool.py --date {}".format(
                path, compact_date(data_date)))
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


def precise_themes(row, concepts):
    return [
        theme for theme in row_themes(row, concepts)
        if not theme.startswith("名称组:")
    ]


def theme_summary(current_rows, previous_rows, concepts, excluded_codes=None):
    excluded_codes = excluded_codes or set()
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
                and row["code"] not in excluded_codes
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
            "industry": stock.get("industry"),
            "themes": stock.get("themes") or [],
            "today_open": today.get("today_open"),
            "gap_pct": today.get("gap_pct"),
            "today_pct": today.get("today_pct"),
            "close_state": today.get("close_state"),
            "continue_limit": today.get("continue_limit"),
            "suspended": bool(today.get("suspended")),
        })
    return {"board_count": max_board, "stocks": output}


def suspension_filter(candidates, yesterday_high):
    """最高板停牌时排除同行业或共享精确概念的一/二板候选。"""
    suspended = [
        stock for stock in (yesterday_high or {}).get("stocks", [])
        if stock.get("suspended")
    ]
    industries = list(dict.fromkeys(
        stock["industry"] for stock in suspended
        if stock.get("industry") and stock["industry"] not in ("-", "未获取")
    ))
    themes = list(dict.fromkeys(
        theme
        for stock in suspended
        for theme in stock.get("themes") or []
        if not theme.startswith("名称组:")
    ))
    excluded = []
    for candidate in candidates:
        same_industry = bool(
            candidate.get("industry")
            and candidate["industry"] not in ("-", "未获取")
            and candidate["industry"] in industries
        )
        matched_themes = [
            theme for theme in precise_themes(candidate, {})
            if theme in themes
        ]
        if same_industry or matched_themes:
            excluded.append({
                "code": candidate.get("code"),
                "name": candidate.get("name"),
                "matched_themes": matched_themes,
                "same_industry": same_industry,
            })
    return {
        "suspended_stocks": [
            {
                "code": stock.get("code"),
                "name": stock.get("name"),
                "board_count": stock.get("board_count"),
            }
            for stock in suspended
        ],
        "themes": themes,
        "industries": industries,
        "excluded_count": len(excluded),
        "excluded": excluded,
    }


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
        matched_rows = [
            performance[row["code"]]
            for row in previous_group
            if row["code"] in performance
        ]
        suspended = sum(bool(row.get("suspended")) for row in matched_rows)
        rows = [row for row in matched_rows if not row.get("suspended")]
        gaps = [row["gap_pct"] for row in rows if row.get("gap_pct") is not None]
        closes = [
            row["today_pct"] for row in rows
            if row.get("today_pct") is not None
        ]
        promoted = sum(bool(row.get("continue_limit")) for row in rows)
        high_open = sum(value > 0 for value in gaps)
        return {
            "mode": mode_name,
            "sample": len(previous_group) - suspended,
            "suspended": suspended,
            "quoted": len(gaps),
            "matched_performance": len(rows),
            "promoted": promoted,
            "promotion_rate_pct": (
                round(promoted * 100.0 / (len(previous_group) - suspended), 2)
                if len(previous_group) > suspended else None
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


def _rule_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_word_evidence(row, is_one_word):
    amount = _rule_number(row.get("amount_yi"))
    seal_fund = _rule_number(row.get("seal_fund_yi"))
    float_cap = _rule_number(row.get("float_cap_yi"))
    seal_ratio = (
        round(seal_fund * 100.0 / amount, 2)
        if seal_fund is not None and amount else None
    )
    seal_over_float = (
        round(seal_fund * 100.0 / float_cap, 2)
        if seal_fund is not None and float_cap else None
    )
    if amount is None:
        fill_probability = None
    elif amount >= 0.5 and seal_ratio is not None and seal_ratio <= 100:
        fill_probability = "高"
    elif amount >= 0.2 and seal_ratio is not None and seal_ratio <= 200:
        fill_probability = "中"
    else:
        fill_probability = "低"
    return {
        "one_word": bool(is_one_word),
        "board_amount_yi": amount,
        "seal_fund_yi": seal_fund,
        "seal_fund_ratio_pct": seal_ratio,
        "seal_fund_over_float_pct": seal_over_float,
        "fill_probability": fill_probability,
    }


def filter_one_word_candidates(candidates, one_word_codes):
    """按内置规则判断一字板是否进入候选；非一字板原样保留。"""
    kept = []
    excluded = []
    for source in candidates:
        row = dict(source)
        evidence = _one_word_evidence(row, one_word_codes.get(row.get("code")))
        row.update(evidence)
        if not evidence["one_word"]:
            kept.append(row)
            continue
        reason = None
        if not RULES["one_word_executable"]:
            reason = "当前口径下一字板不可执行"
        elif evidence["board_amount_yi"] is None:
            reason = "板上成交额未获取"
        elif evidence["board_amount_yi"] <= 0:
            reason = "板上成交额为0"
        elif evidence["seal_fund_yi"] is None:
            reason = "封单未获取"
        elif evidence["seal_fund_yi"] <= 0:
            reason = "封单为0或封单撤除"
        if reason:
            excluded.append({
                "code": row.get("code"),
                "name": row.get("name"),
                "reason": reason,
            })
        else:
            kept.append(row)
    return {
        "candidates": kept,
        "excluded_count": len(excluded),
        "excluded": excluded,
    }


def _leader_collapse(yesterday_high):
    for stock in (yesterday_high or {}).get("stocks") or []:
        if stock.get("suspended"):
            continue
        close_state = stock.get("close_state")
        today_pct = _rule_number(stock.get("today_pct"))
        if close_state in ("天地板", "跌停") or (
                today_pct is not None and today_pct <= -9.5):
            return True
        if (
            stock.get("today_open") is None
            and today_pct is None
            and close_state in (None, "-", "未获取")
        ):
            return True
    return False


def evaluate_market_gate(market, promotion_rate_pct, yesterday_open,
                         yesterday_high, previous_max_board,
                         promotion_sample_time=None):
    """用内置规则拆分市场宽度与最高板风险。"""
    market = market or {}
    zt_count = _rule_number(market.get("zt_count"))
    dt_count = _rule_number(market.get("dt_count"))
    zb_count = _rule_number(market.get("zb_count"))
    missing_counts = None in (zt_count, dt_count, zb_count)
    promotion = _rule_number(promotion_rate_pct)
    break_rate = (
        round(zb_count * 100.0 / (zt_count + zb_count), 2)
        if zb_count is not None and zt_count is not None and zt_count + zb_count
        else None
    )
    breadth_collapse = bool(
        zt_count is not None
        and dt_count is not None
        and dt_count >= 20
        and dt_count >= zt_count / 3.0
    )
    leader_collapse = _leader_collapse(yesterday_high)
    retreat = bool(
        previous_max_board is not None
        and market.get("max_board") is not None
        and market["max_board"] < previous_max_board
        and promotion is not None
        and promotion < 15
    )
    sample_clock = None
    if promotion_sample_time:
        try:
            sample_clock = datetime.strptime(
                str(promotion_sample_time), "%Y-%m-%d %H:%M:%S").time()
        except ValueError:
            sample_clock = None
    opening_unmatched = bool(
        sample_clock
        and sample_clock < datetime.strptime("09:25:00", "%H:%M:%S").time()
    )
    retreat_confirmed = retreat
    if retreat and sample_clock:
        gate_time = datetime.strptime(
            RULES["market_gate"]["retreat_time_gate"], "%H:%M").time()
        retreat_confirmed = sample_clock >= gate_time
    weak = bool(
        (promotion is not None and 15 <= promotion < 25)
        or (break_rate is not None and 35 <= break_rate <= 45)
    )
    opening = yesterday_open or {}
    up_open = _rule_number(opening.get("up_open")) or 0
    down_open = _rule_number(opening.get("down_open")) or 0
    collective_low_open = bool(
        down_open > up_open
        or ((_rule_number(opening.get("avg_gap_pct")) or 0) < 0)
    )

    if leader_collapse:
        state, action, max_picks = "崩塌", "observe_only", 0
    elif missing_counts:
        state, action, max_picks = "数据未获取", "no_conclusion", 0
    elif opening_unmatched:
        state, action, max_picks = "开盘未撮合", "no_conclusion", 0
    elif retreat and not retreat_confirmed:
        state, action, max_picks = "退潮待确认", "max_one", 1
    elif retreat:
        state, action, max_picks = "退潮", "no_trade", 0
    elif breadth_collapse and RULES["collapse_veto"]:
        state, action, max_picks = "崩塌", "no_trade", 0
    elif weak or breadth_collapse or collective_low_open:
        state, action, max_picks = "偏弱", "max_one", 1
    else:
        state, action, max_picks = "允许", "normal", None
    return {
        "state": state,
        "action": action,
        "breadth_collapse": breadth_collapse,
        "leader_collapse": leader_collapse,
        "retreat": retreat,
        "retreat_confirmed": retreat_confirmed,
        "opening_unmatched": opening_unmatched,
        "weak": weak,
        "collective_low_open": collective_low_open,
        "break_rate_pct": break_rate,
        "promotion_rate_pct": promotion,
        "promotion_sample_time": promotion_sample_time,
        "veto": action in ("no_trade", "observe_only", "no_conclusion"),
        "max_picks": max_picks,
        "weak_market_gate": copy.deepcopy(RULES["weak_market_gate"]),
    }


def minimum_candidate_score(theme_score, mode, market_gate):
    """返回当前市场和题材强度下的候选结构分门槛。"""
    mode_gate = {
        "one_to_two": 80,
        "two_to_three": 79,
    }
    if mode not in mode_gate:
        raise ValueError("未知候选模式：{}".format(mode))
    if market_gate["action"] in ("no_trade", "observe_only", "no_conclusion"):
        return None
    if market_gate["state"] == "退潮待确认":
        return 85
    if market_gate["state"] != "偏弱":
        return mode_gate[mode]
    weak_gate = RULES["weak_market_gate"]
    override = weak_gate["override"]
    if theme_score >= override["theme_score_threshold"]:
        return override["min_score_by_mode"][mode]
    return weak_gate["min_score"]


def build_payload(stock_query=None):
    snapshot = datetime.now(CN_TZ)
    stock = resolve_stock_query(stock_query) if stock_query else None
    if stock and not is_mainboard_code(stock["code"]):
        raise RuntimeError("错误：单股审视仅支持沪深主板：{}".format(stock["code"]))
    current = run_json("fetch_limit_up.py", "--current-only", "--json")
    data_date = "{}-{}-{}".format(
        current["base_date"][:4], current["base_date"][4:6], current["base_date"][6:])
    previous_date = "{}-{}-{}".format(
        current["prev_date"][:4], current["prev_date"][4:6], current["prev_date"][6:])

    current_rows = current.get("zt_rows") or []
    previous_path = build_limit_pool_path(previous_date)
    previous_pool = require_limit_pool(previous_path, previous_date)
    previous_generated = False
    previous_rows = previous_pool["stocks"]
    previous_source = "涨停池报告：{}".format(
        previous_pool.get("source_name") or previous_pool.get("source") or "未获取")

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

    yesterday_high = find_yesterday_high(previous_rows, current)
    one_two_candidates = [
        row for row in current_rows
        if row.get("board_count") in (1, 2)
    ]
    permitted_candidates = select_candidates(current_rows)
    board_permission_excluded = len(one_two_candidates) - len(permitted_candidates)
    suspension = suspension_filter(permitted_candidates, yesterday_high)
    suspended_excluded_codes = {
        row["code"] for row in suspension["excluded"]
    }
    permitted_after_suspension = [
        row for row in permitted_candidates
        if row["code"] not in suspended_excluded_codes
    ]
    one_word_filter = filter_one_word_candidates(
        permitted_after_suspension,
        current.get("one_word") or {},
    )
    candidates = one_word_filter["candidates"]
    one_word_audit = {
        key: value for key, value in one_word_filter.items()
        if key != "candidates"
    }
    one_word_excluded_codes = {
        row["code"] for row in one_word_filter["excluded"]
    }
    excluded_codes = suspended_excluded_codes | one_word_excluded_codes
    themes = theme_summary(
        current_rows, previous_rows, concepts, excluded_codes)
    feedback = mode_feedback(previous_rows, current)
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

    stock_review = None
    if stock:
        code = stock["code"]
        quote_payload = run_json("fetch_quote.py", code, "--days", "120", "--json")
        target_metric = metrics.get(code)
        if target_metric is None:
            target_metrics = run_json(
                "fetch_stock_metrics.py",
                "--codes", code,
                "--days", "120",
                "--workers", "1",
                "--json",
            )
            target_metric = next(
                (row for row in target_metrics.get("rows", [])
                 if row.get("code") == code),
                None,
            )
        try:
            structure = run_json(
                "fetch_stock_structure.py", code, "--days", "120", "--json")
        except Exception as exc:  # noqa: BLE001 单股筹码失败仍保留行情事实
            structure = {
                "holders": [],
                "chips": None,
                "concepts": [],
                "limitations": ["筹码与题材未获取：{}".format(exc)],
            }
        stock_concepts = {code: structure.get("concepts") or []}
        stock_review = build_stock_review(
            snapshot=snapshot,
            data_date=data_date,
            query=stock["query"],
            stock={"code": code, "name": stock.get("name") or structure.get("name")},
            current=current,
            previous_rows=previous_rows,
            concepts=stock_concepts,
            quote_payload=quote_payload,
            metrics=target_metric or {},
            structure=structure,
        )
        if structure.get("name"):
            stock_review["name"] = structure["name"]

    quality_summary = collections.Counter(
        (row.get("metrics") or {}).get("quality") or "unavailable"
        for row in candidates
    )
    limitations = [
        "本脚本只提供数据，不做题材取舍，不输出可打板、第二梯队或持有结论",
        "运行时只读取预先保存的昨日涨停池，不自动拉取、生成或覆盖涨停池文件",
        "题材来自当前 F10 精确概念，历史归属可能有变动",
        "筹码为日K+换手率推演值",
    ]
    if quality_summary["degraded"]:
        limitations.append(
            "{} 只候选的换手率与筹码使用近似值，筹码结构只能作为降级证据".format(
                quality_summary["degraded"])
        )

    performance = current.get("yesterday_performance") or {}
    eligible_count = performance.get("eligible_count")
    promotion_rate = (
        round(performance["promote_count"] * 100.0 / eligible_count, 2)
        if eligible_count and performance.get("promote_count") is not None
        else None
    )
    market_gate = evaluate_market_gate(
        market=current.get("overview"),
        promotion_rate_pct=promotion_rate,
        yesterday_open=(current.get("yesterday_open_close") or {}).get("overall"),
        yesterday_high=yesterday_high,
        previous_max_board=(
            current.get("prev_max_board")
            or (previous_pool.get("summary") or {}).get("max_board")
        ),
        promotion_sample_time=current.get("quote_time"),
    )

    return {
        "generated_at": snapshot.isoformat(timespec="seconds"),
        "data_date": data_date,
        "report_path": str(
            build_stock_report_path(data_date, stock["code"])
            if stock else build_report_path(data_date)
        ),
        "previous_date": previous_date,
        "quote_date": current.get("quote_date"),
        "quote_time": current.get("quote_time"),
        "rules": copy.deepcopy(RULES),
        "market_gate": market_gate,
        "board_permission": BOARD_PERMISSION,
        "board_permission_excluded_count": board_permission_excluded,
        "suspension_filter": suspension,
        "one_word_filter": one_word_audit,
        "previous_source": previous_source,
        "previous_pool_report": str(previous_path),
        "previous_pool_generated": previous_generated,
        "previous_context": previous_pool.get("summary"),
        "current_pool_report": None,
        "current_pool_generated": False,
        "market": current.get("overview"),
        "yesterday_open": (current.get("yesterday_open_close") or {}).get("overall"),
        "yesterday_highest_board": yesterday_high,
        "mode_feedback": feedback,
        "metric_quality_summary": dict(sorted(quality_summary.items())),
        "themes": themes,
        "candidates": candidates,
        "stock_review": stock_review,
        "limitations": limitations,
    }


def build_compact_payload(payload, theme_limit=30):
    """把完整预计算压缩为模型决策所需字段，保留原始 --json 供审计。"""
    def number(value, digits=2):
        value = to_float(value)
        return None if value is None else round(value, digits)

    review = payload.get("stock_review") or {}
    all_themes = payload.get("themes") or []
    if review:
        review_themes = set(review.get("themes") or [])
        all_themes = [
            row for row in all_themes if row.get("theme") in review_themes
        ]
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
    source_candidates = payload.get("candidates") or []
    if review:
        source_candidates = [
            row for row in source_candidates if row.get("code") == review.get("code")
        ]
    for row in source_candidates:
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
            "one_word": row.get("one_word"),
            "board_amount_yi": number(row.get("board_amount_yi")),
            "seal_fund_over_float_pct": number(
                row.get("seal_fund_over_float_pct")),
            "fill_probability": row.get("fill_probability"),
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
    compact_review = None
    if review:
        metric = review.get("metrics") or {}
        returns = metric.get("returns_pct") or {}
        ma = metric.get("ma") or {}
        chips = review.get("structure", {}).get("chips") or metric.get("chips") or {}
        quote = review.get("quote") or {}
        intraday = review.get("intraday") or {}
        compact_review = {
            "query": review.get("query"),
            "code": review.get("code"),
            "name": review.get("name"),
            "themes": review.get("themes") or [],
            "quote_date": review.get("quote_date"),
            "quote_consistent": review.get("quote_consistent"),
            "current_limit_status": review.get("current_limit_status"),
            "in_current_limit_pool": review.get("in_current_limit_pool"),
            "current_board_count": review.get("current_board_count"),
            "in_previous_limit_pool": review.get("in_previous_limit_pool"),
            "previous_board_count": review.get("previous_board_count"),
            "yesterday_performance": review.get("yesterday_performance"),
            "quote": {
                "price": number(quote.get("price")),
                "prev_close": number(quote.get("prev_close")),
                "open": number(quote.get("open")),
                "high": number(quote.get("high")),
                "low": number(quote.get("low")),
                "change_pct": number(quote.get("change_pct")),
                "volume_hands": number(quote.get("volume_hands")),
            },
            "intraday": {
                "is_partial_day": intraday.get("is_partial_day"),
                "price_vs_open_pct": number(intraday.get("price_vs_open_pct")),
                "price_from_high_pct": number(intraday.get("price_from_high_pct")),
                "open_gap_pct": number(intraday.get("open_gap_pct")),
                "low_gap_pct": number(intraday.get("low_gap_pct")),
                "volume_ratio_5d": number(intraday.get("volume_ratio_5d")),
                "volume_ratio_scope": intraday.get("volume_ratio_scope"),
                "volume_vs_prev_day": number(intraday.get("volume_vs_prev_day")),
                "turnover_pct": number(intraday.get("turnover_pct")),
                "amount_yuan": number(intraday.get("amount_yuan")),
            },
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
            "holders": review.get("structure", {}).get("holders") or [],
            "metric_source": metric.get("source"),
            "metric_quality": metric.get("quality"),
            "limitations": review.get("limitations") or [],
        }
    metric_quality_summary = payload.get("metric_quality_summary")
    if compact_review:
        quality = compact_review.get("metric_quality")
        metric_quality_summary = {quality: 1} if quality else {}
    previous_context = payload.get("previous_context") or {}
    compact_previous_context = {
        "count": previous_context.get("count"),
        "max_board": previous_context.get("max_board"),
        "max_board_stocks": previous_context.get("max_board_stocks"),
        "board_counts": previous_context.get("board_counts"),
        "top_themes": (previous_context.get("themes") or [])[:10],
    }
    return {
        "schema": (
            "limit-hit.stock-review.compact.v1"
            if compact_review else "limit-hit.candidates.compact.v1"
        ),
        "generated_at": payload.get("generated_at"),
        "data_date": payload.get("data_date"),
        "quote_date": payload.get("quote_date"),
        "quote_time": payload.get("quote_time"),
        "rules": payload.get("rules"),
        "market_gate": payload.get("market_gate"),
        "previous_date": payload.get("previous_date"),
        "board_permission": payload.get("board_permission"),
        "board_permission_excluded_count": payload.get(
            "board_permission_excluded_count"),
        "suspension_filter": payload.get("suspension_filter"),
        "one_word_filter": payload.get("one_word_filter"),
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
        "metric_quality_summary": metric_quality_summary,
        "theme_total_count": len(all_themes),
        "themes_truncated": (
            len(themes) < len(all_themes) if not compact_review else False
        ),
        "themes": themes,
        "candidates": candidates,
        "stock_review": compact_review,
        "limitations": payload.get("limitations") or [],
        "note": (
            "单股事实已扁平化；报告结论由模型按 SKILL 口径判断"
            if compact_review else
            "候选指标已扁平化和四舍五入；完整原始字段使用 --json"
        ),
    }


def print_text(payload):
    review = payload.get("stock_review")
    if review:
        quote = review.get("quote") or {}
        intraday = review.get("intraday") or {}
        metric = review.get("metrics") or {}
        returns = metric.get("returns_pct") or {}
        ma = metric.get("ma") or {}
        chips = (review.get("structure") or {}).get("chips") or {}
        print("=" * 100)
        print("limit-hit 单股审视 · {}（{}） · 行情 {} · 快照 {}".format(
            review.get("name"), review.get("code"),
            review.get("quote_date") or "-", payload.get("generated_at") or "-"))
        print("报告文件：{}".format(payload.get("report_path") or "-"))
        print("行情日期一致：{}".format(
            "是" if review.get("quote_consistent") else "否"))
        print("当日状态：{} · 当前板数 {} · 昨日板数 {}".format(
            review.get("current_limit_status"),
            review.get("current_board_count") if review.get("current_board_count") is not None else "-",
            review.get("previous_board_count") if review.get("previous_board_count") is not None else "-"))
        print("现价 {} · 昨收 {} · 今开 {} · 最高 {} · 最低 {} · 涨跌幅 {}%".format(
            quote.get("price"), quote.get("prev_close"), quote.get("open"),
            quote.get("high"), quote.get("low"), quote.get("change_pct")))
        print("量能：盘中部分量 {} · 相对昨量 {} · 5日量比 {}（{}） · 换手 {}%".format(
            "是" if intraday.get("is_partial_day") else "否",
            intraday.get("volume_vs_prev_day"), intraday.get("volume_ratio_5d"),
            intraday.get("volume_ratio_scope"), intraday.get("turnover_pct")))
        print("价格位置：相对开盘 {}% · 距最高 {}% · 开盘差 {}% · 最低差 {}%".format(
            intraday.get("price_vs_open_pct"), intraday.get("price_from_high_pct"),
            intraday.get("open_gap_pct"), intraday.get("low_gap_pct")))
        print("趋势：5日 {}% / 10日 {}% / 20日 {}% · MA5 {} / MA10 {} / MA20 {}".format(
            returns.get("5d"), returns.get("10d"), returns.get("20d"),
            ma.get("5"), ma.get("10"), ma.get("20")))
        print("筹码：获利盘 {}% · 平均成本 {} · 90%集中度 {}% · 高于均价 {}% · 质量 {}".format(
            chips.get("profit_ratio_pct"), chips.get("avg_cost"),
            chips.get("concentration90_pct"), chips.get("above_avg_pct"),
            metric.get("quality") or "未获取"))
        print("题材：{}".format("、".join(review.get("themes") or []) or "未获取"))
        print("=" * 100)
        for item in review.get("limitations") or []:
            print("注：{}".format(item))
        return

    print("=" * 112)
    print("limit-hit 候选上下文 · {} · 行情 {} {} · 快照 {}".format(
        payload["data_date"], payload.get("quote_date") or "-",
        payload.get("quote_time") or "-",
        payload.get("generated_at") or "-"))
    print("报告文件：{}".format(payload.get("report_path") or "-"))
    print("板块权限：{}（权限外 1/2 板排除 {} 只）".format(
        payload.get("board_permission"),
        payload.get("board_permission_excluded_count")))
    rules = payload.get("rules") or {}
    gate = payload.get("market_gate") or {}
    print("口径：内置规则 · 市场门槛 {} / {} · 最多 {} 只".format(
        gate.get("state"), gate.get("action"),
        "-" if gate.get("max_picks") is None else gate.get("max_picks")))
    one_word_filter = payload.get("one_word_filter") or {}
    print("一字板过滤：排除 {} 只".format(
        one_word_filter.get("excluded_count", 0)))
    suspension = payload.get("suspension_filter") or {}
    print("停牌最高板：{}（同行业/精确概念排除 {} 只）".format(
        "、".join(
            "{}（{}）".format(row["name"], row["code"])
            for row in suspension.get("suspended_stocks") or []
        ) or "无",
        suspension.get("excluded_count", 0)))
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
    if opening.get("status") == "未撮合":
        print("昨日涨停今日开盘：未撮合（本节不适用）")
    else:
        print("昨日涨停今日开盘：{} / 高开 {} / 平开 {} / 低开 {} / 平均 {}%".format(
            opening.get("status") or "未获取",
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
    ap.add_argument("--stock", help="单股名称或代码；输出持仓审视事实，不要求当日涨停")
    output = ap.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="输出完整 JSON，供审计和调试")
    output.add_argument("--compact", action="store_true", help="输出模型友好的单行紧凑 JSON")
    args = ap.parse_args()
    payload = build_payload(stock_query=args.stock)
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
        message = str(exc)
        sys.exit(message if message.startswith("错误：") else "错误：{}".format(message))
