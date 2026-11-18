import copy
import csv
import tempfile
import unittest
from pathlib import Path

from render_review import (json_script, result_name, validate_result, write_ledger,
                           write_report)


START = 1_700_000_000


def fixture(currency='USD', symbol='AAPL', market='NASDAQ'):
    return {
        'schema_version': 1,
        'meta': dict(strategy_name='Price breakout', strategy_description='以此前区间突破入场，时间退出。',
            symbol=symbol, market=market, currency=currency, timezone='America/New_York', interval='1m',
            start_time=START, end_time=START + 600, initial_capital=12000,
            sizing=dict(mode='fixed_notional', value=300, leverage=1, description='每单名义金额300，不使用杠杆'),
            source_description='Isolated test candles, not market data', limitations=['测试行情仅供模板验证']),
        'candles': [dict(time=START + i * 60, open=100, high=104, low=99, close=102) for i in range(10)],
        'indicators': [],
        'trades': [dict(id='first', direction='long', status='closed', entry_time=START + 60,
            entry_price=100, quantity=3, stop=None, target=None, exit_time=START + 120,
            exit_price=107, key_time=START, gross_pnl=21, fees=2, other_costs=4,
            net_pnl=15, balance_after=12015, exit_reason='time',
            checks=[dict(label='区间突破', value='收盘高于此前区间高点', **{'pass': True})])],
    }


class RendererTests(unittest.TestCase):
    def test_neutral_currency_no_indicators_and_null_brackets(self):
        raw = fixture('CNY', '600000', 'SSE')
        result = validate_result(raw)
        self.assertEqual(result['summary']['closed_balance'], 12015)
        self.assertEqual(result['summary']['closed_net_pnl'], 15)
        self.assertIsNone(result['trades'][0]['stop'])
        self.assertEqual(result['indicators'], [])
        self.assertNotIn('summary', raw)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            write_report(path, result)
            write_ledger(path / 'ledger.csv', result)
            text = (path / 'candidate.archify.md').read_text()
            self.assertIn('`CNY`', text)
            self.assertIn('`600000`', text)
            self.assertNotIn('SMA10', text)
            with (path / 'ledger.csv').open(encoding='utf-8-sig', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['止损点'], '')
            self.assertEqual(row['盈利（净）'], '15')
            self.assertEqual(row['交易后金额'], '12015')
            self.assertEqual(row['记账货币'], 'CNY')

    def test_dynamic_levels_and_alternative_indicator(self):
        raw = fixture('USDT', 'BTCUSDT', 'Binance USD-M')
        raw['meta']['sizing'] = dict(mode='fixed_margin', value=75, leverage=3, description='保证金75，3倍杠杆')
        raw['indicators'] = [dict(id='range', name='Donchian upper', pane='price',
            values=[dict(time=START, value=103)], levels=[]),
            dict(id='momentum', name='Rate of change', pane='oscillator',
            values=[dict(time=START, value=-2)], levels=[0])]
        raw['trades'][0]['risk_levels'] = [dict(time=START + 60, stop=97, target=110),
                                         dict(time=START + 100, stop=103, target=None)]
        result = validate_result(raw)
        self.assertEqual(result['trades'][0]['quantity'], 3)
        self.assertEqual(result['summary']['total_other_costs'], 4)
        self.assertEqual(len(result['indicators']), 2)

    def test_price_currency_is_independent_of_account_currency(self):
        raw = fixture('CNY', 'AAPL', 'NASDAQ')
        raw['meta'].update(price_currency='USD', quantity_unit='shares')
        result = validate_result(raw)
        self.assertEqual(result['meta']['price_currency'], 'USD')
        self.assertEqual(result['meta']['currency'], 'CNY')
        self.assertEqual(result['meta']['quantity_unit'], 'shares')
        raw['meta']['quantity_unit'] = ''
        with self.assertRaises(ValueError): validate_result(raw)

    def test_open_costs_do_not_fake_closed_balance_or_equity(self):
        raw = fixture()
        opened = copy.deepcopy(raw['trades'][0])
        opened.update(id='open', status='open', entry_time=START + 180, exit_time=None,
                      exit_price=None, gross_pnl=None, net_pnl=None, balance_after=None, fees=0.5, other_costs=0)
        raw['trades'].append(opened)
        result = validate_result(raw)
        self.assertEqual(result['summary']['closed_balance'], 12015)
        self.assertEqual(result['summary']['total_fees'], 2.5)
        self.assertEqual(result['summary']['open_trades'], 1)
        self.assertIsNone(result['summary']['final_equity'])
        raw['meta']['final_equity'] = 12030.5
        self.assertEqual(validate_result(raw)['summary']['final_equity'], 12030.5)
        raw['trades'][1]['net_pnl'] = 0
        with self.assertRaisesRegex(ValueError, 'Open trade'):
            validate_result(raw)

    def test_net_loss_at_profitable_exit_and_credit_cost(self):
        raw = fixture()
        trade = raw['trades'][0]
        trade.update(gross_pnl=1, fees=3, other_costs=-0.5, net_pnl=-1.5, balance_after=11998.5)
        result = validate_result(raw)
        self.assertEqual(result['summary']['losses'], 1)
        self.assertEqual(result_name(-1.5), 'Loss')
        self.assertEqual(result_name(0), 'Flat')
        self.assertEqual(result_name(None), 'Open')

    def test_wrong_net_balance_and_failed_check_rejected(self):
        for field, value in [('net_pnl', 16), ('balance_after', 11000), ('quantity', 0)]:
            raw = fixture(); raw['trades'][0][field] = value
            with self.assertRaises(ValueError): validate_result(raw)
        raw = fixture(); raw['trades'][0]['checks'][0]['pass'] = False
        with self.assertRaisesRegex(ValueError, 'rule check'): validate_result(raw)

    def test_data_order_nan_and_millisecond_time_rejected(self):
        for field, value in [('time', START * 1000), ('high', float('nan')), ('low', 105)]:
            raw = fixture(); raw['candles'][0][field] = value
            with self.assertRaises(ValueError): validate_result(raw)
        raw = fixture(); raw['candles'][1]['time'] = raw['candles'][0]['time']
        with self.assertRaises(ValueError): validate_result(raw)
        # Weekends and lunch breaks are not treated as missing 24/7 bars.
        raw = fixture(); raw['candles'] = [raw['candles'][0], raw['candles'][5]]
        self.assertEqual(len(validate_result(raw)['candles']), 2)

    def test_unknown_exit_is_range_not_fabricated_timestamp(self):
        raw = fixture(); trade = raw['trades'][0]
        trade.update(exit_time=None, exit_bar_start=START + 60, exit_bar_end=START + 120)
        result = validate_result(raw)
        self.assertIsNone(result['trades'][0]['exit_time'])
        trade['exit_bar_start'] = None
        with self.assertRaises(ValueError): validate_result(raw)

    def test_overlap_requires_explicit_account_ledger(self):
        raw = fixture()
        second = copy.deepcopy(raw['trades'][0]); second.update(id='second', entry_time=START + 90,
                                                               exit_time=START + 180, balance_after=12030)
        raw['trades'].append(second)
        with self.assertRaisesRegex(ValueError, 'overlapping'): validate_result(raw)
        raw['meta'].update(accounting_mode='account_snapshots', balance_basis='cash', final_equity=12043)
        raw['account_events'] = [dict(id='a', time=START + 120, cash=11900, equity=12020),
                                 dict(id='b', time=START + 180, cash=12030, equity=12043)]
        raw['trades'][0].update(account_event_id='a', balance_after=11900)
        raw['trades'][1].update(account_event_id='b', balance_after=12030)
        result = validate_result(raw)
        self.assertEqual(result['summary']['closed_balance'], 12030)
        self.assertEqual(result['summary']['final_equity'], 12043)
        raw['trades'][0]['balance_after'] = 12015
        with self.assertRaisesRegex(ValueError, 'balance_after'): validate_result(raw)
        raw['trades'][0]['balance_after'] = 11900
        raw['account_events'][0]['time'] -= 1
        with self.assertRaisesRegex(ValueError, 'exact exit time'): validate_result(raw)

    def test_required_sizing_basis_and_timezone(self):
        raw = fixture(); raw['meta']['sizing']['mode'] = 'amount'
        with self.assertRaisesRegex(ValueError, 'amount basis'): validate_result(raw)
        raw = fixture(); raw['meta']['timezone'] = 'made-up-zone'
        with self.assertRaisesRegex(ValueError, 'IANA'): validate_result(raw)

    def test_early_snapshot_does_not_become_period_end_equity(self):
        raw = fixture()
        raw['meta'].update(accounting_mode='account_snapshots', balance_basis='equity')
        raw['account_events'] = [dict(id='close', time=START + 120, cash=12015, equity=12020)]
        raw['trades'][0].update(account_event_id='close', balance_after=12020)
        result = validate_result(raw)
        self.assertEqual(result['summary']['closed_balance'], 12020)
        self.assertEqual(result['summary']['account_snapshot_time'], START + 120)
        self.assertIsNone(result['summary']['final_equity'])
        self.assertIsNone(result['meta']['final_equity_source'])
        raw['meta']['final_equity'] = 12100
        result = validate_result(raw)
        self.assertEqual(result['summary']['final_equity'], 12100)
        self.assertIn('adapter', result['meta']['final_equity_source'])
        with tempfile.TemporaryDirectory() as folder:
            write_report(Path(folder), result)
            report = (Path(folder) / 'candidate.archify.md').read_text()
            self.assertIn('最新账户快照金额', report)
            self.assertIn('最新快照时刻', report)
            self.assertIn('最新快照早于区间终点', report)

    def test_end_snapshot_supplies_or_checks_period_end_equity(self):
        raw = fixture()
        raw['meta'].update(accounting_mode='account_snapshots', balance_basis='cash')
        raw['account_events'] = [dict(id='close', time=START + 120, cash=12015, equity=12020),
                                 dict(id='end', time=START + 600, cash=12016, equity=12030)]
        raw['trades'][0].update(account_event_id='close', balance_after=12015)
        result = validate_result(raw)
        self.assertEqual(result['summary']['closed_balance'], 12016)
        self.assertEqual(result['summary']['account_snapshot_time'], START + 600)
        self.assertEqual(result['summary']['final_equity'], 12030)
        self.assertEqual(result['meta']['final_equity_source'], 'end_account_snapshot')
        raw['meta']['final_equity'] = 12030
        self.assertEqual(validate_result(raw)['meta']['final_equity_source'], 'adapter_and_end_snapshot')
        raw['meta']['final_equity'] = 12031
        with self.assertRaisesRegex(ValueError, 'final_equity'): validate_result(raw)

    def test_empty_trades_have_no_win_rate(self):
        raw = fixture(); raw['trades'] = []
        result = validate_result(raw)
        self.assertIsNone(result['summary']['win_rate_percent'])
        self.assertEqual(result['summary']['closed_balance'], 12000)
        self.assertEqual(result['summary']['final_equity'], 12000)

    def test_script_json_escapes_hostile_labels(self):
        value = {'strategy_name': '</script><script>alert(1)</script>&\u2028'}
        encoded = json_script(value)
        self.assertNotIn('<', encoded)
        self.assertNotIn('&', encoded)
        self.assertNotIn('\u2028', encoded)
        import json
        self.assertEqual(json.loads(encoded), value)


if __name__ == '__main__':
    unittest.main()
