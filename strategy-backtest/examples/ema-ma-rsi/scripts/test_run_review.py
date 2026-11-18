import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from run_review import (BEIJING, archive_window, daily_rows, local_window,
                        number, write_ledger)


class ReviewTests(unittest.TestCase):
    def test_recent_three_complete_beijing_days(self):
        start, end = local_window(now=datetime(2026, 11, 18, 0, 5, tzinfo=BEIJING))
        self.assertEqual(start.isoformat(), '2026-11-15T00:00:00+08:00')
        self.assertEqual(end.isoformat(), '2026-11-18T00:00:00+08:00')
        self.assertEqual((end - start).days, 3)

    def test_inclusive_range_rejects_unfinished_dates(self):
        now = datetime(2026, 11, 18, 12, tzinfo=BEIJING)
        start, end = local_window('2026-11-10', '2026-11-15', now)
        self.assertEqual((end - start).days, 6)
        with self.assertRaises(ValueError):
            local_window('2026-11-10', '2026-11-18', now)
        with self.assertRaises(ValueError):
            local_window('2026-11-10', None, now)
        with self.assertRaises(ValueError):
            local_window('2026-11-15', '2026-11-10', now)

    def test_archive_bounds_cover_warmup_and_last_beijing_day(self):
        start, end = local_window('2026-11-10', '2026-11-15', datetime(2026, 11, 18, tzinfo=BEIJING))
        first, last = archive_window(start, end, '5m', 14)
        self.assertEqual(first, date(2026, 11, 4))
        self.assertEqual(last, date(2026, 11, 16))
        first_long, _ = archive_window(start, end, '1h', 100)
        self.assertLess(first_long, first)

    def test_net_ledger_and_open_trade(self):
        closed = dict(id=1, direction='long', entry_time=1794240000,
                      entry_fill=100, stop=99, target=101.5, exit_fill=101.5,
                      net_pnl=-0.1, balance_after=9999.9, fees=1.6,
                      quantity=1, entry_notional=100, margin_used=10, leverage=10)
        opened = dict(closed, id=2, net_pnl=None, balance_after=None, exit_fill=None, fees=0.5)
        summary = dict(config=dict(initial_capital=10000), closed_trades=1, closed_net_pnl=-0.1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.csv'
            balance = write_ledger(path, summary, dict(trades=[closed, opened]))
            self.assertEqual(str(balance), '9999.9')
            with path.open(encoding='utf-8-sig', newline='') as src:
                rows = list(csv.DictReader(src))
            self.assertEqual(rows[0]['Win/Loss（净）'], 'Loss')
            self.assertEqual(rows[0]['亏损USDT'], '0.1')
            self.assertEqual(rows[0]['盈利USDT'], '0')
            self.assertEqual(rows[1]['Win/Loss（净）'], 'Open')
            self.assertEqual(rows[1]['交易后余额USDT'], '')
            closed['balance_after'] = 9998
            with self.assertRaises(ValueError):
                write_ledger(path, summary, dict(trades=[closed]))

    def test_long_chart_aggregation_keeps_total(self):
        origin = datetime(2025, 1, 1, tzinfo=BEIJING)
        trades = [dict(net_pnl=1.25, exit_bar_end_time=(origin + timedelta(days=i)).timestamp()) for i in range(400)]
        rows = daily_rows(dict(trades=trades))
        self.assertLessEqual(len(rows), 60)
        self.assertEqual(sum(float(value) for _, value in rows), 500)
        self.assertIn('至', rows[0][0])
        self.assertEqual(daily_rows(dict(trades=[])), [])

    def test_number_avoids_binary_tails_and_preserves_integer_zeros(self):
        self.assertEqual(number(10000), '10000')
        self.assertEqual(number(0.5000377000000001), '0.5000377')
        self.assertEqual(number(-0.00000000001), '0')
        self.assertEqual(number(None), '')


if __name__ == '__main__':
    unittest.main()
