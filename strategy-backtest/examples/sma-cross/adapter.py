#!/usr/bin/env python3
"""A complete long-only SMA-cross adapter for linear unit-price instruments.

The example confirms signals at candle close, fills at the next available
candle's open, and exits on the opposite signal. It intentionally has no RSI,
fixed stop, or fixed target. It is an example strategy, not a universal matcher.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path


def epoch(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return int(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamps require an explicit UTC offset')
        return int(parsed.timestamp())
    raise ValueError('time must be UTC epoch seconds or an ISO timestamp with offset')


def finite(value, label, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} must be numeric') from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f'{label} must be finite' + (' and positive' if positive else ''))
    return result


def verified_source(path, requested_meta):
    """Bind this CSV to its sidecar without promoting a local hash to provenance."""
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {'data_file': str(path.resolve()), 'data_sha256': digest}
    sidecar = Path(str(path) + '.metadata.json')
    if not sidecar.exists():
        result['source_description'] = '用户本地OHLCV CSV；没有相邻来源元数据，来源未核验。'
        return result
    metadata = json.loads(sidecar.read_text(encoding='utf-8'))
    if not isinstance(metadata, dict):
        raise ValueError('CSV metadata must be a JSON object')
    for field in ('symbol', 'interval'):
        expected = requested_meta.get(field)
        if not expected or metadata.get(field) != expected:
            raise ValueError(f'CSV metadata {field} differs from requested {expected!r}')
    if metadata.get('output_sha256') != digest:
        raise ValueError('CSV SHA256 differs from its source metadata')
    result['data_source_metadata'] = metadata
    result['source_description'] = (
        '本地CSV与来源侧车的品种、周期及SHA256一致；原始下载凭据未在本示例重新核验。'
    )
    return result


def candles_from_csv(path):
    required = {'open_time_ms', 'open', 'high', 'low', 'close', 'volume', 'close_time_ms'}
    bars = []
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('CSV needs columns ' + ', '.join(sorted(required)))
        for line, row in enumerate(reader, 2):
            opened = int(row['open_time_ms'])
            ended = int(row['close_time_ms']) + 1
            if opened % 1000 or ended % 1000 or ended <= opened:
                raise ValueError(f'invalid candle timestamps at row {line}')
            bar = {'time': opened // 1000, 'end_time': ended // 1000,
                   **{key: finite(row[key], f'{key} row {line}') for key in ('open', 'high', 'low', 'close', 'volume')}}
            if not 0 < bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high'] or bar['volume'] < 0:
                raise ValueError(f'invalid OHLCV at row {line}')
            if bars and bar['time'] < bars[-1]['end_time']:
                raise ValueError(f'overlapping, duplicate, or unsorted candles at row {line}')
            bars.append(bar)
    if not bars:
        raise ValueError('empty CSV')
    return bars


def sized_quantity(settings, entry_price, cash, step):
    mode = settings['mode']
    value, leverage = settings['value'], settings['leverage']
    if mode == 'fixed_margin':
        target_notional = value * leverage
        raw = target_notional / entry_price
    elif mode == 'fixed_notional':
        raw = value / entry_price
    elif mode == 'quantity':
        raw = value
    elif mode == 'equity_percent':
        # Explicit sample convention: the percentage specifies NOTIONAL, not margin.
        raw = (cash * value / 100) / entry_price
    else:
        raise ValueError('example supports fixed_margin, fixed_notional, quantity, or equity_percent sizing')
    quantum = Decimal(str(step))
    return float((Decimal(str(raw)) / quantum).quantize(Decimal('1'), rounding=ROUND_FLOOR) * quantum)


def run(bars, request):
    meta_in = request.get('meta', request)
    required = ('strategy_name', 'market', 'symbol', 'currency', 'interval', 'timezone',
                'start_time', 'end_time', 'initial_capital', 'sizing')
    for key in required:
        if key not in meta_in:
            raise ValueError(f'request.meta.{key} is required')
    params = request.get('strategy', meta_in.get('strategy'))
    if not isinstance(params, dict) or not all(key in params for key in ('fast', 'slow')):
        raise ValueError('request.strategy.fast and slow are required')
    fast, slow = params['fast'], params['slow']
    if isinstance(fast, bool) or isinstance(slow, bool) or not isinstance(fast, int) or not isinstance(slow, int) or not 0 < fast < slow:
        raise ValueError('SMA periods must be integers with 0 < fast < slow')
    start, end = epoch(meta_in['start_time']), epoch(meta_in['end_time'])
    if start >= end:
        raise ValueError('start_time must precede exclusive end_time')
    capital = finite(meta_in['initial_capital'], 'initial_capital', True)
    sizing = dict(meta_in['sizing'])
    for key in ('mode', 'value', 'leverage', 'description'):
        if key not in sizing:
            raise ValueError(f'sizing.{key} is required')
    sizing['value'] = finite(sizing['value'], 'sizing.value', True)
    sizing['leverage'] = finite(sizing['leverage'], 'sizing.leverage', True)
    if sizing['leverage'] < 1:
        raise ValueError('sizing.leverage must be at least 1')
    if sizing['mode'] not in ('fixed_margin', 'fixed_notional', 'quantity', 'equity_percent'):
        raise ValueError('unsupported sizing.mode for SMA example')
    if sizing['mode'] == 'equity_percent' and sizing['value'] > 100:
        raise ValueError('equity_percent value must not exceed 100')
    fee_percent = finite(request.get('fee_percent', meta_in.get('fee_percent')), 'fee_percent')
    if fee_percent < 0:
        raise ValueError('fee_percent cannot be negative')
    step = finite(request.get('quantity_step', meta_in.get('quantity_step', meta_in.get('qty_step'))), 'quantity_step', True)
    bars = [bar for bar in bars if bar['end_time'] <= end]
    if not bars or bars[-1]['end_time'] < end or bars[0]['time'] > start:
        raise ValueError('CSV must cover the requested complete candle window')
    if len(bars) < slow + 1:
        raise ValueError('not enough candles to establish a crossover')
    fee = fee_percent / 100
    cash, position, pending = capital, None, None
    trades, equity, fast_points, slow_points = [], [], [], []
    closes, previous = [], None
    rejected = 0
    peak, max_drawdown = capital, 0.0
    for index, bar in enumerate(bars):
        # Pending signals only contain data from the PREVIOUS completed candle.
        if pending is not None and start <= bar['time'] < end:
            action, signal = pending
            if action == 'exit' and position is not None:
                trade = position
                exit_price = bar['open']
                gross = trade['quantity'] * (exit_price - trade['entry_price'])
                exit_fee = trade['quantity'] * exit_price * fee
                cash += gross - exit_fee
                net = gross - trade['entry_fee'] - exit_fee
                trade.update(status='closed', exit_time=bar['time'], exit_bar_time=bar['time'],
                             exit_bar_end_time=bar['end_time'], exit_price=exit_price,
                             gross_pnl=gross, exit_fee=exit_fee, fees=trade['entry_fee'] + exit_fee,
                             net_pnl=net, balance_after=cash, exit_reason='opposite_signal')
                position = None
            elif action == 'entry' and position is None:
                entry = bar['open']
                quantity = sized_quantity(sizing, entry, cash, step)
                notional = quantity * entry
                margin = notional / sizing['leverage']
                entry_fee = notional * fee
                if quantity <= 0 or margin + entry_fee > cash + 1e-9:
                    rejected += 1
                else:
                    cash -= entry_fee
                    trade = {
                        'id': len(trades) + 1, 'direction': 'long', 'status': 'open',
                        'entry_time': bar['time'], 'entry_bar_time': bar['time'],
                        'entry_price': entry, 'entry_fill': entry,
                        'key_time': signal['time'], 'signal_time': signal['end_time'],
                        'exit_time': None, 'exit_bar_time': None, 'exit_bar_end_time': None,
                        'exit_price': None, 'stop': None, 'target': None,
                        'quantity': quantity, 'entry_notional': notional, 'margin_used': margin,
                        'leverage': sizing['leverage'], 'entry_fee': entry_fee, 'exit_fee': 0.0,
                        'fees': entry_fee, 'other_costs': 0.0, 'gross_pnl': None,
                        'net_pnl': None, 'balance_after': None, 'exit_reason': 'open',
                        'checks': [
                            {'label': 'SMA上穿确认',
                             'value': f"前根快/慢 {signal['previous_fast']:.8f}/{signal['previous_slow']:.8f}；信号收盘快/慢 {signal['fast']:.8f}/{signal['slow']:.8f}",
                             'pass': signal['previous_fast'] <= signal['previous_slow'] and signal['fast'] > signal['slow']},
                            {'label': '下一根开盘成交',
                             'value': f"信号K开盘 {signal['time']}，确认 {signal['end_time']}；下一根开盘 {bar['time']}，成交 {entry}",
                             'pass': index == signal['index'] + 1 and bar['time'] >= signal['end_time']},
                        ],
                    }
                    trades.append(trade)
                    position = trade
        pending = None
        closes.append(bar['close'])
        fast_value = sum(closes[-fast:]) / fast if len(closes) >= fast else None
        slow_value = sum(closes[-slow:]) / slow if len(closes) >= slow else None
        if fast_value is not None:
            fast_points.append({'time': bar['time'], 'value': fast_value})
        if slow_value is not None:
            slow_points.append({'time': bar['time'], 'value': slow_value})
        if previous is not None and slow_value is not None and start <= bar['end_time'] < end:
            prev_fast, prev_slow = previous
            up = prev_fast <= prev_slow and fast_value > slow_value
            down = prev_fast >= prev_slow and fast_value < slow_value
            signal = dict(bar, index=index, previous_fast=prev_fast, previous_slow=prev_slow,
                          fast=fast_value, slow=slow_value)
            if up and position is None:
                pending = ('entry', signal)
            elif down and position is not None:
                pending = ('exit', signal)
        if slow_value is not None:
            previous = fast_value, slow_value
        if bar['end_time'] >= start:
            floating = position['quantity'] * (bar['close'] - position['entry_price']) if position else 0.0
            value = cash + floating
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
            equity.append({'time': bar['end_time'], 'cash': cash, 'equity': value,
                           'margin_used': position['margin_used'] if position else 0.0})
    closed = [t for t in trades if t['status'] == 'closed']
    net_total = sum(t['net_pnl'] for t in closed)
    limitations = list(meta_in.get('limitations', [])) + [
        '示例为单笔只做多SMA交叉，收盘确认、下一根可用K开盘成交；没有固定止盈止损。',
        '线性单位价格盈亏；未模拟合约乘数、逆向合约、税费、公司行为、资金费或强平。',
        '手续费按单边名义金额计；无滑点；不宣称适用股票T+1或所有市场撮合。',
        'equity_percent在此示例指权益百分比作为名义仓位；杠杆仅决定所需保证金。',
    ]
    meta = dict(meta_in)
    meta.update(start_time=start, end_time=end, initial_capital=capital, sizing=sizing,
                strategy_description=f'SMA{fast}上穿SMA{slow}后下一根开盘做多；下穿后下一根开盘平仓。',
                source_description=meta_in.get('source_description', '用户提供的本地OHLCV CSV，来源未核验。'),
                fee_percent=fee_percent, quantity_step=step, limitations=limitations,
                accounting_mode='closed_pnl', balance_basis='cash',
                balance_description='已平仓后的累计已实现余额；期末Open交易费用及浮动盈亏另见期末权益。',
                final_equity=equity[-1]['equity'], entry_window='[start_time,end_time)',
                execution_model='收盘确认信号，下一根可用K开盘成交；结束边界不下新单。')
    return {
        'schema_version': 1, 'meta': meta, 'candles': bars,
        'indicators': [
            {'id': 'sma-fast', 'name': f'SMA{fast}', 'label': f'SMA{fast}', 'pane': 'price', 'type': 'line', 'color': '#f5a544', 'values': fast_points},
            {'id': 'sma-slow', 'name': f'SMA{slow}', 'label': f'SMA{slow}', 'pane': 'price', 'type': 'line', 'color': '#7caef5', 'values': slow_points},
        ],
        'trades': trades, 'equity': equity,
        'summary': {'closed_trades': len(closed), 'winning_trades': sum(t['net_pnl'] > 0 for t in closed),
                    'losing_trades': sum(t['net_pnl'] < 0 for t in closed),
                    'breakeven_trades': sum(t['net_pnl'] == 0 for t in closed),
                    'win_rate_percent': sum(t['net_pnl'] > 0 for t in closed) / len(closed) * 100 if closed else None,
                    'closed_net_pnl': net_total, 'total_fees_paid': sum(t['fees'] for t in trades),
                    'final_cash': cash, 'final_equity': equity[-1]['equity'],
                    'max_drawdown_percent': max_drawdown, 'open_trades': sum(t['status'] == 'open' for t in trades),
                    'rejected_entries': rejected},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding='utf-8'))
        source = verified_source(args.csv, request.get('meta', request))
        request.get('meta', request)['source_description'] = source['source_description']
        result = run(candles_from_csv(args.csv), request)
        result['meta'].update(source)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')
    print(json.dumps({'result': str(args.output.resolve()), 'trades': len(result['trades']), **result['summary']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
