#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加载并校验 limit-hit 的固定 YAML 规则配置。"""

import copy
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


def _scalar(value, path, line_number):
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return int(value)
    except ValueError:
        if not value:
            raise ValueError("{}:{} 配置值不能为空".format(path, line_number))
        return value


def _parse_yaml(path):
    """只解析本配置使用的映射和标量，遇到其他 YAML 语法直接报错。"""
    root = {}
    stack = [(-1, root)]
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if "\t" in line:
            raise ValueError("{}:{} 不允许使用制表符".format(path, line_number))
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if ":" not in text:
            raise ValueError("{}:{} 配置格式错误".format(path, line_number))
        key, value = (part.strip() for part in text.split(":", 1))
        if not key:
            raise ValueError("{}:{} 配置键不能为空".format(path, line_number))
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise ValueError("{}:{} 配置键重复：{}".format(path, line_number, key))
        if value:
            parent[key] = _scalar(value, path, line_number)
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return root


def _require(profile, path, expected_type):
    value = profile
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError("配置缺少键：{}".format(".".join(path)))
        value = value[key]
    if type(value) is not expected_type:
        raise ValueError("配置类型错误：{}".format(".".join(path)))
    return value


def _validate(name, profile):
    if not isinstance(profile, dict):
        raise ValueError("配置必须为映射：{}".format(name))
    _require(profile, ("one_word_executable",), bool)
    _require(profile, ("collapse_veto",), bool)
    gate = _require(profile, ("weak_market_gate",), dict)
    _require(gate, ("max_picks",), int)
    _require(gate, ("min_score",), int)
    override = _require(gate, ("override",), dict)
    enabled = _require(override, ("enabled",), bool)
    if enabled:
        _require(override, ("theme_score_threshold",), int)
        modes = _require(override, ("min_score_by_mode",), dict)
        _require(modes, ("one_to_two",), int)
        _require(modes, ("two_to_three",), int)


def load_rule_profile(name="theme-first", path=None):
    config_path = Path(path) if path else CONFIG_PATH
    profiles = _parse_yaml(config_path)
    if profiles.get("version") != 1:
        raise ValueError("配置 version 必须为 1")
    if name not in profiles or name == "version":
        raise ValueError("未知 profile：{}".format(name))
    profile = profiles[name]
    _validate(name, profile)
    return copy.deepcopy(profile)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_word_evidence(row, is_one_word):
    amount = _number(row.get("amount_yi"))
    seal_fund = _number(row.get("seal_fund_yi"))
    float_cap = _number(row.get("float_cap_yi"))
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


def filter_one_word_candidates(candidates, one_word_codes, profile):
    """按 profile 判断一字板是否进入候选；非一字板原样保留。"""
    executable = profile["one_word_executable"]
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
        if not executable:
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
        today_pct = _number(stock.get("today_pct"))
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
                         yesterday_high, previous_max_board, profile):
    """拆分市场宽度与最高板风险，并给出 profile 对应的数量门槛。"""
    market = market or {}
    zt_count = _number(market.get("zt_count"))
    dt_count = _number(market.get("dt_count"))
    zb_count = _number(market.get("zb_count"))
    missing_counts = None in (zt_count, dt_count, zb_count)
    promotion = _number(promotion_rate_pct)
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
    weak = bool(
        (promotion is not None and 15 <= promotion < 25)
        or (break_rate is not None and 35 <= break_rate <= 45)
    )
    opening = yesterday_open or {}
    up_open = _number(opening.get("up_open")) or 0
    down_open = _number(opening.get("down_open")) or 0
    collective_low_open = bool(
        down_open > up_open
        or ((_number(opening.get("avg_gap_pct")) or 0) < 0)
    )

    if leader_collapse:
        state, action, max_picks = "崩塌", "observe_only", 0
    elif missing_counts:
        state, action, max_picks = "数据未获取", "no_conclusion", 0
    elif retreat:
        state, action, max_picks = "退潮", "no_trade", 0
    elif breadth_collapse and profile["collapse_veto"]:
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
        "weak": weak,
        "collective_low_open": collective_low_open,
        "break_rate_pct": break_rate,
        "promotion_rate_pct": promotion,
        "veto": action in ("no_trade", "observe_only", "no_conclusion"),
        "max_picks": max_picks,
        "weak_market_gate": copy.deepcopy(profile["weak_market_gate"]),
    }


def minimum_candidate_score(theme_score, mode, market_gate, profile):
    """返回当前市场和题材强度下的候选结构分门槛。"""
    mode_gate = {
        "one_to_two": 80,
        "two_to_three": 79,
    }
    if mode not in mode_gate:
        raise ValueError("未知候选模式：{}".format(mode))
    if market_gate["action"] in ("no_trade", "observe_only", "no_conclusion"):
        return None
    if market_gate["state"] != "偏弱":
        return mode_gate[mode]
    weak_gate = profile["weak_market_gate"]
    override = weak_gate["override"]
    if (
        override["enabled"]
        and theme_score >= override["theme_score_threshold"]
    ):
        return override["min_score_by_mode"][mode]
    return weak_gate["min_score"]
