#!/usr/bin/env python3
"""A-share daily PE history from Eastmoney.

Usage:
  python3 scripts/pe_history.py 300502
  python3 scripts/pe_history.py 300502 --live-price 172.55 --years 5

Prints one JSON object. The live price is not part of the sample.
Median is the linearly interpolated 50th percentile of daily PE_TTM.
"""

import argparse
import json
import sys
import urllib.request
from datetime import date, timedelta


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def percentile(sorted_values, p):
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = p * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    weight = pos - lo
    return sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight


def load_rows(code, start):
    rows = []
    page = 1
    pages = 1
    while page <= pages:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPT_VALUEANALYSIS_DET"
            "&columns=TRADE_DATE,CLOSE_PRICE,PE_TTM,PE_LAR"
            f"&filter=(SECURITY_CODE=%22{code}%22)(TRADE_DATE%3E=%27{start}%27)"
            f"&pageNumber={page}&pageSize=500&sortTypes=1&sortColumns=TRADE_DATE"
            "&source=WEB&client=WEB"
        )
        payload = fetch(url)
        if not payload.get("success"):
            raise SystemExit(payload.get("message") or "eastmoney returned no data")
        result = payload.get("result") or {}
        batch = result.get("data") or []
        rows.extend(batch)
        pages = result.get("pages") or 1
        page += 1
    by_date = {}
    for row in rows:
        by_date[row["TRADE_DATE"][:10]] = row
    return [by_date[day] for day in sorted(by_date)]


def num(value):
    if value is None:
        return None
    return float(value)


def round_or_none(value, digits=2):
    if value is None:
        return None
    return round(value, digits)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("code", help="six-digit A-share code, e.g. 300502")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--live-price", type=float, default=None)
    args = parser.parse_args()
    code = args.code.strip()
    if len(code) != 6 or not code.isdigit():
        raise SystemExit("code must be a 6-digit A-share code")

    start = (date.today() - timedelta(days=365 * args.years)).isoformat()
    rows = load_rows(code, start)
    if not rows:
        raise SystemExit(f"no rows for {code} since {start}")

    sample = []
    dropped = 0
    lar_sample = []
    for row in rows:
        pe = num(row.get("PE_TTM"))
        if pe is None or pe <= 0:
            dropped += 1
            continue
        sample.append(
            {
                "date": row["TRADE_DATE"][:10],
                "close": num(row.get("CLOSE_PRICE")),
                "pe_ttm": pe,
                "pe_lar": num(row.get("PE_LAR")),
            }
        )
        lar = num(row.get("PE_LAR"))
        if lar is not None and lar > 0:
            lar_sample.append(lar)

    ttm = sorted(row["pe_ttm"] for row in sample)
    lar_sorted = sorted(lar_sample)

    years = {}
    month_end = {}
    for row in sample:
        years.setdefault(row["date"][:4], []).append(row)
        month_end[row["date"][:7]] = row

    year_rows = []
    for year in sorted(years):
        group = years[year]
        values = sorted(row["pe_ttm"] for row in group)
        year_rows.append(
            {
                "year": year,
                "n": len(group),
                "median": round_or_none(percentile(values, 0.5), 1),
                "min": round_or_none(values[0], 1),
                "max": round_or_none(values[-1], 1),
                "last": round_or_none(group[-1]["pe_ttm"], 1),
                "last_date": group[-1]["date"],
            }
        )

    last = sample[-1]
    extreme_min = min(sample, key=lambda row: row["pe_ttm"])
    extreme_max = max(sample, key=lambda row: row["pe_ttm"])
    out = {
        "code": code,
        "source": "eastmoney RPT_VALUEANALYSIS_DET PE_TTM",
        "start": start,
        "sample_first": sample[0]["date"],
        "sample_last": last["date"],
        "n": len(sample),
        "dropped_nonpositive": dropped,
        "median": round_or_none(percentile(ttm, 0.5)),
        "p10": round_or_none(percentile(ttm, 0.1)),
        "p25": round_or_none(percentile(ttm, 0.25)),
        "p75": round_or_none(percentile(ttm, 0.75)),
        "p90": round_or_none(percentile(ttm, 0.9)),
        "pe_lar_median": round_or_none(percentile(lar_sorted, 0.5)),
        "pe_lar_n": len(lar_sample),
        "min": {
            "date": extreme_min["date"],
            "close": extreme_min["close"],
            "pe": round_or_none(extreme_min["pe_ttm"]),
        },
        "max": {
            "date": extreme_max["date"],
            "close": extreme_max["close"],
            "pe": round_or_none(extreme_max["pe_ttm"]),
        },
        "last_close": {
            "date": last["date"],
            "close": last["close"],
            "pe_ttm": round(last["pe_ttm"], 4),
            "pe_lar": round_or_none(last["pe_lar"], 4),
        },
        "years": year_rows,
        "month_end": [
            {
                "ym": ym,
                "date": month_end[ym]["date"],
                "pe": round_or_none(month_end[ym]["pe_ttm"]),
                "close": month_end[ym]["close"],
            }
            for ym in sorted(month_end)
        ],
    }
    if args.live_price is not None:
        live_ttm = last["pe_ttm"] * args.live_price / last["close"]
        live_lar = None
        if last["pe_lar"] is not None and last["pe_lar"] > 0:
            live_lar = last["pe_lar"] * args.live_price / last["close"]
        days_le = sum(1 for value in ttm if value <= live_ttm + 1e-9)
        out["live"] = {
            "price": args.live_price,
            "pe_ttm": round(live_ttm, 2),
            "pe_lar": round_or_none(live_lar),
            "days_le": days_le,
            "pct_le": round(100 * days_le / len(sample), 1),
            "in_sample": False,
        }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
