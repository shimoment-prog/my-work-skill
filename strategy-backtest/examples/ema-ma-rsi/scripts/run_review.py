#!/usr/bin/env python3
"""Download public Binance candles and deliver an offline Archify trade review."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from backtest import Config
from binance_history import INTERVAL_MS, download

BEIJING = timezone(timedelta(hours=8))
SCRIPTS = Path(__file__).resolve().parent


def local_window(start_date=None, end_date=None, now=None):
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    if (start_date is None) != (end_date is None):
        raise ValueError("--start-date and --end-date must be provided together")
    if start_date is None:
        end_date = now.date() - timedelta(days=1)
        start_date = end_date - timedelta(days=2)
    elif isinstance(start_date, str):
        start_date, end_date = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start_date > end_date:
        raise ValueError("start-date must not follow end-date")
    if end_date >= now.date():
        raise ValueError("end-date must be a completed Beijing calendar day")
    return (datetime.combine(start_date, time.min, BEIJING),
            datetime.combine(end_date + timedelta(days=1), time.min, BEIJING))


def archive_window(start, end, interval, longest_length):
    # At least five days and 200 bars; longer indicators receive 10x warmup.
    warmup_ms = max(5 * 86_400_000, max(200, 10 * longest_length) * INTERVAL_MS[interval])
    first = (start - timedelta(milliseconds=warmup_ms)).astimezone(timezone.utc).date()
    last = end.astimezone(timezone.utc)
    exclusive = last.date() + (timedelta(days=1) if last.time() != time.min else timedelta())
    return first, exclusive


def number(value):
    if value is None:
        return ""
    dec = Decimal(str(value)).quantize(Decimal("0.000000001"))
    return "0" if dec == 0 else format(dec, "f").rstrip("0").rstrip(".")


def local_time(seconds):
    return datetime.fromtimestamp(seconds, BEIJING).isoformat()


def result_name(net):
    return "Open" if net is None else ("Win" if net > 0 else "Loss" if net < 0 else "Flat")


def write_ledger(path, summary, payload):
    header = ["编号", "方向", "进场时间（北京时间）", "进场价", "止损点", "止盈点", "平仓价",
              "Win/Loss（净）", "盈利USDT", "亏损USDT", "手续费USDT", "交易后余额USDT",
              "币数量", "实际名义仓位USDT", "实际保证金USDT", "杠杆"]
    balance = Decimal(str(summary["config"]["initial_capital"]))
    closed = 0
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for trade in payload["trades"]:
            net = trade["net_pnl"]
            if net is not None:
                balance += Decimal(str(net))
                if abs(balance - Decimal(str(trade["balance_after"]))) > Decimal("0.000001"):
                    raise ValueError("trade ledger balance does not reconcile")
                closed += 1
            writer.writerow([
                trade["id"], "多" if trade["direction"] == "long" else "空",
                local_time(trade["entry_time"]), number(trade["entry_fill"]),
                number(trade["stop"]), number(trade["target"]), number(trade["exit_fill"]),
                result_name(net), number(max(0, net)) if net is not None else "",
                number(max(0, -net)) if net is not None else "", number(trade["fees"]),
                number(trade["balance_after"]), number(trade["quantity"]),
                number(trade["entry_notional"]), number(trade["margin_used"]), number(trade["leverage"]),
            ])
    if closed != summary["closed_trades"]:
        raise ValueError("ledger closed count differs from summary")
    expected = Decimal(str(summary["config"]["initial_capital"])) + Decimal(str(summary["closed_net_pnl"]))
    if abs(balance - expected) > Decimal("0.000001"):
        raise ValueError("total closed net PnL does not reconcile")
    return balance


def daily_rows(payload):
    amounts = defaultdict(float)
    for trade in payload["trades"]:
        if trade["net_pnl"] is not None:
            day = datetime.fromtimestamp(trade["exit_bar_end_time"], BEIJING).date()
            amounts[day] += trade["net_pnl"]
    dates = sorted(amounts)
    if not dates:
        return []
    # Archify file charts support 60 rows. Aggregate consecutive exit days explicitly.
    span = (dates[-1] - dates[0]).days + 1
    bucket_days = max(1, math.ceil(span / 60))
    buckets = defaultdict(float)
    for day, value in amounts.items():
        bucket = (day - dates[0]).days // bucket_days
        buckets[bucket] += value
    rows = []
    for bucket, value in sorted(buckets.items()):
        first = dates[0] + timedelta(days=bucket * bucket_days)
        last = min(dates[-1], first + timedelta(days=bucket_days - 1))
        label = str(first) if bucket_days == 1 else f"{first}至{last}"
        rows.append((label, number(value)))
    return rows


def write_candidate(output, summary, payload, start, end):
    cfg = summary["config"]
    trades = payload["trades"]
    closed = [t for t in trades if t["net_pnl"] is not None]
    wins, losses = summary["winning_trades"], summary["losing_trades"]
    flat = summary["breakeven_trades"]
    win_rate = "不适用（无已平仓交易）" if summary["win_rate_percent"] is None else f"{summary['win_rate_percent']:.2f}%"
    symbol = payload["meta"]["symbol"]
    date_label = f"{start.date()}至{(end - timedelta(days=1)).date()}"
    gross = sum(t["gross_pnl"] for t in closed)
    closed_fees = sum(t["fees"] for t in closed)
    open_note = "期末有未平仓交易，浮动盈亏另计" if summary["open_position"] else "期末无持仓"
    rows = daily_rows(payload)
    with (output / "daily.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["日期或聚合区间", "净盈亏"])
        writer.writerows(rows)
    chart = ('@chart type=bar data=daily.csv x=日期或聚合区间 y=净盈亏 unit=USDT title="按北京时间退出K结束日归集；超过60日按连续日段聚合"'
             if rows else "本次没有已平仓交易，日盈亏图为空。")
    method_rows = []
    for key, label in [("L1", "多单交叉"), ("L2", "多单回踩"), ("S1", "空单交叉"), ("S2", "空单反弹")]:
        subset = [t for t in closed if t["method"] == key]
        method_rows.append(f"| {label} {key} | {len(subset)} | {sum(t['net_pnl'] > 0 for t in subset)} | {sum(t['net_pnl'] for t in subset):.6f} |")
    cfg_margin = cfg["fixed_margin"]
    size_label = f"{cfg_margin:g} USDT保证金 × {cfg['leverage']:g}倍 = {cfg_margin * cfg['leverage']:g} USDT目标名义仓位"
    intrabar_note = "先止损" if cfg["intrabar"] == "stop-first" else "按TradingView风格OHLC路径判断"
    checks = sum(len(t["checks"]) for t in trades)
    bad = sum(not c["pass"] for t in trades for c in t["checks"])
    if bad:
        raise ValueError("captured trade rules contain failing checks")
    net_loss_at_target = [str(t["id"]) for t in closed if t["exit_reason"].startswith("target") and t["net_pnl"] < 0]
    odd_note = ("触及止盈但扣费后净亏的交易：" + "、".join("#" + n for n in net_loss_at_target) + "。") if net_loss_at_target else ""
    ambiguous = [str(t["id"]) for t in closed if t["both_levels_touched"]]
    ambiguous_note = "、".join("#" + n for n in ambiguous) or "无"
    source = payload["meta"].get("source_description", "本地CSV输入")
    source_meta = summary.get("data_source_metadata")
    official_market = source_meta and source_meta.get("market") == "Binance USD-M perpetual"
    source_directive = ""
    source_detail = "本地输入来源未核验，未声称官方归档校验通过。"
    if official_market:
        sources = source_meta.get("sources", [])
        archive_sources = [s for s in sources if s.get("kind") != "rest"]
        rest_sources = [s for s in sources if s.get("kind") == "rest"]
        verified = sum(s.get("official_sha256_verified") is True for s in archive_sources)
        source_directive = '\n@source archive title="币安官方历史数据归档" url=https://data.binance.vision/'
        source_detail = f"归档{len(archive_sources)}份，其中{verified}份核验官方校验和。[](source:archive)"
        if rest_sources:
            source_directive += '\n@source rest title="币安USD-M公开K线接口" url=https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data'
            source_detail += f" REST响应{len(rest_sources)}份，仅记录本地哈希，无官方归档校验和。[](source:rest)"
    text = f'''# {symbol} 回测与逐笔交易账单 {{#backtest-result}}

本次已平仓 **{len(closed)} 笔，{wins} 胜、{losses} 负、{flat} 平**。扣手续费后净盈亏 **{summary['closed_net_pnl']:.6f} USDT**，净胜率为{win_rate}。

- 初始{cfg['initial_capital']:g} USDT；每单固定{cfg_margin:g} USDT保证金，{cfg['leverage']:g}倍杠杆。
- 目标名义仓位{cfg_margin * cfg['leverage']:g} USDT，不随账户余额复利。
- 点击“逐笔交易表”，查看价格、净盈亏及交易后余额。
- 本次{summary['ambiguous_exit_bars']}笔同根触及两端，按{intrabar_note}计算。

@answer scope="{symbol}·{cfg['interval']}·北京时间{date_label}·独立OHLC模拟"
@meta locale=zh-CN style=clean{source_directive}

## 结果和交易成本是多少？ {{#results}}

@facts
| 已平仓 | {len(closed)} 笔；{open_note} |
| 净胜率 | {win_rate} |
| 期末权益 | {summary['final_equity_mark_to_market']:.6f} USDT |
| 最大回撤 | {summary['max_drawdown_percent_at_bar_close']:.6f}%；按K线收盘权益 |

{chart}

已平仓净盈亏 = 毛盈亏 − 双边费用：{gross:.9f} − {closed_fees:.9f} = {summary['closed_net_pnl']:.9f} USDT。

全部已发生费用为{summary['total_fees_paid']:.9f} USDT，包含末尾未平仓交易已付的开仓费。

| 下单方式 | 笔数 | 净盈利笔数 | 净盈亏（USDT） |
| --- | --- | --- | --- |
{chr(10).join(method_rows)}

## 哪些条件决定入场？ {{#entry-rules}}

@workflow id=entry-flow title="触发后只交易第一根合格关键K"
| trigger | 交叉或回踩触发 | backend |
| wait | 等待关键K | backend |
| key | 收盘核对条件 | backend |
| enter | 收盘价入场 | backend |
| bracket | 固定止损止盈 | backend |
| trigger > wait | 同向有效 |
| wait > key | 第一根 |
| key > enter | 全部满足 | emphasis |
| enter > bracket | {cfg['reward_risk']:g}R |

| 条件 | 做多 | 做空 |
| --- | --- | --- |
| 均线 | 橙SMA{cfg['ma_length']}、白EMA{cfg['ema_length']} | 橙SMA{cfg['ma_length']}、白EMA{cfg['ema_length']} |
| 方式一 | 橙线上穿白线 | 橙线下穿白线 |
| 方式二 | 橙在白上；阴线收在白下 | 橙在白下；阳线收在白上 |
| 关键K | 阳线；最低价严格高于两线 | 阴线；最高价严格低于两线 |
| RSI{cfg['rsi_length']} | {cfg['long_rsi_min']:g}至{cfg['long_rsi_max']:g}，含边界 | {cfg['short_rsi_min']:g}至{cfg['short_rsi_max']:g}，含边界 |
| 止损 | 关键K当时橙线，开仓后固定 | 关键K当时橙线，开仓后固定 |
| 止盈 | 入场加{cfg['reward_risk']:g}倍初始风险 | 入场减{cfg['reward_risk']:g}倍初始风险 |

影线与关键K收盘时计算的均线比较，接触均线不算合格。

方式一允许交叉当根成为关键K。方式二从下一根寻找。

均线反向或相等时取消等待；同时满足两种方式时优先方式一。

## 本次按什么口径计算？ {{#assumptions}}

| 项目 | 设置 |
| --- | --- |
| 时间范围 | {start.isoformat()}至{end.isoformat()}；结束日期含全天 |
| 入场时间边界 | 按关键K收盘时间含首尾；图轴标记K线开盘时间 |
| 周期与参数 | {cfg['interval']}；EMA{cfg['ema_length']}、SMA{cfg['ma_length']}、RSI{cfg['rsi_length']} |
| 本金与仓位 | {cfg['initial_capital']:g} USDT；{size_label} |
| 成交成本 | 单边费率{cfg['fee_percent']:g}%；滑点{cfg['slippage_ticks']}跳 |
| 最小单位 | 价格{cfg['tick_size']:g} USDT；币数量{cfg['qty_step']:g} |
| 启用信号 | 方向{cfg['direction']}；方式{cfg['method']} |
| 数量取整 | 目标名义仓位除以进场价，向下取整至数量单位 |
| 持仓 | 同时单笔，不加仓；平仓当根不重新入场 |
| 双触退出 | {intrabar_note}；交易编号：{ambiguous_note} |
| 指标预热 | 开仓窗口前{summary['warmup_bars_before_entry_start']}根K线 |
| 图表覆盖 | {len(payload['candles'])}根完整K线，不采样 |

数据来源：{source}。原始CSV摘要保存于结果文件。

{source_detail}

@callout level=warning title="回测范围"
| 未计资金费率、标记价格触发和强平；未验证与TradingView逐笔一致。 |
| 区间结束后停止数据；未平仓交易按末根收盘估值，不强制平仓。 |
| K内精确退出时刻未知，仓位框画至退出K结束处。 |
| 图中仓位框为本地绘制，并未创建TradingView账户原生绘图对象。 |

## 怎样核对每笔交易？ {{#review-checklist}}

本次共{checks}项逐笔条件检查，失败{bad}项。点击交易编号可返回对应K线。

图中蓝框标出关键K，入场点在该K收盘时刻。入场点可能位于下一根K的开盘标签上。

Win/Loss按扣除开平仓手续费后的净盈亏判断；盈利和亏损列已含费用，手续费列不重复扣。

平仓后余额 = 初始金额 + 累计已平仓净盈亏。保证金平仓后归还，不作永久支出。

末尾Open行没有平仓价、净盈亏或交易后余额，不计入胜率。期末权益另含浮动盈亏和已付开仓费。

{odd_note}

- [ ] 检查币种、日期、周期、保证金和杠杆。
- [ ] 检查空单为阴线且上影线在两线下方，多单反之。
- [ ] 检查RSI范围、触发、等待和固定止盈止损。
- [ ] 检查每笔净盈亏与余额相加；费用不重复扣除。
- [ ] 对双触退出K核对更细粒度数据，确认先后顺序。
'''
    (output / "candidate.archify.md").write_text(text, encoding="utf-8")


def resolve_archify(explicit=None):
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path.home() / ".agents/skills/archify/bin/archify.mjs",
        Path.home() / ".codex/skills/archify/bin/archify.mjs",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ValueError("Archify CLI unavailable; pass --archify-cli /path/to/archify/bin/archify.mjs. Computed files and draft were preserved.")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--start-date", help="Beijing YYYY-MM-DD, inclusive")
    cli.add_argument("--end-date", help="Beijing YYYY-MM-DD, inclusive; completed days only")
    cli.add_argument("--symbol", default="ETHUSDT")
    cli.add_argument("--interval", default="5m", choices=sorted(INTERVAL_MS))
    cli.add_argument("--initial-capital", type=float, default=10000)
    cli.add_argument("--fixed-margin", type=float, default=50)
    cli.add_argument("--leverage", type=float, default=10)
    cli.add_argument("--ema-length", type=int, default=10)
    cli.add_argument("--ma-length", type=int, default=10)
    cli.add_argument("--rsi-length", type=int, default=14)
    cli.add_argument("--long-rsi-min", type=float, default=50)
    cli.add_argument("--long-rsi-max", type=float, default=70)
    cli.add_argument("--short-rsi-min", type=float, default=30)
    cli.add_argument("--short-rsi-max", type=float, default=50)
    cli.add_argument("--intrabar", choices=["stop-first", "tv-path"], default="stop-first")
    cli.add_argument("--reward-risk", type=float, default=1.5)
    cli.add_argument("--fee-percent", type=float, default=0.05)
    cli.add_argument("--slippage-ticks", type=int, default=0)
    cli.add_argument("--tick-size", type=float, default=0.01)
    cli.add_argument("--qty-step", type=float, default=0.001)
    cli.add_argument("--direction", choices=["both", "long", "short"], default="both")
    cli.add_argument("--method", choices=["both", "cross", "pullback"], default="both")
    cli.add_argument("--csv", type=Path, help="Reuse candles instead of downloading; needs complete range and warmup")
    cli.add_argument("--output-dir", type=Path)
    cli.add_argument("--archify-cli", type=Path)
    cli.add_argument("--node", help="Node executable; defaults to node on PATH")
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    try:
        start, end = local_window(args.start_date, args.end_date)
        if not re.fullmatch(r"[A-Z0-9]{3,30}", args.symbol):
            raise ValueError("symbol must contain 3-30 uppercase letters/digits")
        cfg = Config(start_ms=int(start.timestamp() * 1000), end_ms=int(end.timestamp() * 1000),
                     interval=args.interval, initial_capital=args.initial_capital,
                     fixed_margin=args.fixed_margin, leverage=args.leverage,
                     ema_length=args.ema_length, ma_length=args.ma_length, rsi_length=args.rsi_length,
                     reward_risk=args.reward_risk, fee_percent=args.fee_percent,
                     slippage_ticks=args.slippage_ticks, tick_size=args.tick_size, qty_step=args.qty_step,
                     direction=args.direction, method=args.method, intrabar=args.intrabar,
                     long_rsi_min=args.long_rsi_min, long_rsi_max=args.long_rsi_max,
                     short_rsi_min=args.short_rsi_min, short_rsi_max=args.short_rsi_max)
        cfg.validate()
        if cfg.start_ms % INTERVAL_MS[args.interval] or cfg.end_ms % INTERVAL_MS[args.interval]:
            raise ValueError("interval does not align with Beijing calendar-day boundaries; use an interval such as 5m, 1h, 4h or 8h")
        stamp = datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S-%f")
        output = (args.output_dir or Path.cwd() / ".archify" / f"explainer-{args.symbol.lower()}-backtest-{stamp}").resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("output-dir must be new or empty; refusing to overwrite previous results")
        if args.csv and not args.csv.is_file():
            raise ValueError("--csv file does not exist")
        output.mkdir(parents=True, exist_ok=True)
        if args.csv:
            csv_path = args.csv.resolve()
        else:
            first, last = archive_window(start, end, args.interval,
                                         max(args.ema_length, args.ma_length, args.rsi_length))
            csv_path = output / "data" / f"{args.symbol}-{args.interval}-{first:%Y%m%d}-{last:%Y%m%d}.csv"
            download(args.symbol, args.interval, first, last, csv_path, output / "data" / "cache")
        command = [sys.executable, str(SCRIPTS / "build_trade_chart.py"), "--csv", str(csv_path),
                   "--start", start.isoformat(), "--end", end.isoformat(), "--symbol", args.symbol,
                   "--output-dir", str(output / "results"), "--fragment", str(output / "chart-fragment.html")]
        for key in ("interval", "initial_capital", "fixed_margin", "leverage", "ema_length", "ma_length",
                    "rsi_length", "reward_risk", "fee_percent", "slippage_ticks", "tick_size", "qty_step", "direction", "method",
                    "long_rsi_min", "long_rsi_max", "short_rsi_min", "short_rsi_max", "intrabar"):
            command.extend(["--" + key.replace("_", "-"), str(getattr(args, key))])
        subprocess.run(command, check=True)
        summary = json.loads((output / "results" / "summary.json").read_text(encoding="utf-8"))
        payload = json.loads((output / "results" / "trade_audit.json").read_text(encoding="utf-8"))
        write_ledger(output / "trade-ledger.csv", summary, payload)
        write_candidate(output, summary, payload, start, end)
        archify = resolve_archify(args.archify_cli)
        node = args.node or shutil.which("node")
        if not node:
            raise ValueError("Node unavailable; pass --node /path/to/node. Computed files and draft were preserved.")
        report = output / "backtest-report.html"
        subprocess.run([node, str(archify), "finalize", "explainer", str(output / "candidate.archify.md"),
                        str(report), "--json"], cwd=output, check=True)
        title = f"{args.symbol} 逐笔回测 · {args.fixed_margin:g} USDT保证金 × {args.leverage:g}倍"
        period = f"{start.date()} — {(end - timedelta(days=1)).date()}（含全天）· 北京时间 · {args.interval} · 初始{args.initial_capital:g} USDT"
        subprocess.run([sys.executable, str(SCRIPTS / "build_archify_review.py"),
                        "--chart-fragment", str(output / "chart-fragment.html"), "--report", str(report),
                        "--output", str(output / "review.html"), "--title", title, "--period-label", period], check=True)
        result = {"review_html": str(output / "review.html"), "ledger_csv": str(output / "trade-ledger.csv"),
                  "summary_json": str(output / "results" / "summary.json"),
                  "closed_trades": summary["closed_trades"], "net_pnl": summary["closed_net_pnl"],
                  "final_equity": summary["final_equity_mark_to_market"], "data_sha256": summary["data_sha256"]}
        (output / "run-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        cli.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
