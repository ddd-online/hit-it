# 项目代理说明

本目录独立维护 `limit-hit` 打板候选 skill。目标是生成可复核的一进二、二进三候选报告，不执行资金、仓位或账户操作。

## 开始工作前

1. 先读 `limit-hit/SKILL.md`，确认当前任务属于分析、脚本维护、测试还是报告生成。
2. 涉及具体筛选时，按任务读取对应的 `limit-hit/references/*.md`，不要一次加载全部参考文档。
3. 修改代码前运行 `python -m unittest discover -s tests -v` 获取基线。
4. 运行交易报告前确认当前工作目录是目标交易工作区；脚本使用当前目录读取 `ACCOUNT.md`、`report/` 并生成 `report_path`。

## 项目结构

```text
hit-it/
├── limit-hit/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/
│   └── scripts/
└── tests/
```

- `limit-hit/SKILL.md` 是唯一业务入口。
- `limit-hit/references/` 存放按需加载的筛选细则、运行边界和报告模板。
- `limit-hit/scripts/` 是完整运行期取数层，不依赖项目外部的 `market-data`。
- `tests/` 覆盖预计算路径、报告解析、题材聚合和脚本自包含性。

## 核心边界

- 只处理当日 `board_count` 为 1 或 2 的股票。
- 默认最多 3 个题材，每题材最多一只可打板候选；用户可修改数量。
- 最终状态只能是“可打板 / 只观察 / 排除”。
- 消息无法核验时只能“只观察”。
- 不读取 `MUST.md`、`NOTES.md`、`POSITION.md` 或个股目录。
- 若工作区存在 `ACCOUNT.md`，只读取“板块权限”，不读取其他内容。
- 不创建观察池或额外状态文件。
- 本 skill 不做下单、仓位和资金调度。

## 命令与工作目录

代码测试在项目根目录运行：

```powershell
python -m unittest discover -s tests -v
```

生成交易报告时先进入目标交易工作区，再调用脚本：

```powershell
python D:\github\hit-it\limit-hit\scripts\prepare_candidates.py --compact
```

不要假设报告应写入代码项目根目录。`prepare_candidates.py` 中的 `report_path` 以当前工作目录为基准。

## 运行期硬规则

- 当日涨停必须由 `limit-hit/scripts/fetch_limit_up.py --json` 现场获取，不能用旧报告冒充当日。
- 先运行 `prepare_candidates.py --compact`，模型不得重复手工计算脚本已经给出的涨停数、晋级数、板数、题材聚合和候选指标；需要审计原始字段时改用 `--json`。
- `data_date` 是行情实际日期，`generated_at` 是执行时间，两者不得混用。
- `quote_date != data_date` 时，本轮接力反馈不是当日口径；标“数据未获取”，停止给出可打板结论。
- 报告文件名使用 `data_date`，固定写入 `report/打板候选/<data_date>.md`，同一数据日重跑覆盖更新。
- 昨日涨停优先复用日期早于当前数据日的最近一份 `report/涨停板复盘/YYYY-MM-DD.md`。
- 昨日报告缺失、日期不匹配或关键字段不全时，使用 `fetch_limit_up_history.py --date <prev_date> --json` 回补。
- 只对入围的 1–3 个题材补查板块扩散；只对每个题材筛出的 1–2 只股票补查消息和资金。
- `fetch_news.py` 只补充公司新闻和公告，不能替代题材级网页搜索。
- 最新催化必须记录标题、原始来源、发布时间和链接；二手转述必须回溯原始来源。
- 筹码字段必须标“日K+换手率推演值”，股东户数必须标明截止日和披露滞后。

## 数据与网络

- 取数代码只使用 Python 标准库。
- 真实数据只经 `limit-hit/scripts/` 获取，不新增外部项目路径依赖。
- 网络请求统一走 `http_util.py`，强制直连并绕过系统代理。
- 主数据源和备用源必须保留，不得因主接口暂时可用而删除回退逻辑。
- 东方财富节点包括 `push2.eastmoney.com`、`push2delay.eastmoney.com` 和 `push2test.eastmoney.com`。
- `push2test` 的日K路径可能返回空数据，个股日K必须保留腾讯回退。
- 东财板块 `f160` 与 `f109` 可能逐行同值，10 日涨幅不可直接信任；需要时使用腾讯日K回算。
- 东财部分节点的 `f109/f160/f10` 可能整列为 `-`，输出必须标“未获取”。
- 跨数据源数值不可混比；报告必须记录 `source`、`source_name` 和限制。
- 接口无数据时使用 `None` 或 `-`，并标“未获取”；不得填 0，不得用本机日期或最近K线冒充目标日期。
- 接口返回真实 0 与“未获取”必须区分。

## 股票与题材判断

- 题材优先使用精确概念代码聚合，不用名称模糊匹配代替。
- 同题材其余股票不跟随、只有候选上涨时，判“独涨题材”，排除。
- 全天一字且无法成交的股票不能写成“可打板”。
- ST、退市整理、停牌、权限不足和可核验重大负事件必须硬过滤。
- 基本面不按营收、净利、负债率和估值常规加减分，只检查造假、立案、退市、处罚、业绩暴雷和正在执行的大额减持或解禁。
- 昨日最高板出现天地板、跌停或行情缺失时，当天只能输出“只观察”。
- 用户给出的个人阈值优先于 `selection-rules.md` 的默认值，并写入报告口径。

## 报告与回复

- 报告使用中文，不使用表情符号。
- 数字进入表格，趋势用箭头，状态使用固定标记。
- `可打板名单`表头不得修改。
- 报告必须保留“上游复用”“昨日涨停今日集体开盘”“次日 9:25 开盘退出复核”三节。
- “可打板”必须同时写执行触发和失效条件。
- 盘中未收盘时，次日竞价分母标“待收盘确认”。
- 最终对话必须提供手机可读摘要，包含数据日期、执行时间、市场情绪、可打板或只观察名单、报告路径。
- 所有报告和最终回复必须写“不构成投资建议”。

## 修改与验证

- Python 使用 4 空格缩进，函数和变量使用 snake_case，文件编码为 UTF-8。
- 文档与注释使用中文。
- 不引入第三方依赖，不新增密钥，不写缓存数据。
- 修改取数接口、字段或口径时，同步检查 `prepare_candidates.py`、相关 reference、报告模板和测试。
- 新分支、循环、解析器或金额计算应有最小单测或自检。
- 修改后至少运行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q limit-hit/scripts
```

- 修改取数脚本后，还要用真实标的验证文本和 `--json` 两条分支。
- 修改 `SKILL.md` 后，运行 skill 结构校验：

```powershell
python -X utf8 C:\Users\ljw\.codex\skills\.system\skill-creator\scripts\quick_validate.py D:\github\hit-it\limit-hit
```

- 不提交 `__pycache__/`、`*.pyc`、临时输出或测试缓存。
