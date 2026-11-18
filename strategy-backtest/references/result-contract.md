# 统一回测结果契约

策略适配器输出一个 UTF-8 JSON，传给 `scripts/render_review.py --result`。它既是绘图输入，也是逐笔审计凭据。指标和交易计算由适配器完成；renderer 不识别某个固定策略名称，也不推断 EMA、RSI 或固定止损。

## 顶层字段

`schema_version` 为 `1`，包含 `meta`、`candles`、`indicators`、`trades`。`summary` 由 renderer 核算生成，不拿输入的汇总代替逐笔计算。所有时间为 UTC 秒；`meta.timezone` 指显示时区，不改变原始时间。

## meta

| 字段 | 内容 |
| --- | --- |
| `strategy_name` / `strategy_description` | 策略名称和本次执行规则文本 |
| `parameters` / `costs` | 可选，策略参数和成本模型对象；实际指标窗口、费率、滑点、资金费等在报告展示 |
| `symbol` / `market` / `currency` | 实际品种、市场、记账币种 |
| `price_currency` / `quantity_unit` | 可选，报价币种与数量单位；报价币种缺省同账户币种，跨币种换算由引擎实现 |
| `interval` / `timezone` | 如 `5m` 与 `Asia/Shanghai`；数据可按对应交易日历有合法休市缺口 |
| `start_time` / `end_time` | 本次区间边界，结束边界的包含关系在规则中写明 |
| `initial_capital` | 正数，用户本金 |
| `sizing` | `mode`、`value`、`leverage`、`description`，金额含义必填 |
| `source_description` / `limitations` | 来源证据说明、实际未覆盖机制列表 |
| `price_step` / `quantity_step` | 可选，本次品种精度，不能默认套 ETH 精度 |
| `accounting_mode` | `closed_pnl` 或 `account_snapshots`，见下文 |
| `balance_basis` / `balance_description` | 快照采用 `cash` 或 `equity`，以及读者应如何理解交易后金额 |
| `final_equity` | 可选，由引擎给出含期末持仓浮盈亏与已发费用的权益 |

其它策略参数、费用、数据哈希、订单模型与撮合路径可以作为额外元数据保留。`sizing.mode` 支持 `fixed_margin`、`fixed_notional`、`equity_percent`、`quantity`、`risk_amount`。renderer 不用这些参数重新下单或重复乘杠杆，只展示和核对适配器计算。

## candles 与 indicators

`candles` 每行为 `time,open,high,low,close`，可加 `volume`；`time` 是开盘时间，严格递增，OHLC 正数且高低范围有效。数据完整性按市场日历验证，不能统一强制 24 小时连续。需要时可添加 `end_time` 记录实际收盘边界。

`indicators` 是任意列表，也可为空。每项包含 `id,name,pane,values`，可加 `color` 与 `levels`。`pane` 是 `price` 或 `oscillator`；`values` 为递增的 `{time,value}` 列表，预热未就绪的点省略。价格指标画在 K 线区，副图指标各有独立坐标。不同指标的计算和确认规则由策略实现负责。

## trades

每笔包含下表字段。完全平仓的一笔对应一个净损益；分批交易按引擎定义分组，并在元数据解释交易计数方式。

| 字段 | 内容 |
| --- | --- |
| `id` / `direction` / `status` | 唯一编号；`long` 或 `short`；`closed` 或 `open` |
| `entry_time` / `entry_price` / `quantity` | 实际入场时间、正价格与正数量 |
| `key_time` | 可选，产生信号的 K 开盘时间；没有关键 K 概念时为空 |
| `stop` / `target` | 初始止损止盈，可为 `null`，不补造价格 |
| `risk_levels` | 可选，动态轨迹 `{time,stop,target}`，没有新值可省略对应端 |
| `exit_time` / `exit_price` | 已平仓的实际退出时间和价格；精确时间未知时 `exit_time` 可空 |
| `exit_bar_start` / `exit_bar_end` | 精确退出时间未知时，提供包含退出的 K 时间范围，不伪造秒级成交 |
| `entry_notional` / `margin_used` / `leverage` | 可选，实际名义金额、保证金与杠杆 |
| `gross_pnl` / `fees` / `other_costs` / `net_pnl` | 毛盈亏、费用、其它已计成本和净额；成本不是再次乘杠杆的地方 |
| `balance_after` | 退出后的已实现累计余额或事件账户快照，必须遵守账户模式 |
| `exit_reason` | 实际退出原因，指标退出、追踪止损等均可 |
| `checks` | 本策略条件 `{label,value,pass}`，`value` 写出实际值与条件；不能只写固定“通过” |

已平仓净盈亏 = 毛盈亏 − 费用 − 其它成本。renderer 按净额分 Win、Loss、Flat，不以退出原因为胜负依据。手续费、资金费等需按引擎实际定义填入并解释，不能遗漏或重复扣除。收入性返佣或收到的资金费可作为带符号其它成本并明确说明。

Open 单的 `exit_time,exit_price,gross_pnl,net_pnl,balance_after` 为 `null`，`fees` 可记录已发生的开仓费；未实现损益不能当作已平仓盈利。`key_time` 与 `entry_time` 分开，不能从成交点所在下一根 K 的颜色推断信号 K。

## 账户模式

`closed_pnl` 适用于串行交易的已平仓累计账单：`balance_after = initial_capital + 累计已平仓净额`。这是已实现累计余额，不等于有其它持仓时的现金或总权益。没有其它仓位、资金流或未平费用影响时，才可与期末账户金额直接比较。

多仓、分批成交或复杂资金流采用 `account_snapshots`：附 `account_events` 数组，每项 `{id,time,cash,equity}`。已平仓行的 `account_event_id` 指向实际退出事件；`balance_after` 必须等于该事件的 `equity`，或 `balance_basis=cash` 时的 `cash`。用引擎账本核验这些快照；renderer 只核对引用关系，不宣称重新验证了所有资金流。

`summary.account_snapshot_time` 记录最新快照时间。早于区间终点的快照金额单独显示，不能自动视为期末权益；适配器另给期末权益时注明估值来源。终点快照存在时才可据其权益自动计算期末值。

保存引擎权益曲线时可添加 `equity` 数组及口径说明。累计已平仓净额和期末账户金额分别报告，不能在多仓模式下强制两者相等。
