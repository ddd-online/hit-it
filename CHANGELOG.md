# Changelog

## 1.5.0 - 2026-11-18

- 新增 `limit-hit/config/rules.yaml`，提供等价的 `default`、`theme-first` 默认预设及旧口径 `v1.4`。
- `prepare_candidates.py` 和 `backtest_limit_hit.py` 新增 `--profile`，默认行为改为 `theme-first`。
- 一字板改为可配置执行口径；启用后仅在板上成交额大于 0 且封单可核验时进入候选，并输出完整排板证据。
- 将原“崩塌”拆分为可配置的宽度崩塌与不可关闭的高标崩塌；宽度否决关闭后仍保留退潮、集体低开和高标负反馈限制。
- 偏弱市场支持按题材强度覆盖结构分门槛，数量上限仍为 1。
- 报告模板、运行说明和筛选细则同步回显 profile、规则开关与覆盖前后结果。
- 增加 profile、一字板、市场门槛和回测成交边界测试。
