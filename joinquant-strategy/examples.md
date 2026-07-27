# 示例：自然语言 → 聚宽策略

## 输入（用户逻辑）

股票池：过滤 ST、科创板、北交所、上市未满一年次新股。

选股同时满足：

1. 前一日涨停且为首板  
2. 股价在相对低位：近 60 日最高与最低的中位线以下（例：高 100、低 50 → 须低于 75）  
3. 次日低开 -3%～-4%

买入：符合条件全部等权。  
卖出：买入后第二天上午赚钱就卖；不赚钱拿到尾盘，无论盈亏都卖。

## 时间线拆解

- `T-1`：涨停 + 首板 + 收盘处于 60 日相对低位  
- `T` 开盘：低开落在 `[-4%, -3%]` → 等权买入  
- `T+1` 上午：现价 > 成本 → 卖出  
- `T+1` 尾盘：仍持有的昨日仓 → 清仓  

## 关键实现要点

```python
# 调度：先卖后买
run_daily(morning_sell, time='09:31')
run_daily(buy_stocks, time='09:32')
run_daily(close_sell, time='14:50')

# 首板
is_limit_up(y_close, y_high_limit) and not is_limit_up(dby_close, dby_high_limit)

# 相对低位（用昨收）
y_close < (hist['high'].max() + hist['low'].min()) / 2

# 低开闭区间
-0.04 <= day_open / y_close - 1 <= -0.03

# 等权
order_value(stock, available_cash / len(candidates))

# 避免误卖当日新开仓
if buy_date is None or buy_date >= today:
    continue
```

## 对照表（回复用户时采用此格式）

| 用户规则 | 实现 |
|----------|------|
| 过滤 ST/科创板/北交所/次新 | `is_st`、`688*`、`.XBEI`、上市 `<365` 天 |
| 昨日首板涨停 | 昨涨停且前日未涨停 |
| 相对低位 | 昨收 < (60日高+低)/2 |
| 低开 -3%～-4% | 今开相对昨收 ∈ [-4%, -3%] |
| 等权买入 | 可用现金 / N + `order_value` |
| 次日上午盈利卖 | `09:31` 且 `last_price > avg_cost` |
| 否则尾盘卖 | `14:50` 清仓 |

完整参考实现可见量化项目中的 `first_board_low_open.py`（若同机有 `my-quant` 仓库）。
