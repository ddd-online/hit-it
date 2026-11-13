# 运行期预计算与模型判断边界

## 启动命令

```bash
python limit-hit/scripts/prepare_candidates.py --compact
```

`--compact` 输出模型友好的单行 JSON，只保留决策字段；需要完整审计时使用 `--json`，去掉两者输出文本表。脚本只读写 `report/涨停池/` 下的涨停池事实文件，不生成买卖结论。

盘中可以在任意交易时点运行。历史样本中 `09:30–09:35` 首次封板的候选质量最好，建议每 1–2 分钟运行；`10:00–11:00` 运行时优先看刚封板、刚回封或强题材核心，封板已久且无新催化的票降级。输出中的 `generated_at` 是本次快照时间，当前日K、换手和封板字段可能尚未收盘，报告必须按盘中快照解释。

`data_date` 是行情实际数据日期，`generated_at` 是本次执行时间。报告必须使用 `report_path` 落盘；即使凌晨执行，文件名也只能由 `data_date` 决定。

## 脚本负责的计算

1. 经技能内 `scripts/fetch_limit_up.py` 实拉当日涨停、炸板、跌停、昨日涨停今日表现和市场概览；
2. 读取 `report/涨停池/<previous_date>.json`；不存在时优先用当日 `fetch_limit_up.py --json` 随带的 `prev_zt_rows` 生成；
3. 当日接口未带回昨日池时，经技能内 `scripts/fetch_limit_up_history.py` 按 `prev_date` 回补并生成涨停池文件；
4. 批量取精确概念，并按概念与“东方系/羊字辈”等名称组聚合；当日炸板、跌停和昨日炸板、跌停并发获取，涨停池不在主流程重复拉取；
5. 计算每组今日涨停、昨日涨停、晋级数、最高板和板数结构；
6. 批量计算所有 1/2 板候选的：
   - 首封/末封、开板次数、换手和封单占成交额；
   - 近 5/10/20 日涨幅、MA5/10/20；
   - 5 日量比、开盘差、最低价相对昨收差；
   - 获利盘、平均成本、90% 筹码集中度、筹码峰；
7. 盘后且当日行情已收盘时，若 `report/涨停池/<data_date>.json` 不存在，保存当日池供下一交易日复用；
8. 输出数据限制、异常和“一字板/尾盘首封/开板过多/换手过热”等事实标签；
9. 输出一进二、二进三分模式晋级/高开统计，以及当前最高板和 2 板及以上家数。

脚本不计算最终候选分，不决定题材强弱，不判断消息真假，不输出“可打板”。

## 模型负责的判断

模型拿到脚本 JSON 后只做以下工作：

1. 根据题材宽度、梯队、昨日延续和当前扩散，决定哪些题材值得继续分析；
2. 将入围的 1–3 个题材一次传给 `scripts/fetch_sector_boards.py --search ... --json`，再只对返回的 BK 调用 `scripts/fetch_sector_leaders.py --compact`；需要逐股核验时改用 `--json`；
3. 只对每个题材的前 1–2 只候选搜索最新消息，并调用新闻、资金流报告；
4. 根据洗盘、筹码、梯队、题材趋势和消息硬门槛，决定“可打板/只观察/排除”；
5. 一个题材只保留一只，写入固定报告。

不要把脚本已经算好的字段再逐只手工计算，也不要让脚本决定买卖结论。

## 主要输出字段

- `data_date`、`previous_date`、`quote_date`；
- `board_permission`、`board_permission_excluded_count`：固定沪深主板权限及被排除的一/二板数量；
- `report_path`：由 `data_date` 生成的 `report/打板候选/YYYY-MM-DD.md`；
- `previous_source`、`previous_pool_report`、`previous_pool_generated`、`previous_context`；
- `current_pool_report`、`current_pool_generated`；
- `market`：当日概览；
- `market.max_board`、`market.two_plus_count`、`market.board_counts`：当前连板高度结构；
- `yesterday_open`：昨日涨停今日高开/平开/低开分布；
- `yesterday_highest_board`：昨日最高板及今日表现；
- `mode_feedback`：昨日一进二、二进三的样本、晋级率、高开率和平均开盘/收盘；
- `themes`：题材聚合与板数结构；
- `candidates`：所有当日 1/2 板候选及原始指标；
- `minutes_since_first_seal`、`minutes_since_last_seal`、`is_reseal`：判断是否刚封、刚回封或封板已久；
- `limitations`：口径限制。

`--compact` 顶层保留 `schema`、行情日期、上游来源、市场情绪、分模式反馈、题材聚合、扁平化候选、指标质量摘要和限制；默认只列前 30 个题材并给出 `theme_total_count`、`themes_truncated`，完整题材列表使用 `--json`。候选不再嵌套完整 `metrics`，百分数保留 2 位。

板块权限固定为沪深主板：沪市 `600/601/603/605`，深市 `000/001/002/003`。创业板、科创板和北交所股票不会进入 `candidates` 或题材的 `candidate_codes`。

候选的 `metric_quality`：

- `exact`：东财日 K 与精确换手率可用；
- `degraded`：东财日 K 不可用，腾讯日 K 与当前流通股本近似换手率，筹码只能作辅助证据；
- `unavailable`：日 K 或筹码未获取。

字段缺失就按缺失处理，脚本不会填 0，也不会用本机时间替代行情时间。
开板次数的真实 `0` 与未获取的 `null` 必须保持区别；行情时间一律按北京时间判断。
