# hit-it

面向 A 股短线结构分析的自包含项目，提供 `limit-hit`、`leader-hit` 两个 Codex skill、运行期取数脚本和单元测试。

- `limit-hit`：筛选一进二、二进三打板候选，研判通过独立准入的题材中尚未涨停的第二梯队，也可按名称或代码审视单股是否继续持有；
- `leader-hit`：筛选低位建仓完成、洗盘中、待启动或刚启动且尚未高位的沪深主板股票。

项目不负责资金分配、仓位管理或账户下单。所有报告均不构成投资建议。

当前版本：1.7.1 · [查看发布记录](https://github.com/ddd-online/hit-it/releases)

## 能力边界

- 打板名单只处理当日已经涨停后的 `1→2`、`2→3`；三板及以上、跌停反核不进入打板名单。
- 第二梯队名单只从 `core AND (ladder OR D>=30%)` 的同一精确题材中，选择执行时点仍未涨停且可成交的沪深主板成分股；`D` 使用完整同源成分股并计入平盘。
- 单股持仓审视只支持沪深主板，不要求当日涨停；可检查价格承接、量能、筹码、题材、消息和市场环境。
- 默认最多保留 3 个题材，一个题材最多进入一只可打板候选。
- 打板名单状态为“可打板 / 只观察 / 排除”；第二梯队状态为“第二梯队候选 / 只观察 / 排除”；单股持仓结论为“继续持有 / 条件持有 / 不继续持有”。
- 消息无法核验的股票不能进入“可打板”或“第二梯队候选”；退潮期第二梯队状态上限固定为“只观察”。

## 目录结构

```text
hit-it/
├── limit-hit/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
├── leader-hit/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
└── tests/
```

`limit-hit/SKILL.md` 和 `leader-hit/SKILL.md` 分别是两个运行入口。各自的 `references/` 只提供运行期筛选细则和报告模板。

## 环境要求

- Python 3。
- 可访问东方财富、腾讯、新浪和同花顺公开接口。
- 只使用 Python 标准库，不需要安装第三方包或配置 API 密钥。

运行期脚本强制直连并绕过系统代理。网络层和跨源兜底逻辑位于 `limit-hit/scripts/http_util.py` 与 `limit-hit/scripts/fallback_sources.py`。

## 快速开始

项目根目录适合开发和测试；实际候选报告和涨停池文件从当前工作目录读写。因此生成交易工作区报告时，应先进入目标工作区，再调用本项目的脚本。板块权限固定为沪深主板。

PowerShell 示例：

```powershell
cd <目标交易工作区>
python D:\github\hit-it\limit-hit\scripts\save_limit_pool.py
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --compact
```

规则固定内聚在 `prepare_candidates.py`，直接运行即可：

```powershell
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --compact
```

固定规则允许可核验且有板上成交的一字板进入比较，关闭仅由跌停家数触发的宽度崩塌否决，并在偏弱市场对题材强度 ≥80 的候选使用 80/79 结构分门槛。正常最高板天地板、跌停或行情缺失仍不可关闭。

指定股票名称或代码做持仓审视：

```powershell
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --stock 002520 --compact
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --stock 日发精机 --json
```

单股报告写入 `report/个股审视/<data_date>_<code>.md`。盘中量能会标“盘中部分量”，不会把截至快照的成交量冒充全天量能。

先执行保存脚本应在每个交易日收盘后完成；缺历史文件时可用 `--date YYYYMMDD` 指定日期。候选预计算运行时会拉取当日实时涨停池，但不会自行拉取、回补或生成昨日涨停池。

日常运行使用 `--compact` 获取模型友好的单行 JSON，默认列前 30 个题材；需要审计完整题材和原始字段时使用 `--json`，去掉两者输出文本表。脚本返回的 `report_path` 是唯一报告路径，通常为：

```text
report/打板候选/<data_date>.md
```

涨停池由 `save_limit_pool.py` 独立保存在 `report/涨停池/<date>.json`，已有文件不覆盖。SKILL 运行时只读取昨日文件；文件缺失时停止并提示先补存，不再自动使用接口昨日池。

`data_date` 取行情实际日期，不取执行日期。凌晨、跨日或非交易日运行时，不得用本机日期改写报告文件名。

## 运行流程

1. 收盘后保存当日涨停池：

```powershell
python D:\github\hit-it\limit-hit\scripts\save_limit_pool.py
```

2. 运行 SKILL 时拉取当日实时涨停池，并读取已保存的昨日池、计算题材和候选指标：

```powershell
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --compact
```

3. 对入围题材定位概念板块：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_sector_boards.py --type concept --search 机器人 人工智能 --top 20 --json
```

4. 下钻板块成分股。第二梯队使用完整 JSON 通读全部成分股，不用涨幅榜顺序直接选股：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_sector_leaders.py --board BK1145 --top 300 --json
```

板块榜必须检查 `downstream_supported`。腾讯 `pt02...`、F10 降级、分页不完整或跨源成分股时失败关闭，不得给第二梯队候选。成分股 `--top` 只限制展示，`diffusion` 来自完整扫描。

5. 对初筛股票批量补历史、均线和筹码；随后每只初筛股票默认交给一个独立子代理，完整执行单股评估。下面的单股命令由各子代理分别执行：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_stock_metrics.py --codes sh600410,sz002491 --days 120 --workers 8 --json
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --stock sh600410 --json
```

6. 对少量候选补查消息、资金和股东结构：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_news.py sh600410 --news 10 --ann 10 --json
python D:\github\hit-it\limit-hit\scripts\fetch_capital_flow.py sh600410 --json
python D:\github\hit-it\limit-hit\scripts\fetch_stock_structure.py sh600410 --json
```

一条命令可同时定位多个题材，复用同一份板块扫描结果。`prepare_candidates.py` 和取数脚本负责可复现的数据装配；第二梯队每只初筛股票由独立子代理走完整单股审视流程，只回报单股证据和候选状态。父模型负责检查行情时点一致性、横向比较、题材选优和最终落盘。第二梯队不套用机械总分，结论依据执行时点量价、市场情绪、历史涨跌幅、筹码结构和基本面地雷综合判断。

## leader-hit 快速开始

查找沪深主板 20 元以下股票：

```powershell
python D:\github\hit-it\leader-hit\scripts\prepare_leader_candidates.py --price-max 20 --universe-only --compact
```

结构初筛（默认从低价主板中选快照预排序前 120 只，再拉历史行情）：

```powershell
python D:\github\hit-it\leader-hit\scripts\prepare_leader_candidates.py --price-max 20 --compact
```

判断指定股票：

```powershell
python D:\github\hit-it\leader-hit\scripts\prepare_leader_candidates.py --codes 600000,000001 --compact
```

输出固定状态为“建仓完成 / 洗盘中 / 待启动 / 启动初段 / 只观察 / 排除”。技术结构通过后，仍需核验精确概念、板块扩散、新闻和公告。

## 脚本说明

| 脚本 | 用途 |
|---|---|
| `save_limit_pool.py` | 收盘后保存当日涨停池，支持 `--date YYYYMMDD` |
| `prepare_candidates.py` | 预计算批量候选上下文；`--stock` 审视指定个股的报价、量价、筹码和持有条件 |
| `fetch_limit_up.py` | 当日及昨日涨停、跌停、炸板池和昨日涨停今日表现；`--current-only` 供 SKILL 使用 |
| `fetch_limit_up_history.py` | 保存脚本按指定日期回补历史涨停成功池 |
| `fetch_stock_structure.py` | 概念归属、股东户数、日K与筹码推演 |
| `fetch_stock_metrics.py` | 批量日K趋势、均线、量比、开盘差和筹码指标 |
| `fetch_sector_boards.py` | 概念/行业板块行情榜，用于定位 BK 代码 |
| `fetch_sector_leaders.py` | 板块成分股行情榜，用于判断题材扩散 |
| `fetch_news.py` | 东方财富新闻与公告 |
| `fetch_capital_flow.py` | 个股主力及超大单/大单/中单/小单资金流向 |

脚本不读取规则配置文件，也不接受 `--profile` 入参；所有规则常量直接位于运行脚本内。
| `fetch_quote.py` | 腾讯个股行情快照与日K |
| `fallback_sources.py` | 腾讯、新浪、东财 F10 等备用数据源 |
| `http_util.py` | 强制直连的公共网络层 |
| `leader-hit/scripts/prepare_leader_candidates.py` | 沪深主板低价股、日K、筹码、量能、箱体和左侧涨停结构初判 |
| `leader-hit/scripts/market_data.py` | `leader-hit` 自带的行情、日K与筹码推演层 |

## 数据口径

- 涨停池主要来自东方财富 `push2ex`，涨停池不含 ST 与科创板。
- 概念、股东户数和板块成分股使用东方财富 F10 口径。
- 个股日K优先东方财富；不可用时回退腾讯，近似换手率必须标 `≈`。
- 候选会输出 `metric_quality`；`degraded` 的筹码只能作为辅助证据，不能按精确值计分。
- 板块行情优先东方财富；不可用时回退腾讯，板块代码和资金口径会不同。
- 东财 `f109` 与 `f160` 整批同值时，10 日涨幅输出 `null`。
- 个股资金流优先东方财富；不可用时回退新浪，拆分字段标“未获取”。
- 历史涨停成功池可由同花顺回补，但不包含炸板池、跌停池和市场封板率；“几天几板”不是当前连板数。
- 跨数据源数值不可放在同一比较表中混用，报告必须标注实际数据源与口径。
- 筹码分布是日K换手率衰减与三角分布推演值，不是券商原始筹码。

接口未返回数据时使用 `None` 或 `-`，并标“未获取”；不得填 0，也不得用本机时间或最近一根K线冒充目标日数据。

## 测试

在项目根目录运行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q limit-hit/scripts
```

涉及取数代码时，还应至少运行一次真实数据，并分别验证文本和 `--json` 输出可解析。

## 报告要求

- 报告使用中文，数字进入表格，状态使用固定标记，不使用表情符号。
- 报告固定包含“上游复用”“昨日涨停今日集体开盘”“次日 9:25 开盘退出复核”。
- 报告必须写清数据日期、执行时间、盘中快照或收盘复盘口径。
- 最终回复还需提供适合手机阅读的简短摘要。
- 所有报告结尾必须写“不构成投资建议”。

更完整的业务规则以 `limit-hit/SKILL.md` 及其 `references/` 为准；代码修改、测试和文档维护约束见 `AGENTS.md`。
