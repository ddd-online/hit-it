# 运行期预计算与模型判断边界

## 启动命令

每个交易日收盘后先保存涨停池：

```bash
python limit-hit/scripts/save_limit_pool.py
```

需要补指定日期时：

```bash
python limit-hit/scripts/save_limit_pool.py --date YYYYMMDD
```

运行候选预计算：

```bash
python limit-hit/scripts/prepare_candidates.py --compact
```

保存脚本固定写入当前目录 `report/涨停池/YYYY-MM-DD.json`，已有文件不覆盖。`prepare_candidates.py` 只读取昨日文件，不拉取、回补、生成或覆盖涨停池。

`--compact` 输出模型友好的单行 JSON，只保留决策字段；需要完整审计时使用 `--json`，去掉两者输出文本表。

盘中可以在任意交易时点运行。历史样本中 `09:30–09:35` 首次封板的候选质量最好，建议每 1–2 分钟运行；`10:00–11:00` 运行时优先看刚封板、刚回封或强题材核心，封板已久且无新催化的票降级。输出中的 `generated_at` 是本次快照时间，当前日K、换手和封板字段可能尚未收盘，报告必须按盘中快照解释。

`data_date` 是行情实际数据日期，`generated_at` 是本次执行时间。报告必须使用 `report_path` 落盘；即使凌晨执行，文件名也只能由 `data_date` 决定。

## 脚本负责的计算

1. 经技能内 `scripts/fetch_limit_up.py --current-only --json` 实拉当日涨停、炸板、跌停、昨日涨停今日表现和市场概览，不请求前一日涨停池；
2. 读取 `report/涨停池/<previous_date>.json`；文件不存在、损坏或日期不符时停止运行，并提示先运行保存脚本；
3. 批量取精确概念，并按概念与“东方系/羊字辈”等名称组聚合；
4. 计算每组今日涨停、昨日涨停、晋级数、最高板和板数结构；
5. 先保留当日 1/2 板，再执行沪深主板权限过滤；若昨日最高板停牌，继续排除与其同行业或共享精确概念的候选。被这两步排除的股票不再请求个股指标；
6. 对剩余 1/2 板候选批量计算：
   - 首封/末封、开板次数、换手和封单占成交额；
   - 近 5/10/20 日涨幅、MA5/10/20；
   - 5 日量比、开盘差、最低价相对昨收差；
   - 获利盘、平均成本、90% 筹码集中度、筹码峰；
7. 输出数据限制、异常和“一字板/尾盘首封/开板过多/换手过热”等事实标签；
8. 输出一进二、二进三分模式晋级/高开统计，以及当前最高板和 2 板及以上家数。

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
- `suspension_filter`：停牌最高板、触发的行业/精确概念、连带排除数及逐股排除明细；
- `report_path`：由 `data_date` 生成的 `report/打板候选/YYYY-MM-DD.md`；
- `previous_source`、`previous_pool_report`、`previous_pool_generated`（固定 `false`）、`previous_context`；
- `current_pool_report`、`current_pool_generated`：固定为 `null`/`false`，运行时只处理实时快照，不落盘涨停池；
- `market`：当日概览；
- `market.max_board`、`market.two_plus_count`、`market.board_counts`：当前连板高度结构；
- `yesterday_open`：昨日涨停总样本、有效开盘、行情缺失、停牌和高开/平开/低开分布；
- `yesterday_highest_board`：昨日最高板及今日表现，每项含行业、精确概念/名称组、停牌状态；
- `mode_feedback`：昨日一进二、二进三的有效样本、停牌数、晋级率、高开率和平均开盘/收盘；
- `themes`：题材聚合与板数结构；
- `candidates`：所有当日 1/2 板候选及原始指标；
- `minutes_since_first_seal`、`minutes_since_last_seal`、`is_reseal`：判断是否刚封、刚回封或封板已久；
- `limitations`：口径限制。

`--compact` 顶层保留 `schema`、行情日期、上游来源、市场情绪、分模式反馈、题材聚合、扁平化候选、指标质量摘要和限制；默认只列前 30 个题材并给出 `theme_total_count`、`themes_truncated`，完整题材列表使用 `--json`。候选不再嵌套完整 `metrics`，百分数保留 2 位。

板块权限固定为沪深主板：沪市 `600/601/603/605`，深市 `000/001/002/003`。创业板、科创板和北交所股票不会进入 `candidates` 或题材的 `candidate_codes`。停牌最高板同行业或共享精确概念的候选也不会进入 `candidates` 或 `candidate_codes`，但 `themes[].current_count` 和板数结构仍保留全市场涨停事实。

停牌只按报价型启发式识别：个股行情日等于 `data_date`，且今开、最高、最低、成交量均为 0。股票不在行情响应中或字段完全缺失时是“行情缺失”，不是停牌。盘前零成交也不在 `limit-hit` 合法运行窗口内，不得复用 `suspended` 状态。

候选的 `metric_quality`：

- `exact`：东财日 K 与精确换手率可用；
- `degraded`：东财日 K 不可用，腾讯日 K 与当前流通股本近似换手率，筹码只能作辅助证据；
- `unavailable`：日 K 或筹码未获取。

字段缺失就按缺失处理，脚本不会填 0，也不会用本机时间替代行情时间。
开板次数的真实 `0` 与未获取的 `null` 必须保持区别；行情时间一律按北京时间判断。
