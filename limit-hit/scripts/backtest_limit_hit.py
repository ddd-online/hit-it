#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""limit-hit 结构代理回测。

只使用历史涨停池和日K，回测一进二/二进三的结构条件与次日开盘退出：
- 历史题材用同花顺 reason_tags 代理，不使用事后概念归属；
- 无法复现历史消息硬门槛、分时封板过程和当日板块扩散；
- 每个交易日最多一笔，按 100 股整手和初始本金全仓回测。
"""

import argparse
import collections
import json
import math
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fallback_sources  # noqa: E402
import fetch_limit_up_history  # noqa: E402
import http_util  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
TEMPLATE_PATH = SKILL_DIR / "references" / "backtest-report-template.md"
TENCENT_RAW_KLINE = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    "?param={code},day,,,{days},"
)


def parse_date(value):
    return datetime.strptime(value, "%Y%m%d")


def compact_date(value):
    return value.replace("-", "")


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_raw_kline(code, days=400):
    """腾讯不复权日K，避免前复权价格影响整手资金模拟。"""
    raw = fallback_sources.http_get(
        TENCENT_RAW_KLINE.format(code=code, days=days),
        fallback_sources.TENCENT_REFERER,
        timeout=25,
    )
    data = json.loads(fallback_sources.decode_text(raw))
    node = (data.get("data") or {}).get(code) or {}
    rows = node.get("day") or node.get("qfqday") or []
    return {
        str(row[0]): {
            "date": str(row[0]),
            "open": to_float(row[1]),
            "close": to_float(row[2]),
            "high": to_float(row[3]),
            "low": to_float(row[4]),
        }
        for row in rows
        if len(row) >= 5
    }


def get_trading_dates(start_date, end_date, lookback_days=20):
    bars = fetch_raw_kline("sh000001", days=500)
    all_dates = sorted(day for day in bars if day <= end_date.isoformat())
    dates = [day for day in all_dates if day >= start_date.isoformat()]
    if not dates:
        return [], [], bars
    start_index = all_dates.index(dates[0])
    warmup = all_dates[max(0, start_index - lookback_days):start_index]
    return dates, warmup, bars


def fetch_pool(date_text, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            rows, _total = fetch_limit_up_history.fetch(compact_date(date_text))
            return rows
        except Exception as exc:  # noqa: BLE001 网络抖动重试
            last_error = exc
            if attempt + 1 < retries:
                import time
                time.sleep(1 + attempt)
    raise RuntimeError("{}：{}".format(date_text, last_error))


def load_pools(dates, workers):
    pools = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {executor.submit(fetch_pool, day): day for day in dates}
        for index, future in enumerate(as_completed(futures), 1):
            day = futures[future]
            try:
                pools[day] = future.result()
            except Exception as exc:  # noqa: BLE001 单日缺失保留在报告中
                errors.append({"date": day, "error": str(exc)})
            if index % 20 == 0 or index == len(dates):
                print("历史涨停池 {}/{}".format(index, len(dates)), file=sys.stderr)
    return pools, errors


def apply_consecutive_board_counts(pools, dates):
    """按连续交易日持仓池成员关系恢复真实连板数。"""
    previous_codes = None
    previous_counts = {}
    for day in dates:
        rows = pools.get(day)
        if rows is None:
            previous_codes = None
            previous_counts = {}
            continue
        current_codes = {row["code"] for row in rows}
        for row in rows:
            code = row["code"]
            exact_board_count = row.get("board_count")
            if exact_board_count is not None:
                row["board_count"] = exact_board_count
            elif previous_codes is not None and code in previous_codes:
                previous = previous_counts.get(code)
                row["board_count"] = previous + 1 if previous is not None else None
            elif previous_codes is None:
                row["board_count"] = None
            else:
                row["board_count"] = 1
        previous_codes = current_codes
        previous_counts = {
            row["code"]: row.get("board_count") for row in rows
        }


def is_mainboard(code):
    return code.startswith(("sh600", "sh601", "sh603", "sh605",
                            "sz000", "sz001", "sz002", "sz003"))


def is_st(name):
    return "ST" in str(name or "").upper() or "退" in str(name or "")


def candidate_eligible(row):
    if not is_mainboard(str(row.get("code") or "")):
        return False
    if is_st(row.get("name")):
        return False
    if row.get("board_count") not in (1, 2):
        return False
    turnover = to_float(row.get("turnover_pct"))
    if turnover is not None and turnover > 35:
        return False
    first_seal = str(row.get("first_seal") or "")
    opens = row.get("open_count")
    if first_seal > "14:00:00" and (opens or 0) >= 5:
        return False
    return True


def reason_tags(row):
    return [
        str(tag).strip()
        for tag in row.get("reason_tags") or []
        if str(tag).strip() and str(tag).strip() != "未分类"
    ]


def first_seal_score(clock):
    if not clock:
        return 0
    if clock <= "09:35:00":
        return 30
    if clock <= "10:00:00":
        return 24
    if clock <= "11:00:00":
        return 16
    if clock <= "14:00:00":
        return 8
    return 0


def candidate_score(row):
    opens = row.get("open_count")
    if opens is None:
        open_score = 0
    elif opens == 0:
        open_score = 25
    elif opens <= 2:
        open_score = 18
    elif opens <= 4:
        open_score = 8
    else:
        open_score = 0
    turnover = to_float(row.get("turnover_pct"))
    if turnover is None:
        turnover_score = 0
    elif 5 <= turnover <= 20:
        turnover_score = 20
    elif 20 < turnover <= 25:
        turnover_score = 15
    elif 25 < turnover <= 35:
        turnover_score = 8
    else:
        turnover_score = 12
    board_score = 10 if row.get("board_count") == 2 else 5
    return first_seal_score(str(row.get("first_seal") or "")) + open_score \
        + turnover_score + board_score


def theme_score(current_count, promoted_count, max_board):
    if current_count >= 5:
        width = 25
    elif current_count >= 3:
        width = 20
    elif current_count == 2:
        width = 12
    else:
        width = 5
    if max_board >= 3:
        ladder = 25
    elif max_board == 2:
        ladder = 15
    else:
        ladder = 8
    if promoted_count >= max(2, math.ceil(current_count * 0.4)):
        continuation = 20
    elif promoted_count:
        continuation = 14
    else:
        continuation = 0
    return width + ladder + continuation


def ranked_candidates(current_rows, previous_rows, max_themes,
                      min_theme_score=50, min_candidate_score=60):
    current_by_theme = collections.defaultdict(list)
    previous_by_theme = collections.defaultdict(list)
    for row in current_rows:
        for theme in reason_tags(row):
            current_by_theme[theme].append(row)
    for row in previous_rows:
        for theme in reason_tags(row):
            previous_by_theme[theme].append(row)

    themes = []
    for theme, rows in current_by_theme.items():
        candidates = [row for row in rows if candidate_eligible(row)]
        if not candidates:
            continue
        previous = previous_by_theme.get(theme, [])
        current_codes = {row["code"] for row in rows}
        previous_codes = {row["code"] for row in previous}
        promoted = len(current_codes & previous_codes)
        max_board = max((row.get("board_count") or 0 for row in rows), default=0)
        if len(rows) < 2 and not promoted:
            continue
        score = theme_score(len(rows), promoted, max_board)
        if score < min_theme_score:
            continue
        candidates = [
            row for row in candidates
            if candidate_score(row) >= min_candidate_score
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda row: (
                -candidate_score(row),
                row.get("first_seal") or "99:99:99",
                row["code"],
            )
        )
        themes.append((score, theme, promoted, len(rows), max_board, candidates[:3]))

    themes.sort(key=lambda item: (-item[0], -item[3], -item[4], item[1]))
    ranked = []
    seen_codes = set()
    for score, theme, promoted, current_count, max_board, candidates in themes[:max_themes]:
        for row in candidates:
            if row["code"] in seen_codes:
                continue
            seen_codes.add(row["code"])
            ranked.append({
                "theme": theme,
                "theme_score": score,
                "theme_current_count": current_count,
                "theme_promoted_count": promoted,
                "theme_max_board": max_board,
                "candidate_score": candidate_score(row),
                "row": row,
            })
    return ranked


def simulate_trade(candidate, entry_price, exit_price, capital, fees,
                   position_fraction=1.0):
    allocated_cash = capital * position_fraction
    shares = int(allocated_cash // (entry_price * 100)) * 100
    if shares < 100:
        return None
    buy_value = shares * entry_price
    buy_commission = max(buy_value * fees["commission_rate"], fees["min_commission"])
    buy_transfer = buy_value * fees["transfer_rate"]
    sell_value = shares * exit_price
    sell_commission = max(sell_value * fees["commission_rate"], fees["min_commission"])
    sell_stamp = sell_value * fees["stamp_duty_rate"]
    sell_transfer = sell_value * fees["transfer_rate"]
    total_fees = buy_commission + buy_transfer + sell_commission + sell_stamp + sell_transfer
    net_pnl = sell_value - buy_value - total_fees
    capital_after = capital + net_pnl
    return {
        "shares": shares,
        "entry_price": round(entry_price, 3),
        "exit_price": round(exit_price, 3),
        "position_fraction": position_fraction,
        "allocated_cash": round(allocated_cash, 2),
        "position_value": round(buy_value, 2),
        "cash_after_buy": round(capital - buy_value, 2),
        "gross_gap_pct": round((exit_price / entry_price - 1) * 100, 3),
        "fees": round(total_fees, 2),
        "net_pnl": round(net_pnl, 2),
        "net_return_pct": round(net_pnl * 100.0 / capital, 3),
        "capital_after": round(capital_after, 2),
    }


def max_drawdown_pct(equity_values):
    peak = equity_values[0]
    worst = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)
    return abs(worst) * 100.0


def summarize_trades(trades, initial_capital, signal_days):
    gross = [trade["gross_gap_pct"] for trade in trades]
    winners = [trade["net_pnl"] for trade in trades if trade["net_pnl"] > 0]
    losers = [trade["net_pnl"] for trade in trades if trade["net_pnl"] < 0]
    winner_returns = [trade["net_return_pct"] for trade in trades
                      if trade["net_return_pct"] > 0]
    loser_returns = [trade["net_return_pct"] for trade in trades
                     if trade["net_return_pct"] < 0]
    odds = (
        statistics.mean(winner_returns) / abs(statistics.mean(loser_returns))
        if winner_returns and loser_returns else None
    )
    ending_capital = trades[-1]["capital_after"] if trades else initial_capital
    equity = [initial_capital] + [trade["capital_after"] for trade in trades]
    return {
        "signal_days": signal_days,
        "trade_count": len(trades),
        "direction_wins": sum(value > 0 for value in gross),
        "direction_win_rate_pct": round(
            sum(value > 0 for value in gross) * 100.0 / len(gross), 2
        ) if gross else None,
        "net_wins": len(winners),
        "net_win_rate_pct": round(len(winners) * 100.0 / len(trades), 2)
        if trades else None,
        "odds": round(odds, 2) if odds is not None else None,
        "avg_open_pct": round(statistics.mean(gross), 3) if gross else None,
        "avg_high_open_pct": round(
            statistics.mean(value for value in gross if value > 0), 3
        ) if any(value > 0 for value in gross) else None,
        "avg_low_open_pct": round(
            statistics.mean(value for value in gross if value < 0), 3
        ) if any(value < 0 for value in gross) else None,
        "avg_winner_pnl": round(statistics.mean(winners), 2) if winners else None,
        "avg_loser_pnl": round(statistics.mean(losers), 2) if losers else None,
        "initial_capital": round(initial_capital, 2),
        "ending_capital": round(ending_capital, 2),
        "net_profit": round(ending_capital - initial_capital, 2),
        "net_return_pct": round(
            (ending_capital / initial_capital - 1.0) * 100, 2
        ),
        "max_drawdown_pct": round(max_drawdown_pct(equity), 2),
    }


def monthly_summary(trades):
    grouped = collections.defaultdict(list)
    for trade in trades:
        grouped[trade["signal_date"][:7]].append(trade)
    rows = []
    for month in sorted(grouped):
        items = grouped[month]
        rows.append({
            "month": month,
            "trades": len(items),
            "net_wins": sum(item["net_pnl"] > 0 for item in items),
            "avg_open_pct": round(
                statistics.mean(item["gross_gap_pct"] for item in items), 2),
            "net_pnl": round(sum(item["net_pnl"] for item in items), 2),
        })
    return rows


def pct(value):
    return "-" if value is None else "{:+.2f}%".format(value)


def rate(value):
    return "-" if value is None else "{:.2f}%".format(value)


def money(value):
    return "-" if value is None else "{:,.2f}".format(value)


def render_report(result, trades, monthly, errors, args):
    trade_lines = [
        "| 信号日 | 退出日 | 题材 | 代码 | 名称 | 板数 | 首封 | 开板 | 换手% | 入场 | 开盘退出 | 开盘幅度 | 净收益 | 期末本金 |",
        "|---|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for trade in trades:
        trade_lines.append(
            "| {signal_date} | {exit_date} | {theme} | `{code}` | {name} | "
            "{board_count} | {first_seal} | {open_count} | {turnover_pct:.2f} | "
            "{entry_price:.2f} | {exit_price:.2f} | {gross_gap_pct:+.2f}% | "
            "{net_pnl:+.2f}元 | {capital_after:,.2f}元 |".format(**trade)
        )
    if not trades:
        trade_lines.append("| - | - | - | - | 无成交 | - | - | - | - | - | - | - | - | - |")

    monthly_lines = [
        "| 月份 | 成交 | 净胜 | 平均开盘幅度 | 净收益 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in monthly:
        monthly_lines.append(
            "| {month} | {trades} | {net_wins} | {avg_open_pct:+.2f}% | "
            "{net_pnl:+.2f}元 |".format(**row)
        )
    if not monthly:
        monthly_lines.append("| - | 0 | 0 | - | 0.00元 |")

    missing_lines = [
        "- 历史消息、公告负事件与公司基本面无法逐日完整复现，因此消息硬门槛未纳入。",
        "- 历史题材使用同花顺 `reason_tags` 代理，不代表当时东方财富精确概念。",
        "- 历史池的“几天几板”只作窗口统计；当前连板数由预热期内连续交易日池成员关系回算。",
        "- 缺少历史盘中封板与板块成分扩散快照；入场按 D 日涨停收盘价模拟。",
        "- 每交易日最多一笔，单笔使用当时总权益的 {:.0%}，100 股整手；无法成交的高价股会跳过。".format(
            args.position_fraction),
        "- 当前只回测沪深主板；创业板、科创板和北交所不在该结构代理范围内。",
        "- 回测为结构代理，不等同完整 SKILL 实盘流程。",
    ]
    if errors:
        sample = "、".join(item["date"] for item in errors[:10])
        missing_lines.append(
            "- 历史涨停池缺失/失败 {} 日，缺失日期不生成信号。示例：{}".format(
                len(errors), sample)
        )

    replacements = {
        "period": "{} 至 {}".format(args.start, args.end),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "initial_capital": money(args.capital),
        "backtest_type": "结构代理回测",
        "trading_days": str(result["trading_days"]),
        "warmup_days": str(result["warmup_days"]),
        "entry_rule": "信号日涨停收盘价买入",
        "exit_rule": "下一交易日开盘价卖出",
        "position_rule": "每个交易日最多一笔，单笔使用当时总权益的 {:.0%}，100 股整手，其余现金不计收益".format(
            args.position_fraction),
        "fee_rule": "佣金 {:.4%}（最低 {:.0f} 元）+ 卖出印花税 {:.3%} + 双边过户费 {:.4%}".format(
            args.commission_rate, args.min_commission,
            args.stamp_duty_rate, args.transfer_rate),
        "selection_rule": "题材结构分≥{}/70，候选结构分≥{}/85；每个交易日最多考虑 {} 个题材。".format(
            args.min_theme_score, args.min_candidate_score, args.max_themes),
        "signal_days": str(result["signal_days"]),
        "trade_count": str(result["trade_count"]),
        "direction_win_rate": rate(result["direction_win_rate_pct"]),
        "net_win_rate": rate(result["net_win_rate_pct"]),
        "odds": "-" if result["odds"] is None else "{:.2f}".format(result["odds"]),
        "avg_open_pct": pct(result["avg_open_pct"]),
        "avg_high_open_pct": pct(result["avg_high_open_pct"]),
        "avg_low_open_pct": pct(result["avg_low_open_pct"]),
        "ending_capital": money(result["ending_capital"]),
        "net_profit": money(result["net_profit"]),
        "net_return_pct": pct(result["net_return_pct"]),
        "max_drawdown_pct": "{:.2f}%".format(result["max_drawdown_pct"]),
        "win_rate_definition": "方向胜率按次日开盘涨幅>0%；净收益胜率按扣除费用后的单笔净收益>0%。",
        "odds_definition": "盈利交易平均净收益率 ÷ 亏损交易平均净收益率的绝对值。",
        "monthly_table": "\n".join(monthly_lines),
        "trades_table": "\n".join(trade_lines),
        "limitations": "\n".join(missing_lines),
        "sources": "同花顺历史涨停池；腾讯不复权日K；交易费用为报告列示的假设值。",
    }
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{%s}}" % key, value)
    return text


def run_backtest(args):
    start_date = parse_date(args.start)
    end_date = parse_date(args.end)
    dates, warmup_dates, _index_bars = get_trading_dates(start_date, end_date)
    if not dates:
        raise RuntimeError("区间内没有交易日")

    all_dates = warmup_dates + dates
    pools, errors = load_pools(all_dates, args.workers)
    apply_consecutive_board_counts(pools, all_dates)
    requested_dates = set(dates)
    requested_errors = [
        error for error in errors if error["date"] in requested_dates
    ]
    previous_by_date = {
        all_dates[index]: pools.get(all_dates[index - 1], [])
        for index in range(1, len(all_dates))
    }
    next_date = {dates[index]: dates[index + 1] for index in range(len(dates) - 1)}
    bar_cache = {}
    trade_errors = collections.Counter()
    trades = []
    signal_days = 0
    capital = args.capital
    fees = {
        "commission_rate": args.commission_rate,
        "min_commission": args.min_commission,
        "stamp_duty_rate": args.stamp_duty_rate,
        "transfer_rate": args.transfer_rate,
    }

    for day in dates:
        rows = pools.get(day)
        if rows is None:
            continue
        candidates = ranked_candidates(
            rows,
            previous_by_date.get(day, []),
            args.max_themes,
            args.min_theme_score,
            args.min_candidate_score,
        )
        if not candidates:
            continue
        signal_days += 1
        exit_day = next_date.get(day)
        if not exit_day:
            trade_errors["没有下一交易日"] += 1
            continue

        for item in candidates:
            row = item["row"]
            code = row["code"]
            if code not in bar_cache:
                try:
                    bar_cache[code] = fetch_raw_kline(code)
                except Exception:  # noqa: BLE001 单票失败继续下一候选
                    bar_cache[code] = {}
            entry_bar = bar_cache[code].get(day)
            exit_bar = bar_cache[code].get(exit_day)
            if not entry_bar or not exit_bar:
                trade_errors["日K或次日行情缺失"] += 1
                continue
            entry_price = entry_bar.get("close")
            exit_price = exit_bar.get("open")
            if not entry_price or not exit_price:
                trade_errors["价格缺失"] += 1
                continue
            if entry_bar["open"] == entry_bar["high"] == entry_bar["low"] == entry_bar["close"]:
                trade_errors["一字板无法成交"] += 1
                continue
            simulated = simulate_trade(
                item, entry_price, exit_price, capital, fees,
                args.position_fraction,
            )
            if simulated is None:
                trade_errors["6000元不足一手"] += 1
                continue
            capital = simulated["capital_after"]
            trades.append({
                "signal_date": day,
                "exit_date": exit_day,
                "theme": item["theme"],
                "theme_score": item["theme_score"],
                "code": code,
                "name": row.get("name"),
                "board_count": row.get("board_count"),
                "first_seal": row.get("first_seal") or "-",
                "open_count": row.get("open_count") if row.get("open_count") is not None else "-",
                "turnover_pct": to_float(row.get("turnover_pct")) or 0.0,
                **simulated,
            })
            break

    summary = summarize_trades(trades, args.capital, signal_days)
    summary["trading_days"] = len(dates)
    summary["warmup_days"] = len(warmup_dates)
    summary["missing_pool_days"] = len(requested_errors)
    summary["skip_reasons"] = dict(trade_errors)
    monthly = monthly_summary(trades)
    report_path = Path(args.report) if args.report else (
        Path.cwd() / "report" / "回测" /
        "涨停打板_{}_{}.md".format(args.start, args.end)
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(summary, trades, monthly, requested_errors, args),
        encoding="utf-8",
    )
    return {
        "report_path": str(report_path),
        "summary": summary,
        "monthly": monthly,
        "trades": trades,
        "errors": requested_errors,
    }


def main():
    ap = argparse.ArgumentParser(description="limit-hit 结构代理回测")
    ap.add_argument("--start", default="20260101", help="开始日期 YYYYMMDD")
    ap.add_argument("--end", default=datetime.now().astimezone().strftime("%Y%m%d"),
                    help="结束日期 YYYYMMDD")
    ap.add_argument("--capital", type=float, default=6000.0, help="初始本金，默认6000")
    ap.add_argument("--position-fraction", type=float, default=1.0,
                    help="单笔使用的总权益比例，0-1，默认1（全仓）")
    ap.add_argument("--workers", type=int, default=4, help="历史涨停池并发数")
    ap.add_argument("--max-themes", type=int, default=3, help="每日最多考虑题材数")
    ap.add_argument("--min-theme-score", type=int, default=50,
                    help="题材结构分门槛（满分70）")
    ap.add_argument("--min-candidate-score", type=int, default=60,
                    help="候选结构分门槛（满分85）")
    ap.add_argument("--commission-rate", type=float, default=0.00025)
    ap.add_argument("--min-commission", type=float, default=5.0)
    ap.add_argument("--stamp-duty-rate", type=float, default=0.0005)
    ap.add_argument("--transfer-rate", type=float, default=0.00001)
    ap.add_argument("--report", help="报告路径")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()
    if args.capital <= 0:
        ap.error("--capital 必须大于 0")
    if not 0 < args.position_fraction <= 1:
        ap.error("--position-fraction 必须在 (0, 1] 区间")

    result = run_backtest(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    summary = result["summary"]
    summary["position_fraction"] = args.position_fraction
    print("回测区间：{} 至 {}".format(args.start, args.end))
    print("交易日：{}，信号日：{}，成交：{}".format(
        summary["trading_days"], summary["signal_days"], summary["trade_count"]))
    print("连板数回算预热：{} 个交易日".format(summary["warmup_days"]))
    print("单笔权益仓位：{:.0%}".format(args.position_fraction))
    print("方向胜率：{}，净收益胜率：{}，赔率：{}".format(
        rate(summary["direction_win_rate_pct"]),
        rate(summary["net_win_rate_pct"]),
        "-" if summary["odds"] is None else summary["odds"]))
    print("平均开盘：{}，平均高开：{}，平均低开：{}".format(
        pct(summary["avg_open_pct"]),
        pct(summary["avg_high_open_pct"]),
        pct(summary["avg_low_open_pct"])))
    print("本金 {:.2f} -> {:.2f}，净收益 {:.2f}，收益率 {}".format(
        summary["initial_capital"], summary["ending_capital"],
        summary["net_profit"], pct(summary["net_return_pct"])))
    print("报告：{}".format(result["report_path"]))


if __name__ == "__main__":
    http_util.run(main)
