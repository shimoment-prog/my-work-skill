---
name: joinquant-strategy
description: >-
  根据自然语言投资逻辑查阅聚宽 API 并生成可回测的 JoinQuant 策略脚本。
  Use when the user describes 聚宽/JoinQuant 选股买卖逻辑、要求写策略脚本、
  回测代码、initialize/run_daily/order_target，或提到涨停、首板、等权、调仓等策略需求。
---

# 聚宽策略脚本生成

将用户用语言描述的投资逻辑，转为可直接粘贴到聚宽研究/回测平台的 Python 策略。

## 何时使用

- 用户用中文/英文描述股票池、选股条件、买入、卖出规则
- 要求「写一个聚宽策略」「根据逻辑生成 JoinQuant 脚本」
- 需要对照聚宽 API 实现涨停、市值、基本面、定时调仓等逻辑

## 工作流（必须按序执行）

复制进度并勾选：

```
进度:
- [ ] 1. 解析逻辑五要素
- [ ] 2. 查阅聚宽 API（禁止凭记忆编造签名）
- [ ] 3. 消歧（仅在影响实现时提问）
- [ ] 4. 生成完整策略脚本并落盘
- [ ] 5. 输出逻辑对照表与回测注意点
```

### 1. 解析逻辑五要素

从用户描述中抽出并结构化：

| 要素 | 要明确的内容 |
|------|----------------|
| 股票池 | 标的范围 + 过滤（ST/科创板/北交所/次新/停牌等） |
| 选股条件 | 多条件是「同时」还是「任一」；涉及的时间基准（昨收/今开/近 N 日） |
| 买入方式 | 等权/固定只数/目标仓位；用现金还是总资产 |
| 卖出方式 | 触发时点（开盘/盘中/尾盘）、盈亏条件、持有天数 |
| 调度时点 | 每天何时选股、何时买卖（映射到 `run_daily`） |

时间线必须写成交易日序列，例如：`T-1 涨停 → T 低开买入 → T+1 卖出`。

### 2. 查阅聚宽 API

**禁止**凭记忆编造函数名、参数、返回字段。写代码前至少查阅下列之一：

- 在线文档：https://www.joinquant.com/help/api/help#name:api
- PDF：https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf
- 本 skill 速查：[api-cheatsheet.md](api-cheatsheet.md)

页面若为前端渲染抓不全正文，改用 PDF / 速查表，或用搜索定位具体 API 说明。

常用映射见速查表；不确定的 API 先查再写。

### 3. 消歧规则

仅当歧义会改变下单结果时才提问，例如：

- 「低开 -3% 到 -4%」是闭区间还是开区间
- 「相对低位」用昨收还是今开作比较价
- 「赚钱」是相对成本、昨收还是开盘价
- 等权是按可用现金还是总资产

无歧义则采用合理默认，并在回复中简短注明。

### 4. 生成脚本

输出要求：

1. **完整可运行**：含 `initialize`；优先 `run_daily`，避免与 `handle_data` 混用
2. **默认设置**：
   - `set_benchmark('000300.XSHG')`
   - `set_option('use_real_price', True)`
   - `set_option('avoid_future_data', True)`
   - 常规股票佣金（买万三、卖万三+印花税千一、最低 5 元），用户另有规定除外
3. **结构清晰**：股票池过滤 / 信号判断 / 买入 / 卖出 分函数
4. **防未来函数**：历史用 `attribute_history` / `get_price`（不含当日）；当日开盘价、涨跌停、停牌用 `get_current_data()`
5. **落盘**：写入用户指定路径；未指定则写到当前项目合适目录（如 `test/`），文件名反映策略含义
6. **注释**：文件头用 3～5 行概括选股与买卖；关键条件旁加简短中文注释

骨架模板：

```python
def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    # set_order_cost(...)
    # g.xxx = ...
    run_daily(sell_logic, time='09:31')
    run_daily(buy_logic, time='09:32')
    # 如有尾盘卖出: run_daily(close_sell, time='14:50')

def filter_stock_pool(context, stock_list):
    ...

def check_signal(context, stock):
    ...

def buy_logic(context):
    ...

def sell_logic(context):
    ...
```

买卖时点冲突时：先卖后买（先释放现金再开仓）。

### 5. 回复用户

简洁说明：

1. 脚本路径
2. **逻辑对照表**（用户原话 → 代码实现）
3. 回测建议（需要盘中价/开盘价判断时建议**分钟**频率；纯日线条件可用日频）
4. 已采用的默认假设（若有）

## 常见语义 → 实现约定

| 用户说法 | 默认实现 |
|----------|----------|
| 过滤 ST | `get_current_data()[s].is_st` 或名称含 `ST` |
| 过滤科创板 | 代码以 `688` 开头 |
| 过滤北交所 | 代码以 `.XBEI` 结尾 |
| 过滤次新股（上市未满一年） | `(today - get_security_info(s).start_date).days < 365` |
| 涨停 | `close >= high_limit * 0.997` |
| 首板 | 昨日涨停且前日未涨停 |
| 等权买入全部候选 | `available_cash / n` + `order_value` |
| 持有 N 只 | `head(N)` 或打分后取 TopN + `order_target_value` |
| 次日上午盈利就卖 | `run_daily(..., '09:31')`，`last_price > avg_cost` |
| 尾盘清仓 | `run_daily(..., '14:50')` + `order_target(s, 0)` |

更多 API 细节见 [api-cheatsheet.md](api-cheatsheet.md)。完整示例见 [examples.md](examples.md)。

## 质量检查（生成后自检）

- [ ] 无未来函数（未在当日收盘前使用当日收盘价做决策，除非逻辑明确允许）
- [ ] 停牌、涨跌停无法成交已处理或已注释风险
- [ ] 卖出不会误卖「当日新买入」仓位（若持有期跨日）
- [ ] 区间比较方向正确（如 -4%～-3% → `[-0.04, -0.03]`）
- [ ] 未混用 `handle_data` 与 `run_daily`
