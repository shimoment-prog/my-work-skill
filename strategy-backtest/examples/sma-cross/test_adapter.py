import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from adapter import main, run, sized_quantity, verified_source

BASE = 1735689600


def bars():
    closes = [10, 9, 8, 9, 10, 11, 10, 9, 8, 9, 10, 11]
    result = []
    for i, close in enumerate(closes):
        opened = 20 if i == 5 else 30 if i == 8 else close
        result.append(dict(time=BASE + i * 60, end_time=BASE + (i + 1) * 60,
                           open=opened, high=max(opened, close), low=min(opened, close), close=close, volume=1))
    return result


def request():
    return dict(meta=dict(strategy_name='SMA cross sample', market='linear-example', symbol='TEST',
                          currency='USD', interval='1m', timezone='UTC', start_time=BASE,
                          end_time=BASE + 12 * 60, initial_capital=10000,
                          sizing=dict(mode='fixed_margin', value=50, leverage=10,
                                      description='Fixed 50 USD margin at 10x')),
                strategy=dict(fast=2, slow=3), fee_percent=0.1, quantity_step=0.001)


class AdapterTests(unittest.TestCase):
    def test_source_sidecar_binds_symbol_interval_and_file_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / 'candles.csv'
            csv_path.write_text('test data\n')
            sidecar = Path(str(csv_path) + '.metadata.json')
            metadata = {'symbol': 'TEST', 'interval': '1m',
                        'output_sha256': hashlib.sha256(csv_path.read_bytes()).hexdigest()}
            sidecar.write_text(json.dumps(metadata))
            result = verified_source(csv_path, request()['meta'])
            self.assertEqual(result['data_source_metadata'], metadata)
            for field, value in (('symbol', 'BTCUSDT'), ('interval', '5m'), ('output_sha256', '0' * 64)):
                with self.subTest(field=field):
                    changed = dict(metadata, **{field: value})
                    sidecar.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        verified_source(csv_path, request()['meta'])

    def test_source_without_sidecar_is_explicitly_unverified(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / 'candles.csv'
            csv_path.write_text('test data\n')
            result = verified_source(csv_path, request()['meta'])
            self.assertIn('来源未核验', result['source_description'])
            self.assertNotIn('data_source_metadata', result)
            self.assertEqual(result['data_sha256'], hashlib.sha256(csv_path.read_bytes()).hexdigest())

    def test_cli_rejects_mislabelled_symbol_before_writing_result(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / 'ETHUSDT.csv'
            csv_path.write_text('unparsed because source must fail first\n')
            sidecar = Path(str(csv_path) + '.metadata.json')
            sidecar.write_text(json.dumps({'symbol': 'ETHUSDT', 'interval': '1m',
                                          'output_sha256': hashlib.sha256(csv_path.read_bytes()).hexdigest()}))
            requested = request()
            requested['meta']['symbol'] = 'BTCUSDT'
            request_path = Path(folder) / 'request.json'
            request_path.write_text(json.dumps(requested))
            output = Path(folder) / 'result.json'
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(['--csv', str(csv_path), '--request', str(request_path), '--output', str(output)])
            self.assertEqual(raised.exception.code, 1)
            self.assertFalse(output.exists())

    def test_signal_close_fills_next_open_and_exit_uses_next_open(self):
        result = run(bars(), request())
        first = result['trades'][0]
        self.assertEqual(first['key_time'], BASE + 4 * 60)
        self.assertEqual(first['entry_time'], BASE + 5 * 60)
        self.assertEqual(first['entry_price'], 20)
        self.assertEqual(first['exit_time'], BASE + 8 * 60)
        self.assertEqual(first['exit_price'], 30)
        self.assertTrue(all(c['pass'] for c in first['checks']))

    def test_fixed_margin_leverage_is_applied_once_and_costs_reconcile(self):
        first = run(bars(), request())['trades'][0]
        self.assertEqual(first['quantity'], 25)
        self.assertEqual(first['entry_notional'], 500)
        self.assertEqual(first['margin_used'], 50)
        self.assertEqual(first['gross_pnl'], 250)
        self.assertAlmostEqual(first['fees'], 1.25)
        self.assertAlmostEqual(first['net_pnl'], 248.75)
        self.assertAlmostEqual(first['balance_after'], 10248.75)

    def test_no_rsi_stop_or_target_and_final_trade_stays_open(self):
        result = run(bars(), request())
        self.assertEqual(len(result['indicators']), 2)
        self.assertTrue(all(i['pane'] == 'price' for i in result['indicators']))
        self.assertTrue(all(t['stop'] is None and t['target'] is None for t in result['trades']))
        last = result['trades'][-1]
        self.assertEqual(last['status'], 'open')
        for key in ('net_pnl', 'balance_after', 'exit_price', 'exit_time'):
            self.assertIsNone(last[key])
        self.assertEqual(result['summary']['closed_trades'], 1)
        self.assertEqual(result['summary']['open_trades'], 1)

    def test_sizing_modes_have_explicit_different_semantics(self):
        common = dict(value=50, leverage=10)
        self.assertEqual(sized_quantity(dict(common, mode='fixed_margin'), 20, 10000, .001), 25)
        self.assertEqual(sized_quantity(dict(common, mode='fixed_notional'), 20, 10000, .001), 2.5)
        self.assertEqual(sized_quantity(dict(common, mode='quantity'), 20, 10000, .001), 50)
        self.assertEqual(sized_quantity(dict(common, mode='equity_percent', value=10), 20, 10000, .001), 50)

    def test_later_candles_do_not_change_earlier_entry(self):
        before = run(bars(), request())['trades'][0]
        changed = bars()
        for b in changed[9:]:
            b.update(open=100, high=100, low=100, close=100)
        after = run(changed, request())['trades'][0]
        for key in ('entry_time', 'entry_price', 'key_time', 'quantity', 'exit_time', 'exit_price'):
            self.assertEqual(before[key], after[key])

    def test_missing_money_semantics_and_capital_are_rejected(self):
        for field in ('initial_capital', 'sizing'):
            cfg = request()
            del cfg['meta'][field]
            with self.assertRaises(ValueError):
                run(bars(), cfg)
        cfg = request()
        cfg['meta']['sizing']['leverage'] = 0
        with self.assertRaises(ValueError):
            run(bars(), cfg)


if __name__ == '__main__':
    unittest.main()
