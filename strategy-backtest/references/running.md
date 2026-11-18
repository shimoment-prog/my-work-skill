# 运行通用回测模板

`SKILL_DIR` 是当前 skill 所在目录。Python 3.9 或更新版本即可运行公共脚本；Archify 报告另需已安装的 `archify` 和 Node.js。图表库已随包附带。

## 本次策略在工作目录执行

先按用户规则创建本次 `request.json`、数据适配器和策略实现。常见调用形式：

```bash
python3 ./strategy_adapter.py \
  --request ./request.json --csv ./data/candles.csv --output ./result.json
```

这是适配器调用约定，不是固定策略引擎。可以使用 pandas、专业回测框架或用户已有实现；新增依赖写入本次工作目录并说明。不要把别的策略名称传给 EMA 示例脚本后宣称已执行新策略。

将标准结果渲染成正式页面：

```bash
python3 "$SKILL_DIR/scripts/render_review.py" \
  --result ./result.json --output-dir ./review-output
```

需要指定依赖位置时增加 `--archify-cli /path/to/archify/bin/archify.mjs --node /path/to/node`。默认查找 `~/.agents/skills/archify`、`~/.codex/skills/archify` 与 PATH 中的 Node。调用前读取已安装 Archify skill 的 explainer 说明。

输出目录须新建或为空，不覆盖旧结果。依赖或 Archify 阶段失败时保留计算结果，说明未完成的步骤。纯离线测试可对该次命令设置 `ARCHIFY_UPDATE_CHECK_DISABLED=1`，正常交付遵循 Archify 的更新提示规则。

## 内置数据适配器

币安 USDT 永续可用：

```bash
python3 "$SKILL_DIR/scripts/binance_history.py" \
  --symbol BTCUSDT --interval 15m \
  --start 2026-11-01 --end 2026-11-10 \
  --output ./data/candles.csv --cache-dir ./data/cache
```

此处日期按 UTC，开始包含、结束排他。实际转换本次用户日期，并另加足够预热；这些示例数值不是默认配置。其它市场由相应数据适配器完成。

下载器保存 CSV 与元数据、原始来源及 SHA256。ZIP 有官方校验和时校验；缺档 REST 只记录本地响应哈希，不称官方归档已核验。不会绕过访问限制或用其它市场替代。

## 可运行示例

`examples/sma-cross/` 展示另一种策略如何接入通用结果。它的下一根开盘成交、只做多和线性盈亏仅属于示例，不是主模板限制。

此前的完整实现保留在 `examples/ema-ma-rsi/`，原来的 `scripts/run_review.py` 仍可针对该示例运行，其说明和资产在同级目录。该程序不作为新策略的通用入口。

## 交付文件

- `review.html`：单文件 K 线、任意指标、仓位和逐笔表，附 Archify 报告。
- `trade-ledger.csv`：逐笔价格、净盈亏、费用和账户金额。
- `result.json` / `summary.json`：标准化逐笔结果、参数与计算摘要。
- `candidate.archify.md` / `daily.csv`：本次实际数字生成的候选与日盈亏数据。
- `backtest-report.html` 及凭据：Archify 官方报告与交付记录。
- `review.manifest.json`：组合产物哈希及官方报告来源。

用浏览器单独检查组合页。策略规则正确性需要样例和独立交易核对；renderer 格式及金额校验通过不代表任意策略实现已经正确。
