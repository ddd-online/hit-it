# limit-hit 回测报告 · {{period}}

> 本报告是结构代理回测，不是完整 SKILL 实盘胜率。历史消息、公告负事件、盘中封板可成交性和板块扩散未复现；不得将期末收益直接外推。

生成时间：{{generated_at}}

初始本金：{{initial_capital}} 元

回测类型：{{backtest_type}}

## 口径

| 项目 | 设定 |
|---|---|
| 样本区间 | {{period}} |
| 交易日 | {{trading_days}} |
| 连板数回算预热 | {{warmup_days}} 个交易日 |
| 规则预设 | {{profile_name}} |
| 规则开关 | {{profile_rules}} |
| 筛选门槛 | {{selection_rule}} |
| 开仓规则 | {{entry_rule}} |
| 退出规则 | {{exit_rule}} |
| 仓位规则 | {{position_rule}} |
| 费用规则 | {{fee_rule}} |
| 数据源 | 同花顺历史涨停池、腾讯不复权日K |

## 核心结果

| 指标 | 结果 |
|---|---:|
| 信号日 | {{signal_days}} |
| 实际成交 | {{trade_count}} |
| 方向胜率 | {{direction_win_rate}} |
| 净收益胜率 | {{net_win_rate}} |
| 赔率 | {{odds}} |
| 平均开盘幅度 | {{avg_open_pct}} |
| 平均高开幅度 | {{avg_high_open_pct}} |
| 平均低开幅度 | {{avg_low_open_pct}} |
| 初始本金 | {{initial_capital}} 元 |
| 期末本金 | {{ending_capital}} 元 |
| 净收益 | {{net_profit}} 元 |
| 净收益率 | {{net_return_pct}} |
| 最大回撤 | {{max_drawdown_pct}} |

胜率定义：{{win_rate_definition}}
赔率定义：{{odds_definition}}

## 月度结果

{{monthly_table}}

## 交易明细

{{trades_table}}

## 数据完整性与限制

{{limitations}}

数据源：{{sources}}

不构成投资建议
