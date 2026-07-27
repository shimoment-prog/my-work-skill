# 聚宽策略 API 速查

权威来源：[平台 API 文档](https://www.joinquant.com/help/api/help#name:api) · [PDF](https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf)

本表只列策略脚本高频接口；签名以官网为准，冲突时以官网为准。

## 策略框架

| API | 用途 |
|-----|------|
| `initialize(context)` | 回测/模拟启动时执行一次 |
| `run_daily(func, time='09:30')` | 每个交易日定时执行；`func(context)` |
| `run_weekly` / `run_monthly` | 周/月定时 |
| `before_trading_start` / `after_trading_end` | 开盘前 / 收盘后 |
| `handle_data(context, data)` | 按回测频率驱动；**不建议与 run_daily 混用** |
| `g` | 全局变量命名空间 |
| `context.current_dt` | 当前逻辑时间 |
| `context.portfolio` | 账户：`total_value` / `available_cash` / `positions` |

推荐：`run_daily` 编排买卖；不要写 `run_daily(handle_data, ...)`。

## 设置

```python
set_benchmark('000300.XSHG')
set_option('use_real_price', True)
set_option('avoid_future_data', True)
set_order_cost(
    OrderCost(
        open_tax=0, close_tax=0.001,
        open_commission=0.0003, close_commission=0.0003,
        close_today_commission=0, min_commission=5,
    ),
    type='stock',
)
log.set_level('order', 'error')
```

## 证券列表与信息

| API | 用途 |
|-----|------|
| `get_all_securities(['stock'], date)` | 某日全市场股票，`.index` 为代码列表 |
| `get_security_info(code)` | `start_date` 上市日、`display_name` 等 |
| `get_index_stocks('000300.XSHG')` | 指数成分股 |

代码后缀：`.XSHG` 上交所、`.XSHE` 深交所、`.XBEI` 北交所。

## 行情与当日状态

| API | 用途 | 注意 |
|-----|------|------|
| `attribute_history(sec, count, '1d', fields, skip_paused=True)` | 单标的历史；**不含当天** | 防未来函数 |
| `get_price(sec, end_date=..., count=..., fields=...)` | 单/多标的行情 | `end_date` 勿大于当前逻辑日 |
| `history(count, unit, field, security_list)` | 多标的某一字段历史 | 天级不含当天 |
| `get_current_data()` | 当日：`day_open` / `high_limit` / `low_limit` / `paused` / `is_st` / `last_price` / `name` | 仅交易时段可靠；按需取值 |

涨停判断常用：`close >= high_limit * 0.997`。

## 基本面

```python
q = query(valuation.code, valuation.market_cap).filter(
    valuation.code.in_(codes)
).order_by(valuation.market_cap.asc())
df = get_fundamentals(q)
```

常用表：`valuation`（市值等）、`indicator`、`income`、`balance` 等。字段以数据字典为准。

## 交易

| API | 用途 |
|-----|------|
| `order(sec, amount)` | 买卖指定股数 |
| `order_target(sec, amount)` | 调仓到目标股数；`0` 清仓 |
| `order_value(sec, value)` | 买入/卖出指定金额 |
| `order_target_value(sec, value)` | 调仓到目标市值 |

持仓：`context.portfolio.positions[sec].total_amount` / `avg_cost` / `price` / `init_time`。

## 常见过滤片段

```python
current_data = get_current_data()
# ST / 停牌 / 退
cd.is_st or cd.paused or ('ST' in cd.name) or ('退' in cd.name)
# 科创板
stock.startswith('688')
# 北交所
stock.endswith('.XBEI')
# 上市未满一年
(context.current_dt.date() - get_security_info(stock).start_date).days < 365
```

## 定时建议

| 场景 | time |
|------|------|
| 开盘买入/用开盘价判断 | `'09:30'` 或 `'09:31'`（集合竞价后更稳） |
| 上午是否盈利卖出 | `'09:31'`～`'10:00'` |
| 尾盘清仓 | `'14:50'` 或 `'14:55'` |

需要更真实的盘中价时，回测频率选**分钟**。
