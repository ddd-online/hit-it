# hit-it

面向 A 股短线涨停接力的自包含项目，提供 `limit-hit` Codex skill、运行期取数脚本和单元测试。

项目只筛选一进二、二进三打板候选，不负责资金分配、仓位管理或账户下单。所有报告均不构成投资建议。

当前版本：1.0.0 · [查看发布记录](https://github.com/ddd-online/hit-it/releases)

## 能力边界

- 只处理当日已经涨停后的 `1→2`、`2→3`。
- 三板及以上、跌停反核、未涨停埋伏不属于本项目。
- 默认最多保留 3 个题材，一个题材最多进入一只可打板候选。
- 最终状态固定为“可打板 / 只观察 / 排除”。
- 消息无法核验的股票不能进入“可打板”。

## 目录结构

```text
hit-it/
├── limit-hit/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
└── tests/
```

`limit-hit/SKILL.md` 是运行入口。`references/` 提供运行边界、筛选细则、上游报告复用和报告模板。`scripts/` 包含全部取数、指标计算和预计算代码。

## 环境要求

- Python 3。
- 可访问东方财富、腾讯、新浪和同花顺公开接口。
- 只使用 Python 标准库，不需要安装第三方包或配置 API 密钥。

运行期脚本会优先直连，连续失败后回退系统代理。网络层和跨源兜底逻辑位于 `limit-hit/scripts/http_util.py` 与 `limit-hit/scripts/fallback_sources.py`。

## 快速开始

项目根目录适合开发和测试；实际报告、昨日复盘报告和 `ACCOUNT.md` 从当前工作目录读取。因此生成交易工作区报告时，应先进入目标工作区，再调用本项目的脚本。

PowerShell 示例：

```powershell
cd <目标交易工作区>
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --json
```

默认输出完整 JSON；去掉 `--json` 输出紧凑文本。脚本返回的 `report_path` 是唯一报告路径，通常为：

```text
report/打板候选/<data_date>.md
```

`data_date` 取行情实际日期，不取执行日期。凌晨、跨日或非交易日运行时，不得用本机日期改写报告文件名。

## 运行流程

1. 建立当日与昨日样本、计算题材和候选指标：

```powershell
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --json
```

2. 对入围题材定位概念板块：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_sector_boards.py --type concept --search 机器人 --top 20 --json
```

3. 下钻板块成分股：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_sector_leaders.py --board BK1145 --top 300 --json
```

4. 对少量候选补查消息、资金和股东结构：

```powershell
python D:\github\hit-it\limit-hit\scripts\fetch_news.py sh600410 --news 10 --ann 10 --json
python D:\github\hit-it\limit-hit\scripts\fetch_capital_flow.py sh600410 --json
python D:\github\hit-it\limit-hit\scripts\fetch_stock_structure.py sh600410 --json
```

`prepare_candidates.py` 负责可复现的数据装配和指标计算；题材取舍、消息核验、评分与最终状态由运行 SKILL 的模型完成。

## 脚本说明

| 脚本 | 用途 |
|---|---|
| `prepare_candidates.py` | 预计算市场情绪、昨日反馈、题材梯队、候选日K、量能和筹码 |
| `fetch_limit_up.py` | 当日及昨日涨停、跌停、炸板池和昨日涨停今日表现 |
| `fetch_limit_up_history.py` | 昨日报告不可用时回补历史涨停成功池 |
| `fetch_stock_structure.py` | 概念归属、股东户数、日K与筹码推演 |
| `fetch_stock_metrics.py` | 批量日K趋势、均线、量比、开盘差和筹码指标 |
| `fetch_sector_boards.py` | 概念/行业板块行情榜，用于定位 BK 代码 |
| `fetch_sector_leaders.py` | 板块成分股行情榜，用于判断题材扩散 |
| `fetch_news.py` | 东方财富新闻与公告 |
| `fetch_capital_flow.py` | 个股主力及超大单/大单/中单/小单资金流向 |
| `fetch_quote.py` | 腾讯个股行情快照与日K |
| `fallback_sources.py` | 腾讯、新浪、东财 F10 等备用数据源 |
| `http_util.py` | 直连优先、系统代理兜底的公共网络层 |

## 数据口径

- 涨停池主要来自东方财富 `push2ex`，涨停池不含 ST 与科创板。
- 概念、股东户数和板块成分股使用东方财富 F10 口径。
- 个股日K优先东方财富；不可用时回退腾讯，近似换手率必须标 `≈`。
- 板块行情优先东方财富；不可用时回退腾讯，板块代码和资金口径会不同。
- 个股资金流优先东方财富；不可用时回退新浪，拆分字段标“未获取”。
- 历史涨停成功池可由同花顺回补，但不包含炸板池、跌停池和市场封板率。
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
