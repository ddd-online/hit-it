#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于历史涨停池和日 K 的板块第二梯队结构代理回测。"""

import argparse
import collections
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


BACKTEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKTEST_DIR.parents[1]
sys.path.insert(0, str(BACKTEST_DIR))
sys.path.insert(0, str(REPO_ROOT / "limit-hit" / "scripts"))

import backtest_limit_hit as backtest  # noqa: E402
import fallback_sources  # noqa: E402
import fetch_sector_boards as sector_boards  # noqa: E402
import fetch_sector_leaders as sector_leaders  # noqa: E402
import fetch_stock_structure  # noqa: E402
import http_util  # noqa: E402


ALLOWED_TIMES = ("09:40", "10:00", "10:30", "11:00")
TEMPLATE_PATH = BACKTEST_DIR / "sector-second-line-template.md"


def parse_times(raw):
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values:
        raise argparse.ArgumentTypeError("至少指定一个执行时点")
    parsed = []
    for value in values:
        try:
            clock = datetime.strptime(value, "%H:%M").strftime("%H:%M:%S")
        except ValueError:
            raise argparse.ArgumentTypeError("时间格式应为 HH:MM")
        if value not in ALLOWED_TIMES:
            raise argparse.ArgumentTypeError(
                "只支持 09:40,10:00,10:30,11:00")
        parsed.append(clock)
    return tuple(parsed)


def visible_limit_rows(rows, cutoff):
    return [
        row for row in rows
        if row.get("first_seal") and str(row["first_seal"]) <= cutoff
    ]


def theme_ladder(current_rows, previous_rows, min_score, max_themes):
    current_by_theme = collections.defaultdict(list)
    previous_by_theme = collections.defaultdict(list)
    for row in current_rows:
        for theme in backtest.reason_tags(row):
            current_by_theme[theme].append(row)
    for row in previous_rows:
        for theme in backtest.reason_tags(row):
            previous_by_theme[theme].append(row)

    themes = []
    for theme, rows in current_by_theme.items():
        previous = previous_by_theme.get(theme, [])
        promoted = len(
            {row["code"] for row in rows}
            & {row["code"] for row in previous}
        )
        if len(rows) < 2 and not promoted:
            continue
        max_board = max((row.get("board_count") or 0 for row in rows), default=0)
        score = backtest.theme_score(len(rows), promoted, max_board)
        if score >= min_score:
            themes.append({
                "theme": theme,
                "theme_score": score,
                "current_count": len(rows),
                "promoted_count": promoted,
                "max_board": max_board,
            })

    themes.sort(
        key=lambda item: (
            -item["theme_score"],
            -item["current_count"],
            -item["max_board"],
            item["theme"],
        )
    )
    return themes[:max_themes]


def build_board_index(rows):
    return {
        str(row.get("name") or "").strip(): row["code"]
        for row in rows
        if str(row.get("name") or "").strip() and row.get("code")
    }


def normalize_members(raw_members):
    rows = []
    seen = set()
    for item in raw_members:
        raw_code = str(
            item.get("code") or item.get("SECURITY_CODE") or "")
        code = (
            raw_code
            if raw_code.startswith(("sh", "sz", "bj"))
            else fallback_sources.infer_sec_code(raw_code)
        )
        name = str(
            item.get("name")
            or item.get("SECURITY_NAME_ABBR")
            or item.get("SECURITY_NAME")
            or ""
        )
        if not code or code in seen:
            continue
        if not backtest.is_mainboard(code) or backtest.is_st(name):
            continue
        seen.add(code)
        rows.append({
            "code": code,
            "name": name,
            "float_cap_yi": item.get("float_cap_yi"),
        })
    return rows


def member_rows(bk_code, fetch_members=None):
    fetch_members = (
        fetch_members or sector_leaders.fetch_f10_members_quotes)
    _board_name, raw_members = fetch_members(bk_code)
    return normalize_members(raw_members)


def dedupe_candidates(candidates):
    best = {}
    for row in candidates:
        current = best.get(row["code"])
        if current is None or row["theme_score"] > current["theme_score"]:
            best[row["code"]] = row
    return sorted(
        best.values(),
        key=lambda row: (-row["theme_score"], row["code"]),
    )


def pct_change(now, before):
    if now is None or not before:
        return None
    return round((now / before - 1.0) * 100.0, 4)


def mean(values):
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def bar_features(bars, data_date):
    eligible = [
        bar for bar in bars
        if bar.get("date") and bar["date"] <= data_date
    ]
    if len(eligible) < 21 or eligible[-1]["date"] != data_date:
        return None
    day = eligible[-1]
    previous = eligible[-2]
    previous_window = eligible[:-1]
    closes = [
        bar.get("close") for bar in previous_window[-20:]
        if bar.get("close") is not None
    ]
    if len(closes) < 20:
        return None
    volumes = [
        bar.get("volume_hands") if bar.get("volume_hands") is not None
        else bar.get("volume")
        for bar in previous_window[-20:]
    ]
    previous_close = previous.get("close")
    if previous_close is None:
        return None
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    range20_low = min(bar["low"] for bar in previous_window[-20:])
    range20_high = max(bar["high"] for bar in previous_window[-20:])
    prior_volume_average = mean(volumes[-6:-1])
    return {
        "data_date": data_date,
        "previous_close": previous_close,
        "open": day.get("open"),
        "high": day.get("high"),
        "low": day.get("low"),
        "close": day.get("close"),
        "limit_price": round(previous_close * 1.10, 2),
        "open_gap_pct": pct_change(day.get("open"), previous_close),
        "previous_ma5": round(ma5, 4),
        "previous_ma10": round(ma10, 4),
        "previous_return5_pct": pct_change(
            previous_close,
            previous_window[-6]["close"] if len(previous_window) >= 6 else None,
        ),
        "previous_position20_pct": round(
            (previous_close - range20_low)
            / (range20_high - range20_low) * 100.0,
            2,
        ) if range20_high > range20_low else None,
        "previous_volume_ratio5": round(
            volumes[-1] / prior_volume_average, 3
        ) if len(volumes) >= 6 and prior_volume_average else None,
    }


def second_line_eligible(feature):
    if not feature:
        return False
    required = (
        feature.get("open"),
        feature.get("previous_close"),
        feature.get("previous_ma5"),
        feature.get("previous_ma10"),
        feature.get("open_gap_pct"),
        feature.get("previous_return5_pct"),
        feature.get("previous_position20_pct"),
    )
    if any(value is None for value in required):
        return False
    return bool(
        feature["open"] < feature["limit_price"] - 0.001
        and -3.0 <= feature["open_gap_pct"] <= 5.0
        and feature["previous_close"] >= feature["previous_ma5"]
        and feature["previous_close"] >= feature["previous_ma10"]
        and -3.0 <= feature["previous_return5_pct"] <= 15.0
        and 20.0 <= feature["previous_position20_pct"] <= 85.0
    )


def candidate_outcome(feature, next_bar):
    close = feature.get("close")
    next_open = (next_bar or {}).get("open")
    return {
        "sealed_close": bool(
            close is not None
            and close >= feature["limit_price"] * 0.999
        ),
        "next_open": next_open,
        "next_open_premium_pct": pct_change(next_open, close),
    }


def can_fill_second_line(feature):
    open_price = feature.get("open")
    return bool(
        open_price is not None
        and open_price < feature["limit_price"] - 0.001
    )


def run_group(signals, initial_capital, fees, signal_days=None):
    capital = initial_capital
    trades = []
    seen_dates = set()
    for signal in sorted(signals, key=lambda row: row["date"]):
        if signal["date"] in seen_dates:
            continue
        trade = backtest.simulate_trade(
            signal,
            signal["entry_price"],
            signal["exit_price"],
            capital,
            fees,
        )
        if trade is None:
            continue
        seen_dates.add(signal["date"])
        capital = trade["capital_after"]
        trades.append({
            "signal_date": signal["date"],
            "exit_date": signal.get("exit_date"),
            "theme": signal.get("theme"),
            "code": signal.get("code"),
            "name": signal.get("name"),
            **trade,
            **signal.get("outcome", {}),
        })
    summary = backtest.summarize_trades(
        trades,
        initial_capital,
        signal_days if signal_days is not None else len(trades),
    )
    premiums = [
        trade["next_open_premium_pct"]
        for trade in trades
        if trade.get("next_open_premium_pct") is not None
    ]
    summary.update({
        "sealed_count": sum(bool(trade.get("sealed_close")) for trade in trades),
        "seal_rate_pct": round(
            sum(bool(trade.get("sealed_close")) for trade in trades)
            * 100.0 / len(trades),
            2,
        ) if trades else None,
        "next_open_premium_sample": len(premiums),
        "positive_next_open_rate_pct": round(
            sum(value > 0 for value in premiums) * 100.0 / len(premiums),
            2,
        ) if premiums else None,
        "avg_next_open_premium_pct": round(
            sum(premiums) / len(premiums), 4
        ) if premiums else None,
    })
    return {"summary": summary, "trades": trades}


def bars_at(bars, data_date):
    for index, bar in enumerate(bars):
        if bar.get("date") == data_date:
            next_bar = bars[index + 1] if index + 1 < len(bars) else None
            return bar, next_bar
    return None, None


def preload_bars(codes, bars_by_code, load_bars, workers=1):
    missing = list(dict.fromkeys(
        code for code in codes if code not in bars_by_code
    ))
    errors = []
    if workers <= 1:
        for code in missing:
            try:
                bars_by_code[code] = load_bars(code)
            except Exception as exc:  # noqa: BLE001 单票失败不阻断整批
                bars_by_code[code] = []
                errors.append({"code": code, "error": str(exc)})
        return bars_by_code, errors

    with ThreadPoolExecutor(max_workers=min(workers, 16)) as executor:
        futures = {
            executor.submit(load_bars, code): code
            for code in missing
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                bars_by_code[code] = future.result()
            except Exception as exc:  # noqa: BLE001 单票失败不阻断整批
                bars_by_code[code] = []
                errors.append({"code": code, "error": str(exc)})
    return bars_by_code, errors


def signal_from_candidate(candidate, bars, group):
    day, next_bar = bars_at(bars, candidate["data_date"])
    if (
            not day
            or not next_bar
            or day.get("close") is None
            or next_bar.get("open") is None):
        return None
    entry_price = day["close"] if group == "limit" else day["open"]
    if entry_price is None:
        return None
    return {
        "date": candidate["data_date"],
        "exit_date": next_bar["date"],
        "theme": candidate["theme"],
        "theme_score": candidate["theme_score"],
        "code": candidate["code"],
        "name": candidate["name"],
        "group": group,
        "entry_price": entry_price,
        "exit_price": next_bar["open"],
        "outcome": candidate_outcome(candidate, next_bar),
    }


def select_candidates_for_day(
        time, data_date, themes, current_rows, board_index,
        bars_by_code, load_bars, load_members, members_cache,
        min_candidate_score=60, workers=1):
    signals = []
    skipped = []
    visible_codes = {row["code"] for row in current_rows}

    for theme_item in themes:
        theme = theme_item["theme"]
        limit_rows = [
            row for row in current_rows
            if theme in backtest.reason_tags(row)
            and backtest.candidate_eligible(row)
            and backtest.candidate_score(row) >= min_candidate_score
            and backtest.entry_visible(
                row, time, require_reseal_after=True)
        ]
        if limit_rows:
            selected = max(
                limit_rows,
                key=lambda row: (
                    backtest.candidate_score(row),
                    row.get("board_count") or 0,
                ),
            )
            preload_bars(
                [selected["code"]], bars_by_code, load_bars, workers)
            bars = bars_by_code.get(selected["code"], [])
            feature = bar_features(bars, data_date)
            if feature:
                feature.update({
                    "theme": theme,
                    "theme_score": theme_item["theme_score"],
                    "code": selected["code"],
                    "name": selected.get("name"),
                    "limit_price": feature["close"],
                })
                signal = signal_from_candidate(feature, bars, "limit")
                if signal:
                    signals.append(signal)
                else:
                    skipped.append({
                        "date": data_date,
                        "group": "limit",
                        "code": selected["code"],
                        "reason": "D+1 开盘缺失",
                    })

        board_code = board_index.get(theme)
        if not board_code:
            skipped.append({
                "date": data_date,
                "group": "second_line",
                "theme": theme,
                "reason": "未找到同名精确概念板块",
            })
            continue

        members = members_cache.get(board_code)
        if members is None:
            members = load_members(board_code)
            members_cache[board_code] = members

        preload_bars(
            [member["code"] for member in members],
            bars_by_code,
            load_bars,
            workers,
        )

        candidates = []
        for member in members:
            if member["code"] in visible_codes:
                continue
            bars = bars_by_code.get(member["code"], [])
            feature = bar_features(bars, data_date)
            if not second_line_eligible(feature):
                continue
            if not can_fill_second_line(feature):
                continue
            candidates.append({
                **feature,
                "code": member["code"],
                "name": member.get("name"),
                "theme": theme,
                "theme_score": theme_item["theme_score"],
                "float_cap_yi": member.get("float_cap_yi"),
            })
        if candidates:
            selected = max(
                candidates,
                key=lambda row: (
                    row["theme_score"],
                    -abs(row["open_gap_pct"] - 3.0),
                    row.get("previous_volume_ratio5") or 0.0,
                    -(row.get("float_cap_yi") or float("inf")),
                ),
            )
            bars = bars_by_code[selected["code"]]
            signal = signal_from_candidate(selected, bars, "second_line")
            if signal:
                signals.append(signal)
            else:
                skipped.append({
                    "date": data_date,
                    "group": "second_line",
                    "code": selected["code"],
                    "reason": "D+1 开盘缺失",
                })

    return dedupe_candidates(signals), skipped


def report_paths(workspace, start, end, times):
    report_dir = workspace / "report" / "回测"
    paths = {
        time: report_dir / "板块第二梯队_{}_{}_{}.md".format(
            time[:5].replace(":", "-"), start, end)
        for time in times
    }
    paths["comparison"] = (
        report_dir / "板块第二梯队_时点对比_{}_{}.md".format(start, end)
    )
    return paths


def render_rules(result):
    return "\n".join([
        "- D 日涨停后买入，D+1 开盘退出。",
        "- 打板组按 D 日涨停价成交，且执行时点后必须开板回封。",
        "- 第二梯队组按 D 日开盘价成交，候选只使用 D-1 及以前结构和 D 日开盘。",
        "- 两组各以 {} 元独立计算。".format(
            backtest.money(result["capital"])),
    ])


def render_summary(group):
    summary = group["summary"]
    return "\n".join([
        "| 指标 | 结果 |",
        "|---|---:|",
        "| 成交数 | {} |".format(summary["trade_count"]),
        "| 方向胜率 | {} |".format(
            backtest.rate(summary["direction_win_rate_pct"])),
        "| 净收益胜率 | {} |".format(
            backtest.rate(summary["net_win_rate_pct"])),
        "| 当日封板率 | {} |".format(
            backtest.rate(summary.get("seal_rate_pct"))),
        "| 次日正溢价率 | {} |".format(
            backtest.rate(summary.get("positive_next_open_rate_pct"))),
        "| 平均次日溢价 | {} |".format(
            backtest.pct(summary.get("avg_next_open_premium_pct"))),
        "| 期末本金 | {} 元 |".format(
            backtest.money(summary["ending_capital"])),
        "| 净收益率 | {} |".format(
            backtest.pct(summary["net_return_pct"])),
        "| 最大回撤 | {:.2f}% |".format(
            summary["max_drawdown_pct"]),
    ])


def render_trades(trades):
    lines = [
        "| 信号日 | 退出日 | 题材 | 代码 | 名称 | 入场 | 退出 | 当日封板 | 次日溢价 | 净收益 | 期末本金 |",
        "|---|---|---|---|---|---:|---:|---|---:|---:|---:|",
    ]
    for trade in trades:
        lines.append(
            "| {signal_date} | {exit_date} | {theme} | `{code}` | {name} | "
            "{entry_price:.2f} | {exit_price:.2f} | {sealed} | {premium} | "
            "{net_pnl:+.2f}元 | {capital_after:,.2f}元 |".format(
                sealed="是" if trade.get("sealed_close") else "否",
                premium=backtest.pct(
                    trade.get("next_open_premium_pct")),
                **trade,
            )
        )
    if not trades:
        lines.append("| - | - | - | - | 无成交 | - | - | - | - | - | - |")
    return "\n".join(lines)


def render_skipped(skipped):
    grouped = collections.Counter(
        (row.get("group") or "-", row["reason"]) for row in skipped
    )
    lines = [
        "| 分组 | 原因 | 次数 |",
        "|---|---|---:|",
    ]
    for (group, reason), count in sorted(grouped.items()):
        lines.append("| {} | {} | {} |".format(group, reason, count))
    if not skipped:
        lines.append("| - | 无 | 0 |")
    return "\n".join(lines)


def render_time_report(result):
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "time": result["time"],
        "period": "{} 至 {}".format(result["start"], result["end"]),
        "initial_capital": backtest.money(result["capital"]),
        "rules": render_rules(result),
        "limit_summary": render_summary(result["groups"]["limit"]),
        "limit_trades": render_trades(result["groups"]["limit"]["trades"]),
        "second_line_summary": render_summary(
            result["groups"]["second_line"]),
        "second_line_trades": render_trades(
            result["groups"]["second_line"]["trades"]),
        "skipped": render_skipped(result.get("skipped", [])),
        "limitations": "\n".join(
            "- " + item for item in result["limitations"]),
    }
    for key, value in replacements.items():
        text = text.replace("{{%s}}" % key, value)
    return text


def render_comparison(results):
    lines = [
        "# 板块第二梯队时点对比",
        "",
        "| 时点 | 打板成交 | 打板期末 | 第二梯队成交 | 第二梯队期末 |",
        "|---|---:|---:|---:|---:|",
    ]
    for result in results:
        limit = result["groups"]["limit"]["summary"]
        second = result["groups"]["second_line"]["summary"]
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                result["time"],
                limit["trade_count"],
                backtest.money(limit["ending_capital"]),
                second["trade_count"],
                backtest.money(second["ending_capital"]),
            )
        )
    lines.extend(["", "不构成投资建议"])
    return "\n".join(lines)


def default_board_catalog():
    args = SimpleNamespace(
        type="concept",
        source="eastmoney",
        sort="change",
        top=10000,
        min_up=0,
        search=[],
    )
    _source, rows, _meta = sector_boards.prepare_rows(args)
    return rows


def default_load_bars(code):
    bars, _source, _limitations = fetch_stock_structure.fetch_kline(
        code, days=400, cutoff=None)
    return bars


def run_time_point(
        time,
        pools,
        dates,
        board_index,
        capital,
        fees,
        bars_by_code=None,
        members_cache=None,
        load_bars=None,
        load_members=None,
        min_theme_score=50,
        min_candidate_score=60,
        workers=1):
    bars_by_code = bars_by_code if bars_by_code is not None else {}
    members_cache = members_cache if members_cache is not None else {}
    load_bars = load_bars or default_load_bars
    load_members = load_members or member_rows
    selected = []
    skipped = []

    for index, data_date in enumerate(dates):
        current = visible_limit_rows(pools.get(data_date, []), time)
        previous = pools.get(dates[index - 1], []) if index else []
        themes = theme_ladder(
            current,
            previous,
            min_score=min_theme_score,
            max_themes=3,
        )
        day_signals, day_skipped = select_candidates_for_day(
            time,
            data_date,
            themes,
            current,
            board_index,
            bars_by_code,
            load_bars,
            load_members,
            members_cache,
            min_candidate_score=min_candidate_score,
            workers=workers,
        )
        selected.extend(day_signals)
        skipped.extend(day_skipped)

    limit_signals = [row for row in selected if row["group"] == "limit"]
    second_signals = [
        row for row in selected if row["group"] == "second_line"
    ]
    return {
        "time": time,
        "groups": {
            "limit": run_group(
                limit_signals,
                capital,
                fees,
                signal_days=len({row["date"] for row in limit_signals}),
            ),
            "second_line": run_group(
                second_signals,
                capital,
                fees,
                signal_days=len({row["date"] for row in second_signals}),
            ),
        },
        "skipped": skipped,
    }


def build_arg_parser():
    parser = argparse.ArgumentParser(description="板块第二梯队结构代理回测")
    parser.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=10000.0,
                        help="每组初始本金，默认 10000")
    parser.add_argument(
        "--times",
        type=parse_times,
        default=("09:40:00", "10:00:00", "10:30:00", "11:00:00"),
        help="执行时点，支持 09:40,10:00,10:30,11:00",
    )
    parser.add_argument("--workers", type=int, default=4,
                        help="历史涨停池并发数，默认 4")
    return parser


def main():
    args = build_arg_parser().parse_args()
    if args.capital <= 0:
        raise SystemExit("错误：--capital 必须大于 0")

    start_date = backtest.parse_date(args.start)
    end_date = backtest.parse_date(args.end)
    dates, warmup_dates, _index_bars = backtest.get_trading_dates(
        start_date, end_date)
    if not dates:
        raise RuntimeError("区间内没有交易日")

    all_dates = warmup_dates + dates
    pools, errors = backtest.load_pools(all_dates, args.workers)
    backtest.apply_consecutive_board_counts(pools, all_dates)
    board_index = build_board_index(default_board_catalog())
    fees = {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_duty_rate": 0.0005,
        "transfer_rate": 0.00001,
    }
    limitations = [
        "当前概念成分不是历史快照，存在概念调整和幸存者偏差。",
        "日 K 无法还原四个时点的真实价格、量比和盘口。",
        "D 日开盘价是第二梯队组指定的历史代理买入价。",
    ]
    requested_dates = set(dates)
    requested_errors = [
        error for error in errors if error["date"] in requested_dates
    ]
    if requested_errors:
        limitations.append(
            "历史涨停池缺失/失败 {} 日；示例：{}".format(
                len(requested_errors),
                "、".join(row["date"] for row in requested_errors[:10]),
            )
        )

    paths = report_paths(Path.cwd(), args.start, args.end, args.times)
    bars_by_code = {}
    members_cache = {}
    results = []
    for time in args.times:
        print("执行时点 {}".format(time), file=sys.stderr)
        result = run_time_point(
            time,
            pools,
            dates,
            board_index,
            args.capital,
            fees,
            bars_by_code=bars_by_code,
            members_cache=members_cache,
            workers=args.workers,
        )
        result.update({
            "start": args.start,
            "end": args.end,
            "capital": args.capital,
            "limitations": limitations,
        })
        results.append(result)
        path = paths[time]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_time_report(result), encoding="utf-8")

    comparison_path = paths["comparison"]
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        render_comparison(results), encoding="utf-8")
    print("报告：{}".format(comparison_path))


if __name__ == "__main__":
    http_util.run(main)
