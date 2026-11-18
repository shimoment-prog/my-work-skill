import hashlib
import io
import json
import unittest
import urllib.error
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import binance_history as history


class HistoryTest(unittest.TestCase):
    def zipped(self, body, member="ETHUSDT-5m-2025-01.csv", extra=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(member, body)
            if extra:
                archive.writestr(extra, "ignored")
        return buffer.getvalue()

    def test_real_archive_shape_header_and_microseconds(self):
        body = "open_time,open,high,low,close,volume,close_time,quote_volume\n1735689600000000,10,12,9,11,2,1735689899999999,0\n"
        rows = history.archive_rows(self.zipped(body), "ETHUSDT-5m-2025-01.zip", 300000)
        self.assertEqual(rows, [(1735689600000, "10", "12", "9", "11", "2", 1735689899999)])

    def test_unsafe_zip_member_is_rejected_even_without_extraction(self):
        payload = self.zipped("", extra="../outside.csv")
        with self.assertRaisesRegex(history.DataError, "unsafe ZIP"):
            history.archive_rows(payload, "ETHUSDT-5m-2025-01.zip", 300000)

    def test_invalid_ohlc_and_nan_are_rejected(self):
        for price in ("10,9,8,11,2", "NaN,12,9,11,2"):
            body = "1735689600000,{},1735689899999\n".format(price)
            with self.assertRaises(history.DataError):
                history.archive_rows(self.zipped(body), "ETHUSDT-5m-2025-01.zip", 300000)

    def test_missing_internal_candle_is_not_silently_accepted(self):
        start = 1735689600000
        rows = [(start, "1", "1", "1", "1", "0", start + 299999),
                (start + 600000, "1", "1", "1", "1", "0", start + 899999)]
        with self.assertRaisesRegex(history.DataError, "incomplete"):
            history.validate_range(rows, start, start + 900000, 300000, start + 900001)

    def test_identical_duplicates_deduplicate_conflicts_reject(self):
        start = 1735689600000
        row = (start, "1", "1", "1", "1", "0", start + 299999)
        self.assertEqual(history.validate_range([row, row], start, start + 300000, 300000, start + 400000), [row])
        conflict = row[:5] + ("2", row[-1])
        with self.assertRaisesRegex(history.DataError, "conflicting duplicate"):
            history.validate_range([row, conflict], start, start + 300000, 300000, start + 400000)

    def test_unfinished_bar_is_excluded(self):
        start = 1735689600000
        rows = [(start + i * 300000, "1", "1", "1", "1", "0", start + (i + 1) * 300000 - 1) for i in range(2)]
        result = history.validate_range(rows, start, start + 600000, 300000, start + 450000)
        self.assertEqual(result, rows[:1])

    def test_corrupt_cached_checksum_stops_download(self):
        with TemporaryDirectory() as temporary:
            cache = Path(temporary)
            filename = "ETHUSDT-5m-2025-01.zip"
            (cache / filename).write_bytes(b"corrupt")
            (cache / (filename + ".CHECKSUM")).write_text("{}  {}\n".format(hashlib.sha256(b"good").hexdigest(), filename))
            with patch.object(history, "fetch", side_effect=AssertionError("no network expected")):
                with self.assertRaisesRegex(history.DataError, "SHA256 mismatch"):
                    history.archive_bytes("https://data.binance.vision/" + filename, cache)

    def test_date_must_be_exact_iso(self):
        self.assertEqual(history.parse_date("2025-01-01"), date(2025, 1, 1))
        for invalid in ("20250101", "2025-1-1", "2025-02-30", "2025-01-01T00:00"):
            with self.assertRaises(Exception):
                history.parse_date(invalid)


class RestFallbackTest(unittest.TestCase):
    @staticmethod
    def rows(start, count, step):
        return [[start + i * step, "10", "12", "9", "11", "2",
                 start + (i + 1) * step - 1] for i in range(count)]

    def test_rest_pagination_keeps_end_bound_and_caches_raw_responses(self):
        start, step = 1735689600000, 60000
        first = json.dumps(self.rows(start, 1500, step)).encode()
        second = json.dumps(self.rows(start + 1500 * step, 100, step)).encode()
        end = start + 1600 * step
        with TemporaryDirectory() as temporary:
            cache = Path(temporary)
            with patch.object(history, "fetch", side_effect=[first, second]) as fetch:
                rows, sources = history.rest_rows("ETHUSDT", "1m", start, end, end + 1, cache)
            self.assertEqual(len(rows), 1600)
            self.assertEqual(len(sources), 2)
            for call in fetch.call_args_list:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(call.args[0]).query)
                self.assertEqual(query["endTime"], [str(end - 1)])
                self.assertEqual(query["limit"], ["1500"])
            self.assertEqual(sources[0]["sha256"], hashlib.sha256(first).hexdigest())
            self.assertFalse(sources[0]["official_sha256_verified"])
            self.assertEqual(Path(sources[0]["raw_cache_file"]).read_bytes(), first)
            with patch.object(history, "fetch", side_effect=AssertionError("cache should be used")):
                cached_rows, cached_sources = history.rest_rows("ETHUSDT", "1m", start, end, end + 1, cache)
            self.assertEqual(cached_rows, rows)
            self.assertEqual(cached_sources, sources)

    def test_rest_end_excludes_unfinished_candle(self):
        start, step = 1735689600000, 300000
        with TemporaryDirectory() as temporary:
            with patch.object(history, "fetch", return_value=json.dumps(self.rows(start, 1, step)).encode()) as fetch:
                rows, _ = history.rest_rows("ETHUSDT", "5m", start, start + 2 * step,
                                           start + step + step // 2, Path(temporary))
            self.assertEqual(len(rows), 1)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(fetch.call_args.args[0]).query)
            self.assertEqual(query["endTime"], [str(start + step - 1)])

    def test_archive_404_falls_back_to_rest_including_current_utc_day(self):
        fixed_now = datetime(2025, 1, 2, 2, 30, tzinfo=timezone.utc)
        missing = urllib.error.HTTPError("https://archive", 404, "not published", {}, None)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz)

        def response(url, limit):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            first = int(query["startTime"][0])
            final = int(query["endTime"][0]) + 1
            return json.dumps(self.rows(first, (final - first) // 3600000, 3600000)).encode()

        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "history.csv"
            with patch.object(history, "datetime", Clock), \
                 patch.object(history, "archive_bytes", side_effect=missing), \
                 patch.object(history, "fetch", side_effect=response):
                metadata = history.download("ETHUSDT", "1h", date(2025, 1, 1), date(2025, 1, 3),
                                            output, Path(temporary) / "cache")
            self.assertEqual(metadata["rows"], 26)
            self.assertEqual(metadata["last_close_time_ms"], int(datetime(2025, 1, 2, 2, tzinfo=timezone.utc).timestamp() * 1000) - 1)
            self.assertTrue(all(source["kind"] == "rest" for source in metadata["sources"]))
            self.assertTrue(all(not source["official_sha256_verified"] for source in metadata["sources"]))
            self.assertEqual(metadata["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_api_error_and_nonadvancing_response_are_rejected(self):
        start, step = 1735689600000, 300000
        for response in ({"code": -1121, "msg": "invalid symbol"},
                         self.rows(start, 1, step) * 2):
            with self.subTest(response=response), TemporaryDirectory() as temporary:
                with patch.object(history, "fetch", return_value=json.dumps(response).encode()):
                    with self.assertRaises(history.DataError):
                        history.rest_rows("ETHUSDT", "5m", start, start + step, start + step + 1, Path(temporary))

    def test_non_404_archive_errors_do_not_trigger_rest(self):
        forbidden = urllib.error.HTTPError("https://archive", 403, "forbidden", {}, None)
        with TemporaryDirectory() as temporary:
            with patch.object(history, "archive_bytes", side_effect=forbidden), \
                 patch.object(history, "rest_rows", side_effect=AssertionError("must not bypass denial")):
                with self.assertRaises(urllib.error.HTTPError):
                    history.download("ETHUSDT", "5m", date(2025, 1, 1), date(2025, 1, 2),
                                     Path(temporary) / "history.csv", Path(temporary) / "cache")


if __name__ == "__main__":
    unittest.main()
