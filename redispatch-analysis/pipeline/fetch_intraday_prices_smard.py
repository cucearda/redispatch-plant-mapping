"""
Fetch German quarter-hour electricity spot prices from SMARD.de
(Bundesnetzagentur) and save to a CSV.

What this gets
--------------
By default: the EPEX SPOT "Intraday Continuous" quarter-hour volume-weighted
average price for the DE/LU bidding zone -- SMARD filter 5078.
Coverage on SMARD: 2019-11-17 -> present.

Optional second pull: the Day-Ahead Quarter-Hour Auction price (SMARD filter
5104). This is a DIFFERENT product (forward auction, not continuous intraday)
but extends back to 2015-11-29 in the pre-split DE-AT-LU bidding zone -- if
you want the longest possible 15-min price history.

Why not 2013
------------
Quarter-hour intraday prices before 2015 are not in any free public dataset.
The EPEX intraday quarter-hour product launched in Dec 2011, but historical
tick/average data from EPEX SPOT itself is sold under a commercial licence.
ENTSO-E Transparency Platform starts in 2015 and does not publish intraday
continuous prices either -- only day-ahead. So the realistic options are:

  * Intraday Continuous 15-min  -> SMARD filter 5078, 2019-11 -> present
  * Day-Ahead Auction 15-min    -> SMARD filter 5104, 2015-11 -> present
                                    (DE-AT-LU until 2018-09-30, then DE-LU)
  * Day-Ahead Auction hourly    -> SMARD filter 4169, 2018-10 -> present;
                                    ENTSO-E goes back to 2015-01-01

Set MODE below to "intraday_continuous", "day_ahead_15min", or "both".

Output
------
CSV with columns:
    product             "intraday_continuous" or "day_ahead_15min"
    timestamp_utc       ISO-8601 UTC start of the quarter-hour
    timestamp_local     Europe/Berlin local time (handles DST)
    price_eur_per_mwh   EUR/MWh
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MODE = "intraday_continuous"   # "intraday_continuous" | "day_ahead_15min" | "both"
# WARNING about "day_ahead_15min" (filter 5104): the recent values look like a
# price (~60 EUR/MWh in 2026), but the pre-2018 DE-AT-LU values are ~570 EUR/MWh
# for ordinary winter nights, which is not a plausible spot price. The filter
# label on SMARD may not match what we think it is, or the older bidding zone
# uses a different unit. Verify against SMARD's web UI before using.

# (filter_id, region, label) -- order matters: earlier rows for the same
# timestamp/product are kept, later ones skipped. DE-AT-LU is queried first
# to grab pre-Oct-2018 history before the DE-LU split.
SERIES = {
    "intraday_continuous": [
        (5078, "DE-LU", "intraday_continuous"),
    ],
    "day_ahead_15min": [
        (5104, "DE-AT-LU", "day_ahead_15min"),  # 2015-11 .. 2018-09
        (5104, "DE-LU",    "day_ahead_15min"),  # 2018-10 -> present
    ],
}

USER_AGENT = "Mozilla/5.0 (seminar-paper data fetch; contact: wachsjul2002@gmail.com)"
BASE = "https://www.smard.de/app/chart_data"
RESOLUTION = "quarterhour"
OUTPUT = Path(__file__).with_name("intraday_prices_de_15min.csv")
BERLIN = ZoneInfo("Europe/Berlin")


def http_get_json(url: str, retries: int = 4, backoff: float = 1.5) -> dict | list:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            last_err = e
            if isinstance(e, HTTPError) and e.code == 404:
                raise
            sleep_for = backoff ** attempt
            print(f"  ! {e} -- retry in {sleep_for:.1f}s", file=sys.stderr)
            time.sleep(sleep_for)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def list_weeks(filter_id: int, region: str) -> list[int]:
    url = f"{BASE}/{filter_id}/{region}/index_{RESOLUTION}.json"
    payload = http_get_json(url)
    return sorted(payload.get("timestamps", []))


def fetch_week(filter_id: int, region: str, week_ts_ms: int) -> list[tuple[int, float | None]]:
    url = f"{BASE}/{filter_id}/{region}/{filter_id}_{region}_{RESOLUTION}_{week_ts_ms}.json"
    payload = http_get_json(url)
    out: list[tuple[int, float | None]] = []
    for row in payload.get("series", []):
        if not row:
            continue
        ts_ms = row[0]
        price = row[1] if len(row) > 1 else None
        out.append((ts_ms, price))
    return out


def ms_to_iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def ms_to_iso_berlin(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(BERLIN).isoformat(timespec="seconds")


def collect(label: str, filter_id: int, region: str, seen: set[tuple[str, int]]) -> list[tuple[str, int, float]]:
    print(f"\n[{label} / filter {filter_id} / {region}]")
    weeks = list_weeks(filter_id, region)
    if not weeks:
        print("  no weeks available")
        return []
    print(f"  {len(weeks)} weeks: {ms_to_iso_utc(weeks[0])[:10]} .. {ms_to_iso_utc(weeks[-1])[:10]}")
    rows: list[tuple[str, int, float]] = []
    skipped = 0
    for i, wk in enumerate(weeks, 1):
        print(f"  [{i:4d}/{len(weeks)}] {ms_to_iso_utc(wk)[:10]}", end="\r")
        for ts_ms, price in fetch_week(filter_id, region, wk):
            key = (label, ts_ms)
            if key in seen:
                continue
            if price is None:
                skipped += 1
                continue
            seen.add(key)
            rows.append((label, ts_ms, price))
    print()
    print(f"  collected {len(rows):,} rows ({skipped:,} skipped: no price)")
    return rows


def main() -> int:
    if MODE not in ("intraday_continuous", "day_ahead_15min", "both"):
        print(f"Unknown MODE={MODE!r}", file=sys.stderr)
        return 2
    plan: list[tuple[int, str, str]] = []
    if MODE in ("intraday_continuous", "both"):
        plan += SERIES["intraday_continuous"]
    if MODE in ("day_ahead_15min", "both"):
        plan += SERIES["day_ahead_15min"]

    seen: set[tuple[str, int]] = set()
    all_rows: list[tuple[str, int, float]] = []
    for filter_id, region, label in plan:
        all_rows.extend(collect(label, filter_id, region, seen))

    all_rows.sort(key=lambda r: (r[0], r[1]))

    print(f"\nWriting {len(all_rows):,} rows to {OUTPUT.name}")
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product", "timestamp_utc", "timestamp_local", "price_eur_per_mwh"])
        for label, ts_ms, price in all_rows:
            w.writerow([label, ms_to_iso_utc(ts_ms), ms_to_iso_berlin(ts_ms), price])
    print(f"Done -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
