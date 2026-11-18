#!/usr/bin/env python3
"""Validate a strategy-neutral result and deliver an offline Archify trade review.

This renderer does not execute strategy rules or infer quantity, leverage, PnL,
market calendars, fill paths, fees, or liquidation rules. Those belong to the
chosen engine and adapter. Only the supplied records and accounting invariants
are checked here.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
TOLERANCE = Decimal('0.000001')
SIZING_MODES = {'fixed_margin', 'fixed_notional', 'equity_percent', 'quantity', 'risk_amount'}


def finite(value, label, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    if positive and value <= 0 or nonnegative and value < 0:
        raise ValueError(f'{label} has an invalid sign')
    return value


def timestamp(value, label):
    finite(value, label, nonnegative=True)
    if int(value) != value or value >= 100_000_000_000:
        raise ValueError(f'{label} must be Unix seconds, not milliseconds')
    return value


def text_field(obj, key, label='meta'):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}.{key} must be nonempty text')
    return value


def decimal(value):
    return Decimal(str(value))


def equal(actual, expected, label):
    finite(actual, label)
    if abs(decimal(actual) - decimal(expected)) > TOLERANCE:
        raise ValueError(f'{label} does not reconcile: {actual} versus {expected}')


def result_name(net):
    return 'Open' if net is None else 'Win' if net > 0 else 'Loss' if net < 0 else 'Flat'


def number(value):
    if value is None:
        return ''
    dec = decimal(value).quantize(Decimal('0.000000001'))
    return '0' if not dec else format(dec, 'f').rstrip('0').rstrip('.')


def validate_result(raw):
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise ValueError('schema_version must be 1')
    result = copy.deepcopy(raw)
    meta = result.get('meta')
    if not isinstance(meta, dict):
        raise ValueError('meta must be an object')
    for key in ('strategy_name', 'symbol', 'market', 'currency', 'interval', 'timezone', 'source_description'):
        text_field(meta, key)
    meta.setdefault('price_currency', meta['currency'])
    text_field(meta, 'price_currency')
    if 'quantity_unit' in meta:
        text_field(meta, 'quantity_unit')
    try:
        ZoneInfo(meta['timezone'])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('meta.timezone must name an available IANA time zone') from exc
    initial = finite(meta.get('initial_capital'), 'meta.initial_capital', positive=True)
    start = timestamp(meta.get('start_time'), 'meta.start_time')
    end = timestamp(meta.get('end_time'), 'meta.end_time')
    if start >= end:
        raise ValueError('start_time must precede end_time')
    sizing = meta.get('sizing')
    if not isinstance(sizing, dict) or sizing.get('mode') not in SIZING_MODES:
        raise ValueError('meta.sizing.mode must explicitly state the order amount basis')
    finite(sizing.get('value'), 'meta.sizing.value', positive=True)
    if finite(sizing.get('leverage'), 'meta.sizing.leverage', positive=True) < 1:
        raise ValueError('leverage must be at least 1')
    text_field(sizing, 'description', 'meta.sizing')
    for key in ('price_step', 'quantity_step'):
        if meta.get(key) is not None:
            finite(meta[key], 'meta.' + key, positive=True)
    if not isinstance(meta.get('limitations', []), list) or not all(isinstance(x, str) for x in meta.get('limitations', [])):
        raise ValueError('meta.limitations must be an array of text')
    if meta.get('strategy_description') is not None and not isinstance(meta['strategy_description'], str):
        raise ValueError('meta.strategy_description must be text')
    mode = meta.setdefault('accounting_mode', 'closed_pnl')
    if mode not in ('closed_pnl', 'account_snapshots'):
        raise ValueError('accounting_mode must be closed_pnl or account_snapshots')
    basis = meta.setdefault('balance_basis', 'equity')
    if basis not in ('cash', 'equity'):
        raise ValueError('balance_basis must be cash or equity')
    if mode == 'closed_pnl':
        meta['balance_description'] = '已平仓账面余额＝初始本金＋累计已平仓净盈亏；不含未平仓交易的已付费用和浮动盈亏。'
    else:
        meta['balance_description'] = f'交易后金额取该平仓事件的账户快照（{basis}）；不按逐笔盈亏简单相加。'
    if meta.get('final_equity') is not None:
        finite(meta['final_equity'], 'meta.final_equity')

    candles = result.get('candles')
    if not isinstance(candles, list) or not candles:
        raise ValueError('candles must contain OHLC records')
    candle_times = set()
    previous = -1
    for index, candle in enumerate(candles):
        if not isinstance(candle, dict):
            raise ValueError('candle must be an object')
        label = f'candles[{index}]'
        time = timestamp(candle.get('time'), label + '.time')
        if time <= previous or time >= end:
            raise ValueError('candle times must increase strictly and precede end_time')
        previous = time
        candle_times.add(time)
        for key in ('open', 'high', 'low', 'close'):
            finite(candle.get(key), label + '.' + key, positive=True)
        if not candle['low'] <= min(candle['open'], candle['close']) <= max(candle['open'], candle['close']) <= candle['high']:
            raise ValueError(label + ' has inconsistent OHLC')
        if candle.get('volume') is not None:
            finite(candle['volume'], label + '.volume', nonnegative=True)
    indicators = result.setdefault('indicators', [])
    if not isinstance(indicators, list):
        raise ValueError('indicators must be an array')
    ids = set()
    for indicator in indicators:
        if not isinstance(indicator, dict):
            raise ValueError('indicator must be an object')
        iid = text_field(indicator, 'id', 'indicator')
        if iid in ids:
            raise ValueError('indicator ids must be unique')
        ids.add(iid)
        text_field(indicator, 'name', 'indicator')
        if indicator.get('pane') not in ('price', 'oscillator'):
            raise ValueError('indicator pane must be price or oscillator')
        if not isinstance(indicator.get('values'), list):
            raise ValueError('indicator.values must be an array')
        previous = -1
        for point in indicator['values']:
            time = timestamp(point.get('time'), 'indicator point time')
            if time not in candle_times or time <= previous:
                raise ValueError('indicator times must be increasing candle times')
            finite(point.get('value'), 'indicator point value')
            previous = time
        if not isinstance(indicator.get('levels', []), list):
            raise ValueError('indicator.levels must be an array')
        for level in indicator.get('levels', []):
            finite(level, 'indicator level')

    events = result.setdefault('account_events', [])
    if not isinstance(events, list):
        raise ValueError('account_events must be an array')
    events_by_id = {}
    previous = -1
    for event in events:
        eid = event.get('id')
        if isinstance(eid, bool) or not isinstance(eid, (str, int)) or not str(eid):
            raise ValueError('account event requires a text or integer id')
        if str(eid) in events_by_id:
            raise ValueError('account event ids must be unique')
        time = timestamp(event.get('time'), 'account event time')
        if time < previous or not start <= time <= end:
            raise ValueError('account events must be chronological and in the requested window')
        finite(event.get('cash'), 'account event cash')
        finite(event.get('equity'), 'account event equity')
        events_by_id[str(eid)] = event
        previous = time
    if mode == 'account_snapshots' and not events:
        raise ValueError('account_snapshots requires an explicit event ledger')

    trades = result.get('trades')
    if not isinstance(trades, list):
        raise ValueError('trades must be an array')
    ids, previous_entry, previous_exit = set(), -1, -1
    balance = decimal(initial)
    net_total, gross_total, fees_total, costs_total = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    wins = losses = flats = opened = 0
    last_event_time = -1
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            raise ValueError('trade must be an object')
        label = f'trades[{index}]'
        tid = trade.get('id')
        if isinstance(tid, bool) or not isinstance(tid, (str, int)) or not str(tid) or str(tid) in ids:
            raise ValueError('trade ids must be unique nonempty strings or integers')
        ids.add(str(tid))
        if trade.get('direction') not in ('long', 'short') or trade.get('status') not in ('closed', 'open'):
            raise ValueError(label + ' requires explicit direction and status')
        entry = timestamp(trade.get('entry_time'), label + '.entry_time')
        if entry < previous_entry or not start <= entry <= end:
            raise ValueError('trades must be entry-ordered and enter in the requested window')
        if mode == 'closed_pnl' and entry < previous_exit:
            raise ValueError('overlapping positions require account_snapshots accounting')
        previous_entry = entry
        finite(trade.get('entry_price'), label + '.entry_price', positive=True)
        finite(trade.get('quantity'), label + '.quantity', positive=True)
        for key in ('stop', 'target', 'entry_notional', 'margin_used'):
            trade.setdefault(key, None)
            if trade[key] is not None:
                finite(trade[key], label + '.' + key, positive=key != 'margin_used', nonnegative=key == 'margin_used')
        if trade.get('leverage') is not None and finite(trade['leverage'], label + '.leverage', positive=True) < 1:
            raise ValueError('trade leverage must be at least 1')
        trade.setdefault('key_time', None)
        if trade['key_time'] is not None:
            timestamp(trade['key_time'], label + '.key_time')
            if trade['key_time'] not in candle_times or trade['key_time'] > entry:
                raise ValueError('key_time must identify a candle no later than entry')
        for field in ('fees', 'other_costs'):
            if field not in trade:
                raise ValueError(label + '.' + field + ' is required, use 0 when absent')
            finite(trade[field], label + '.' + field, nonnegative=field == 'fees')
        fees_total += decimal(trade['fees'])
        costs_total += decimal(trade['other_costs'])
        checks = trade.get('checks')
        if not isinstance(checks, list):
            raise ValueError(label + '.checks must be an array')
        for check in checks:
            text_field(check, 'label', 'check')
            if not isinstance(check.get('value'), str) or check.get('pass') is not True:
                raise ValueError('every supplied rule check must explicitly pass')
        levels = trade.get('risk_levels', [])
        if not isinstance(levels, list):
            raise ValueError('risk_levels must be an array')
        previous_risk_time = -1
        for point in levels:
            time = timestamp(point.get('time'), 'risk level time')
            if time < entry or time > end or time <= previous_risk_time:
                raise ValueError('risk levels must be inside the holding window')
            previous_risk_time = time
            for field in ('stop', 'target'):
                if point.get(field) is not None:
                    finite(point[field], 'risk ' + field, positive=True)
        trade.setdefault('exit_reason', '')
        for key in ('exit_time', 'exit_price', 'exit_bar_start', 'exit_bar_end', 'gross_pnl', 'net_pnl', 'balance_after'):
            trade.setdefault(key, None)
        if trade['status'] == 'open':
            opened += 1
            if any(trade[key] is not None for key in ('exit_time', 'exit_price', 'gross_pnl', 'net_pnl', 'balance_after')):
                raise ValueError('Open trade cannot have closed fill, PnL or closing balance')
            previous_exit = end + 1
            continue
        finite(trade['exit_price'], label + '.exit_price', positive=True)
        finite(trade['gross_pnl'], label + '.gross_pnl')
        finite(trade['net_pnl'], label + '.net_pnl')
        exact = trade['exit_time']
        if exact is not None:
            exit_end = timestamp(exact, label + '.exit_time')
        else:
            bar_start = timestamp(trade['exit_bar_start'], label + '.exit_bar_start')
            exit_end = timestamp(trade['exit_bar_end'], label + '.exit_bar_end')
            if bar_start >= exit_end:
                raise ValueError('unknown exit time requires a valid enclosing candle range')
        if not entry <= exit_end <= end:
            raise ValueError('exit must be after entry and within the requested window')
        if any(point['time'] > exit_end for point in trade.get('risk_levels', [])):
            raise ValueError('risk levels cannot extend after exit')
        previous_exit = exit_end
        net = decimal(trade['gross_pnl']) - decimal(trade['fees']) - decimal(trade['other_costs'])
        equal(trade['net_pnl'], net, label + '.net_pnl')
        if mode == 'closed_pnl':
            balance += net
            equal(trade['balance_after'], balance, label + '.balance_after')
        else:
            event = events_by_id.get(str(trade.get('account_event_id')))
            if not event:
                raise ValueError('closed trade must reference its account event')
            if exact is not None and event['time'] != exact:
                raise ValueError('account event must match exact exit time')
            if exact is None and not max(entry, trade['exit_bar_start']) <= event['time'] <= exit_end:
                raise ValueError('account event must lie in the exit candle range')
            equal(trade['balance_after'], event[basis], label + '.balance_after')
            last_event_time = max(last_event_time, event['time'])
        net_total += net
        gross_total += decimal(trade['gross_pnl'])
        wins += net > 0
        losses += net < 0
        flats += net == 0
    closed = wins + losses + flats
    account_snapshot_time = None
    if mode == 'account_snapshots':
        account_snapshot_time = events[-1]['time']
        balance = decimal(events[-1][basis])
        if meta.get('final_equity') is not None and account_snapshot_time == end:
            equal(meta['final_equity'], events[-1]['equity'], 'meta.final_equity')
    final_equity = meta.get('final_equity')
    if mode == 'closed_pnl' and not opened:
        if final_equity is not None:
            equal(final_equity, balance, 'meta.final_equity')
        final_equity = float(balance)
    if mode == 'account_snapshots':
        if account_snapshot_time == end:
            final_equity = events[-1]['equity']
            meta['final_equity_source'] = ('adapter_and_end_snapshot' if meta.get('final_equity') is not None
                                           else 'end_account_snapshot')
        elif final_equity is not None:
            meta['final_equity_source'] = 'adapter; latest account snapshot precedes the period end'
        else:
            meta['final_equity_source'] = None
    result['summary'] = dict(closed_trades=closed, open_trades=opened, wins=wins, losses=losses, flats=flats,
        win_rate_percent=wins / closed * 100 if closed else None,
        closed_net_pnl=float(net_total), closed_gross_pnl=float(gross_total),
        total_fees=float(fees_total), total_other_costs=float(costs_total),
        closed_balance=float(balance), final_equity=final_equity,
        account_snapshot_time=account_snapshot_time,
        balance_basis=basis, accounting_mode=mode,
        rule_checks=sum(len(trade['checks']) for trade in trades))
    return result


def local_text(seconds, zone):
    return datetime.fromtimestamp(seconds, ZoneInfo(zone)).isoformat()


def write_ledger(path, result):
    meta = result['meta']
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['交易', '方向', '进场时间', '进场价', '止损点', '止盈点', '平仓时间或区间', '平仓价',
                         'Win/Loss（净）', '盈利（净）', '亏损（净）', '手续费', '其他成本', '交易后金额',
                         '数量', '名义仓位', '保证金', '杠杆', '记账货币', '价格币种', '数量单位'])
        for trade in result['trades']:
            net = trade['net_pnl']
            exit_label = ''
            if trade['status'] == 'closed':
                exit_label = (local_text(trade['exit_time'], meta['timezone']) if trade['exit_time'] is not None else
                    local_text(trade['exit_bar_start'], meta['timezone']) + ' — ' + local_text(trade['exit_bar_end'], meta['timezone']))
            writer.writerow([trade['id'], '多' if trade['direction'] == 'long' else '空',
                local_text(trade['entry_time'], meta['timezone']), number(trade['entry_price']), number(trade['stop']),
                number(trade['target']), exit_label, number(trade['exit_price']), result_name(net),
                number(max(0, net)) if net is not None else '', number(max(0, -net)) if net is not None else '',
                number(trade['fees']), number(trade['other_costs']), number(trade['balance_after']),
                number(trade['quantity']), number(trade['entry_notional']), number(trade['margin_used']),
                number(trade.get('leverage', meta['sizing']['leverage'])), meta['currency'], meta['price_currency'], meta.get('quantity_unit', '')])


def inline(value):
    return '`' + str(value).replace('`', 'ˋ').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ') + '`'


def write_report(output, result):
    meta, summary = result['meta'], result['summary']
    zone = meta['timezone']
    daily = defaultdict(Decimal)
    for trade in result['trades']:
        if trade['status'] == 'closed':
            time = trade['exit_time'] if trade['exit_time'] is not None else trade['exit_bar_end']
            day = datetime.fromtimestamp(time, ZoneInfo(zone)).date().isoformat()
            daily[day] += decimal(trade['net_pnl'])
    # File charts accept at most 60 rows; retain sums by adjacent exit-day groups.
    ordered = sorted(daily.items())
    size = max(1, math.ceil(len(ordered) / 60))
    rows = []
    for first in range(0, len(ordered), size):
        batch = ordered[first:first + size]
        label = batch[0][0] if len(batch) == 1 else batch[0][0] + '至' + batch[-1][0]
        rows.append((label, number(sum((value for _, value in batch), Decimal(0)))))
    with (output / 'daily.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['日期或聚合区间', '净盈亏']); writer.writerows(rows)
    chart = ('@chart type=bar data=daily.csv x=日期或聚合区间 y=净盈亏 unit=' + json.dumps(meta['currency']) +
             ' title="按退出时刻或退出K结束日归集；超60条按相邻日期聚合"') if rows else '本次没有已平仓交易，分日盈亏图为空。'
    currency = inline(meta['currency'])
    win_rate = f"{summary['win_rate_percent']:.2f}%" if summary['win_rate_percent'] is not None else '不适用'
    balance_label = '最新账户快照金额' if meta['accounting_mode'] == 'account_snapshots' else '已平仓账面余额'
    snapshot_facts = ''
    snapshot_note = ''
    if meta['accounting_mode'] == 'account_snapshots':
        snapshot_facts = f"\n| 最新快照时刻 | {inline(local_text(summary['account_snapshot_time'], zone))} |"
        if summary['account_snapshot_time'] < meta['end_time']:
            snapshot_note = ('最新快照早于区间终点。期末权益由适配器另行提供，早先快照不核验该期末值。'
                             if summary['final_equity'] is not None else
                             '最新快照早于区间终点；本次未提供期末权益，保留空值。')
        else:
            snapshot_note = '期末权益取区间终点账户快照，并与适配器提供值核对。' if meta.get('final_equity') is not None else '期末权益取区间终点账户快照。'
    strategy_description = inline(meta.get('strategy_description') or '策略说明未提供；以本次适配器与逐笔条件为准。')
    cost_model = meta.get('costs', meta.get('cost_model'))
    if cost_model is None:
        cost_model = {key: meta[key] for key in ('fee_percent', 'slippage_ticks', 'slippage_model', 'funding_model') if key in meta}
    costs_label = inline(json.dumps(cost_model, ensure_ascii=False, separators=(',', ':')) if cost_model else '适配器未注明费率与滑点模型；总额见成本统计。')
    parameters_label = inline(json.dumps(meta.get('parameters', {}), ensure_ascii=False, separators=(',', ':')) if meta.get('parameters') else '以策略说明与逐笔检查为准。')
    limits = meta.get('limitations', []) or ['适配器未声明额外边界；交付前仍需核对成交与费用模型。']
    limit_rows = '\n'.join('| ' + inline(item) + ' |' for item in limits)
    rules = sorted({check['label'] for trade in result['trades'] for check in trade['checks']})
    rule_rows = '\n'.join('| ' + inline(label) + ' |' for label in rules) or '| 本次未提供逐笔规则检查；需由策略适配器补充。 |'
    check_statement = (f"本次提供{summary['rule_checks']}项逐笔条件检查，均通过。检查内容由本次策略适配器提供。"
                       if summary['rule_checks'] else '本次未提供逐笔条件检查，需由策略适配器补充。')
    closed_fees = sum((decimal(t['fees']) for t in result['trades'] if t['status'] == 'closed'), Decimal(0))
    closed_costs = sum((decimal(t['other_costs']) for t in result['trades'] if t['status'] == 'closed'), Decimal(0))
    scope = meta['symbol'] + ' · ' + meta['interval'] + ' · ' + meta['timezone']
    # Report prose is fixed; supplied labels/descriptions are literal inline code.
    text = f'''# 策略回测与逐笔交易账单 {{#backtest-result}}

本次已平仓 **{summary['closed_trades']} 笔**。扣除费用及其他成本后，净盈亏为 **{summary['closed_net_pnl']:.6f}** {currency}，净胜率为{win_rate}。

- {summary['wins']} 笔净盈利、{summary['losses']} 笔净亏损、{summary['flats']} 笔持平。
- 初始资金{number(meta['initial_capital'])} {currency}；每笔金额含义由本次仓位配置确定。
- 未平仓{summary['open_trades']}笔，不计入已平仓胜率。
- 点击“逐笔交易表”查看价格、费用和账户金额。

@answer scope={json.dumps(scope, ensure_ascii=False)}
@meta locale=zh-CN style=clean

## 本次使用什么策略和参数？ {{#configuration}}

| 项目 | 本次输入 |
| --- | --- |
| 策略 | {inline(meta['strategy_name'])} |
| 策略说明 | {strategy_description} |
| 策略参数 | {parameters_label} |
| 交易品种 | {inline(meta['symbol'])} |
| 交易市场 | {inline(meta['market'])} |
| 区间起点 | {inline(local_text(meta['start_time'], zone))} |
| 区间终点 | {inline(local_text(meta['end_time'], zone))} |
| K线周期与时区 | {inline(meta['interval'])}；{inline(zone)} |
| 记账货币 | {currency} |
| 价格币种 | {inline(meta['price_currency'])} |
| 数量单位 | {inline(meta.get('quantity_unit', '适配器未注明'))} |
| 初始本金 | {number(meta['initial_capital'])} |
| 每笔金额类型 | {inline(meta['sizing']['mode'])} |
| 每笔金额参数 | {number(meta['sizing']['value'])} |
| 仓位说明 | {inline(meta['sizing']['description'])} |
| 杠杆 | {number(meta['sizing']['leverage'])}倍；数量由引擎计算，展示层不再乘杠杆 |
| 成本模型 | {costs_label} |
| 记账方式 | {inline(meta['accounting_mode'])}；余额基准{inline(meta['balance_basis'])} |
| 指标 | {inline('、'.join(i['name'] for i in result['indicators']) or '无')} |

## 结果和成本是多少？ {{#results}}

@facts
| 已平仓 | {summary['closed_trades']}笔 |
| 净胜率 | {win_rate} |
| {balance_label} | {number(summary['closed_balance'])} {currency} |{snapshot_facts}
| 期末权益 | {number(summary['final_equity']) if summary['final_equity'] is not None else '未提供'} {currency} |

{chart}

已平仓净盈亏 = 毛盈亏 − 手续费 − 其他成本：{number(summary['closed_gross_pnl'])} − {number(closed_fees)} − {number(closed_costs)} = {number(summary['closed_net_pnl'])} {currency}。

全部已发生手续费为{number(summary['total_fees'])} {currency}，其他成本为{number(summary['total_other_costs'])} {currency}。

{inline(meta['balance_description'])}

{snapshot_note}

## 怎样核对开单条件和图表？ {{#rule-checks}}

{check_statement}

@list
{rule_rows}

图表按本次指标列表绘制。未设置的止损或止盈保持空值。

存在信号K时用蓝框标出；仓位区域从实际入场时刻开始。动态止盈止损按适配器提供的轨迹显示，仓位框仅展示入场初始风险。

## 数据和模拟有哪些边界？ {{#scope}}

数据来源：{inline(meta['source_description'])}。

@callout level=warning title="本次适配器声明的边界"
{limit_rows}

本模板核对记录和记账关系。指标、交易日历、成交顺序、资金费和强平模型由所选引擎与适配器负责。

## 交付前如何检查？ {{#checklist}}

- [ ] 核对策略、品种、市场、时间区间和时区。
- [ ] 核对本金、每笔金额含义、杠杆及真实数量。
- [ ] 逐笔查看入场条件、止损止盈及退出规则。
- [ ] 核对净盈亏、手续费、其他成本及账户金额。
- [ ] 对照原始行情和引擎日志检查异常成交。
'''
    (output / 'candidate.archify.md').write_text(text, encoding='utf-8')


def resolve_archify(explicit=None):
    candidates = [Path(explicit).expanduser()] if explicit else [
        Path.home() / '.agents/skills/archify/bin/archify.mjs',
        Path.home() / '.codex/skills/archify/bin/archify.mjs']
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise ValueError('Archify CLI unavailable; use --archify-cli. Validated records and draft were preserved.')


def json_script(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        .replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
        .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def render_fragment(result, path):
    template = (ROOT / 'assets/trade_chart.template.html').read_text(encoding='utf-8')
    if template.count('__TRADE_DATA_JSON__') != 1:
        raise ValueError('template must contain one data placeholder')
    fragment = template.replace('__TRADE_DATA_JSON__', json_script(result))
    tag = '<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>'
    if fragment.count(tag) != 1:
        raise ValueError('template must contain the pinned chart dependency tag')
    library = (ROOT / 'assets/chart_assets/lightweight-charts-4.2.3.js').read_text(encoding='utf-8')
    fragment = fragment.replace(tag, '<script>\n' + library.replace('</script', '<\\/script') + '\n</script>')
    if len(fragment.encode('utf-8')) >= 16_000_000:
        raise ValueError('inline chart exceeds 16MB; split the requested range')
    path.write_text(fragment, encoding='utf-8')


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--result', required=True, type=Path)
    cli.add_argument('--output-dir', required=True, type=Path)
    cli.add_argument('--archify-cli', type=Path)
    cli.add_argument('--node')
    args = cli.parse_args(argv)
    try:
        raw = json.loads(args.result.read_text(encoding='utf-8'))
        result = validate_result(raw)
        output = args.output_dir.resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError('output-dir must be new or empty')
        output.mkdir(parents=True, exist_ok=True)
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        (output / 'summary.json').write_text(json.dumps(result['summary'], ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        write_ledger(output / 'trade-ledger.csv', result)
        write_report(output, result)
        render_fragment(result, output / 'chart-fragment.html')
        archify = resolve_archify(args.archify_cli)
        node = args.node or shutil.which('node')
        if not node:
            raise ValueError('Node unavailable; use --node. Validated records and draft were preserved.')
        report = output / 'backtest-report.html'
        subprocess.run([node, str(archify), 'finalize', 'explainer', str(output / 'candidate.archify.md'), str(report), '--json'], cwd=output, check=True)
        meta = result['meta']
        period = f"{local_text(meta['start_time'], meta['timezone'])} — {local_text(meta['end_time'], meta['timezone'])} · {meta['interval']} · {meta['market']}"
        subprocess.run([sys.executable, str(ROOT / 'scripts/build_archify_review.py'), '--chart-fragment', str(output / 'chart-fragment.html'),
            '--report', str(report), '--output', str(output / 'review.html'), '--title', meta['strategy_name'] + ' · ' + meta['symbol'],
            '--period-label', period], check=True)
        receipt = dict(review_html=str(output / 'review.html'), ledger_csv=str(output / 'trade-ledger.csv'),
                       result_json=str(output / 'result.json'), summary=result['summary'],
                       source_result_sha256=hashlib.sha256(args.result.read_bytes()).hexdigest())
        (output / 'run-summary.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        print(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        cli.exit(1, f'ERROR: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
