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
python limit-hit/scripts/prepare_candidates.py --compact
```

单股持仓审视使用同一入口：

```bash
python limit-hit/scripts/prepare_candidates.py --stock 002520 --compact
python limit-hit/scripts/prepare_candidates.py --stock 日发精机 --compact
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
5. 先保留当日 1/2 板，再执行沪深主板权限过滤；若昨日最高板停牌，继续排除与其同行业或共享精确概念的候选；最后按内置规则处理一字板，只保留板上成交额大于 0 且封单可核验的一字板。被这些硬过滤排除的股票不再请求个股指标；
6. 对剩余 1/2 板候选批量计算：
   - 首封/末封、开板次数、换手和封单占成交额；
   - 近 5/10/20 日涨幅、MA5/10/20；
   - 5 日量比、开盘差、最低价相对昨收差；
   - 获利盘、平均成本、90% 筹码集中度、筹码峰；
7. 输出数据限制、异常和“一字板/尾盘首封/开板过多/换手过热”等事实标签；
8. 输出一进二、二进三分模式晋级/高开统计，以及当前最高板和 2 板及以上家数；
9. 使用脚本内置规则输出 `rules`、`market_gate` 和 `one_word_filter`，拆分宽度崩塌与高标崩塌；同时输出真实行情快照时间 `quote_time` 与 `market_gate.promotion_sample_time`。
10. `09:25` 前按行情时间识别“开盘未撮合”，不得把 `open=0` 计算成低开；`09:25–10:30` 的低晋级率输出“退潮待确认”，10:30 后才确认退潮。

单股模式在上述公共上下文之外额外完成：

1. 把裸代码、交易所前缀/后缀或唯一股票名称解析为沪深主板代码；名称歧义时停止；
2. 即使目标不在当日涨停池，也直接获取实时报价、日K、量能、均线、筹码和股东户数；
3. 标记目标当日状态、当前/昨日板数、是否曾涨停或炸板；
4. 计算现价相对开盘/最高价、相对昨日成交量、5 日量比和换手；
5. 盘中明确标记 `volume_ratio_scope=盘中部分量`，收盘后才按全天量能。

股票名称检索优先使用腾讯证券检索，失败或无结果时回退东方财富证券检索；两者都失败时停止，不使用模糊猜测。

脚本不计算最终候选分，不决定题材强弱，不判断消息真假，不输出“可打板”。

## 模型负责的判断

模型拿到脚本 JSON 后只做以下工作：

1. 根据题材宽度、梯队、昨日延续和当前扩散，决定哪些题材值得继续分析；
2. 将入围的 1–3 个题材一次传给 `scripts/fetch_sector_boards.py --search ... --json`，检查每行 `source_board_code` 与 `downstream_supported`，再只对同源 `BK` 调用 `scripts/fetch_sector_leaders.py --json`；同一份完整成分股事实同时用于扩散统计和第二梯队初筛；
3. 只对每个题材的前 1–2 只候选搜索最新消息，并调用新闻、资金流报告；
4. 根据洗盘、筹码、梯队、题材趋势和消息硬门槛，决定“可打板/只观察/排除”；
5. 一个题材只保留一只，写入固定报告。

题材强度分仍由模型按 100 分规则研判。必须结合 `field_quality` 和原始字段标明 `exact / partial / unavailable`；只有 `exact` 能通过 80/85 硬门槛，`partial` 只能“只观察”，不得按剩余满分归一后放行。

第二梯队使用相同数据链路但不套用打板分数：

1. 先按 `core / ladder / D` 计算准入；`D=上涨/(上涨+下跌+平盘)`，只接受 `diffusion.sample_complete=true` 的同源完整样本；
2. 用 `fetch_sector_leaders.py --json` 通读题材内全部主板、未涨停、仍在交易成分股；`--top` 不参与扩散率计算；
3. 模型先按同题材逻辑、执行时点量价和相对强弱保留每题材 2–5 只，并按代码去重；
4. 父模型先冻结本批 `data_date`、快照时间和题材/BK 上下文；同一代码只评估一次；
5. 每只初筛股票启动一个独立子代理，完整执行单股审视流程：`prepare_candidates.py --stock <代码> --json`、目标股题材扩散、新闻公告、资金和六维判断；
6. 子代理只返回单股证据与“第二梯队候选/只观察/排除”，不横向比较、不写最终报告、不创建状态文件；
7. 父模型汇总后检查行情时点一致性，再按题材横向比较并选优；
8. 环境不支持子代理时报告限制并询问是否允许串行；获准后才能降级并注明。

脚本的排序、板块分和候选指标只提供事实，不得直接决定第二梯队；子代理结论也不能按数量或简单多数机械汇总，最终取舍由父模型完成。

单股模式下，模型只对目标股相关的 1–3 个精确题材补板块扩散，只对目标股补新闻和资金；随后按价格承接、量能、筹码、题材、消息和市场六类证据，给出“继续持有 / 条件持有 / 不继续持有”。不得把高开上涨等同于健康，也不得用盘中部分量充当全天量能。

不要把脚本已经算好的字段再逐只手工计算，也不要让脚本决定买卖结论。

## 主要输出字段

- `data_date`、`previous_date`、`quote_date`、`quote_time`；
- `board_permission`、`board_permission_excluded_count`：固定沪深主板权限及被排除的一/二板数量；
- `suspension_filter`：停牌最高板、触发的行业/精确概念、连带排除数及逐股排除明细；
- `rules`：脚本内置规则名称、三个规则开关及 `second_line`、`theme_score`、`market_gate` 口径；
- `market_gate`：`breadth_collapse`、`leader_collapse`、`retreat`、`retreat_confirmed`、`promotion_sample_time`、情绪状态、动作和数量上限；
- `one_word_filter`：一字板排除数和逐股原因；保留候选仍在 `candidates`；
- `report_path`：由 `data_date` 生成的 `report/打板候选/YYYY-MM-DD.md`；
- `previous_source`、`previous_pool_report`、`previous_pool_generated`（固定 `false`）、`previous_context`；
- `current_pool_report`、`current_pool_generated`：固定为 `null`/`false`，运行时只处理实时快照，不落盘涨停池；
- `market`：当日概览；
- `market.max_board`、`market.two_plus_count`、`market.board_counts`：当前连板高度结构；
- `yesterday_open`：`status`、总样本、有效开盘、未撮合、行情缺失、停牌和高开/平开/低开分布；未撮合时计数与均值可为 `null`；
- `yesterday_highest_board`：昨日最高板及今日表现，每项含行业、精确概念/名称组、停牌状态；
- `mode_feedback`：昨日一进二、二进三的有效样本、停牌数、晋级率、高开率和平均开盘/收盘；
- `themes`：题材聚合与板数结构；
- `candidates`：所有当日 1/2 板候选及原始指标；
- `stock_review`：单股模式目标、当日状态、报价、量价、筹码、股东户数和数据限制；批量模式为 `null`；
- `minutes_since_first_seal`、`minutes_since_last_seal`、`is_reseal`：判断是否刚封、刚回封或封板已久；
- `limitations`：口径限制。

`--compact` 顶层保留 `schema`、行情日期、上游来源、市场情绪、分模式反馈、题材聚合、扁平化候选、指标质量摘要和限制；默认只列前 30 个题材并给出 `theme_total_count`、`themes_truncated`，完整题材列表使用 `--json`。候选不再嵌套完整 `metrics`，百分数保留 2 位。

一字板候选额外输出 `one_word`、`board_amount_yi`、`seal_fund_yi`、`seal_fund_ratio_pct`、`seal_fund_over_float_pct` 和 `fill_probability`。`fill_probability` 的固定口径：板上成交额 ≥0.5 亿元且封单/成交额 ≤100% 为“高”；成交额 ≥0.2 亿元且封单/成交额 ≤200% 为“中”；板上成交额大于 0 的其他可执行情形为“低”。它只是排板辅助判断，不改变零成交、封单缺失/为零的硬过滤。

单股 `--compact` 的 `schema` 为 `limit-hit.stock-review.compact.v1`，只保留目标相关题材和目标候选（若目标当日属于 1/2 板候选）；`stock_review` 包含 `current_limit_status`、当前/昨日板数、`quote_consistent`、`intraday.is_partial_day`、`volume_ratio_scope`、`volume_vs_prev_day`、均线、收益和筹码。完整报价与原始指标使用 `--json`。

板块权限固定为沪深主板：沪市 `600/601/603/605`，深市 `000/001/002/003`。创业板、科创板和北交所股票不会进入 `candidates` 或题材的 `candidate_codes`。停牌最高板同行业或共享精确概念的候选也不会进入 `candidates` 或 `candidate_codes`，但 `themes[].current_count` 和板数结构仍保留全市场涨停事实。

停牌只按报价型启发式识别：个股行情日等于 `data_date`，且今开、最高、最低、成交量均为 0。股票不在行情响应中或字段完全缺失时是“行情缺失”，不是停牌。盘前零成交也不在 `limit-hit` 合法运行窗口内，不得复用 `suspended` 状态。

候选的 `metric_quality`：

- `exact`：东财日 K 与精确换手率可用；
- `degraded`：东财日 K 不可用，腾讯日 K 与当前流通股本近似换手率，筹码只能作辅助证据；
- `unavailable`：日 K 或筹码未获取。

字段缺失就按缺失处理，脚本不会填 0，也不会用本机时间替代行情时间。
开板次数的真实 `0` 与未获取的 `null` 必须保持区别；行情时间一律按北京时间判断。
