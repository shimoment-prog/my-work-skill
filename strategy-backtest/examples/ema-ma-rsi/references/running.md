# 运行与输出

`SKILL_DIR` 指当前 skill 文件夹，用实际安装位置替换。Python 3.9 或更新版本，只需标准库；HTML 报告另依赖 Archify 和 Node.js。图表库已随包附带，结果离线可打开。

## 一键运行

在用户工作目录运行，不必切换到 skill 目录。省略日期时取最近 3 个完整北京时间日。

```bash
python3 "$SKILL_DIR/scripts/run_review.py"
```

自定义日期和仓位：

```bash
python3 "$SKILL_DIR/scripts/run_review.py" \
  --symbol ETHUSDT --interval 5m \
  --start-date 2026-11-10 --end-date 2026-11-15 \
  --initial-capital 10000 --fixed-margin 50 --leverage 10 \
  --fee-percent 0.05 --slippage-ticks 0 \
  --tick-size 0.01 --qty-step 0.001 \
  --output-dir ./backtest-nov10-15
```

起止日期成对提供，结束日含全天。输出文件夹须不存在或为空，避免覆盖旧结果；未指定时创建 `./.archify/explainer-<symbol>-backtest-<timestamp>/`。只支持与北京时间零点对齐的完整固定周期；`6h`、`12h`、`1d` 会明确拒绝，`5m`、`1h`、`4h`、`8h` 等可用。

`--symbol` 使用裸合约代码，如 `ETHUSDT`，只处理 USDT 永续。资金始终按固定保证金模式计算；`--leverage 10` 不会把盈亏重复乘以 10。

可调整的策略参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--ema-length` / `--ma-length` / `--rsi-length` | 10 / 10 / 14 | 当前 K 周期的根数 |
| `--long-rsi-min` / `--long-rsi-max` | 50 / 70 | 多单 RSI 区间，含边界 |
| `--short-rsi-min` / `--short-rsi-max` | 30 / 50 | 空单 RSI 区间，含边界 |
| `--reward-risk` | 1.5 | 初始风险对应目标倍数 |
| `--direction` | `both` | `both`、`long`、`short` |
| `--method` | `both` | `both`、`cross`、`pullback` |
| `--intrabar` | `stop-first` | 双触止损优先；可选 `tv-path` 为 OHLC 路径假设 |

## 复用已有数据

```bash
python3 "$SKILL_DIR/scripts/run_review.py" \
  --start-date 2026-11-10 --end-date 2026-11-15 \
  --csv /absolute/path/ETHUSDT-5m.csv \
  --output-dir ./backtest-offline \
  --archify-cli /absolute/path/archify/bin/archify.mjs \
  --node /absolute/path/node
```

没有自定义依赖位置时省略最后两项。Archify 自动寻找 `~/.agents/skills/archify/bin/archify.mjs` 和 `~/.codex/skills/archify/bin/archify.mjs`；Node 默认取 PATH。

CSV 列必须包含 `open_time_ms,open,high,low,close,volume,close_time_ms`。时间用 UTC 毫秒，`close_time_ms` 为下一根开盘前 1 毫秒。数据必须顺序连续、无缺 K、覆盖完整区间并有指标预热。下载器自动外扩至少 5 天及至少 `max(200,10×最长指标周期)` 根 K；本地 CSV 至少提供最长指标周期加 1 根预热，复现旧结果应使用相同预热历史。

相邻 `<CSV>.metadata.json` 存在时，核验 CSV SHA256、symbol、interval。没有来源元数据仍可计算，但页面必须标为用户输入、来源未核验，不能宣称币安官方归档已核验。

测试纯离线运行时，可仅对该命令设置 `ARCHIFY_UPDATE_CHECK_DISABLED=1`，避免 Archify 更新检查发请求。正常交付按已安装 Archify 的更新提示约定执行。

## 数据来源与产物

下载按 UTC 日外扩覆盖北京时间窗口，优先月 ZIP，再补日 ZIP；缺档才用官方 `/fapi/v1/klines` 分页，保留已收盘 K。所有来源记录在 CSV 元数据。ZIP 有官方 CHECKSUM 时验证；REST 只记录本地原始响应 SHA256，不称作官方归档校验和。访问失败不会绕过地域或访问限制，也不使用其他交易所数据代替。

| 文件 | 内容 |
| --- | --- |
| `review.html` | 最终单文件页面：K 线、RSI、仓位框、逐笔表及 Archify 报告 |
| `trade-ledger.csv` | 中文逐笔账单，可直接查看或导入表格 |
| `results/summary.json` | 参数、成本、胜率、期末权益、数据哈希和限制 |
| `results/trades.csv` | 已平仓交易原始字段 |
| `results/trade_audit.json` | 图表数据、逐笔触发与关键 K 检查；包含 Open 单 |
| `results/equity.csv` | 每根 K 收盘权益 |
| `results/report.md` | 回测数值报告 |
| `candidate.archify.md` / `daily.csv` | 本次数字生成的 Archify 候选与分日盈亏 |
| `backtest-report.html` 及凭据 | Archify 正式报告与交付记录 |
| `review.manifest.json` / `run-summary.json` | 组合文件哈希、来源与运行摘要 |
| `data/` | 自动获取时的 CSV、元数据和原始响应缓存 |

组合页面中的官方报告保持原字节；报告的交付凭据不等于自定义图表通过浏览器检查。超过 16 MB 的图表会停止并要求拆分日期，不静默丢弃 K 或交易。

某步失败后修复依赖，可沿用已保存的数据，在新输出目录加 `--csv` 重跑；不要删除旧结果。只有 Archify 阶段失败时，也可以按其 skill 对候选执行 `finalize explainer`，再调用 `build_archify_review.py --chart-fragment ... --report ... --output ... --title ... --period-label ...` 继续封装。
