"""Stage 1: collect closed Polymarket markets from Gamma into the parquet store.

Two modes:
  backfill:    --start 2025-09-01 --end 2026-09-17   (walk endDate windows)
  incremental: --since-days 7                        (nightly)

Gamma caps `offset` at ~2000, so windows that overflow are split, first by date down
to one day, then by volume band. Incremental mode additionally (a) pages the most
recently *closed* markets by closedTime, which catches markets resolved early whose
endDate is far away, and (b) re-fetches stored markets that are closed but not yet
resolved, so their outcome fills in once UMA settles.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

from common import GAMMA, client, get_json, parse_ts, resolved_yes
from store import load_markets, market_row, markets_records, upsert_markets

PAGE = 100
OFFSET_CAP = 2000


def _params(lo: date, hi: date, vol_lo: float, vol_hi: float | None, offset: int) -> dict:
    p = {"closed": "true", "limit": PAGE, "offset": offset, "order": "id", "ascending": "true",
         "end_date_min": lo.isoformat(), "end_date_max": hi.isoformat(), "volume_num_min": vol_lo}
    if vol_hi is not None:
        p["volume_num_max"] = vol_hi
    return p


def fetch_window(c, lo: date, hi: date, vol_lo: float, vol_hi: float | None, sink: dict) -> None:
    """Collect markets with lo <= endDate < hi and vol_lo <= volume < vol_hi into sink (by id)."""
    rows, offset, overflow = [], 0, False
    while True:
        page = get_json(c, f"{GAMMA}/markets", _params(lo, hi, vol_lo, vol_hi, offset))
        if not isinstance(page, list):
            overflow = True
            break
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
        if offset > OFFSET_CAP:
            overflow = True
            break
        time.sleep(0.05)
    if overflow:
        span = (hi - lo).days
        if span > 1:
            mid = lo + timedelta(days=span // 2)
            fetch_window(c, lo, mid, vol_lo, vol_hi, sink)
            fetch_window(c, mid, hi, vol_lo, vol_hi, sink)
            return
        # one-day window still overflows: split by volume band
        if vol_hi is None:
            cut = vol_lo * 4
        else:
            cut = math.sqrt(vol_lo * vol_hi)
            if vol_hi / vol_lo < 1.05:
                print(f"  {lo} vol[{vol_lo:.0f},{vol_hi:.0f}) still overflows; keeping {len(rows)}", file=sys.stderr)
                for m in rows:
                    sink[str(m["id"])] = m
                return
        fetch_window(c, lo, hi, vol_lo, cut, sink)
        fetch_window(c, lo, hi, cut, vol_hi, sink)
        return
    for m in rows:
        sink[str(m["id"])] = m
    print(f"  {lo}..{hi} vol>={vol_lo:.0f}{'' if vol_hi is None else f'<{vol_hi:.0f}'}: {len(rows)}", file=sys.stderr)


def fetch_recently_closed(c, since_ts: int, min_volume: float, sink: dict) -> int:
    """Page closed markets by closedTime desc until older than since_ts (bounded by the offset cap)."""
    n, offset = 0, 0
    while offset <= OFFSET_CAP:
        page = get_json(c, f"{GAMMA}/markets", {"closed": "true", "order": "closedTime", "ascending": "false",
                                                "limit": PAGE, "offset": offset, "volume_num_min": min_volume})
        if not isinstance(page, list) or not page:
            break
        stop = False
        for m in page:
            ct = parse_ts(m.get("closedTime"))
            if ct and ct < since_ts:
                stop = True
                break
            sink[str(m["id"])] = m
            n += 1
        if stop or len(page) < PAGE:
            break
        offset += PAGE
        time.sleep(0.05)
    return n


def refetch_pending(c, max_age_days: int, limit: int, sink: dict) -> int:
    """Re-fetch stored markets that are closed but not cleanly resolved yet."""
    df = load_markets()
    if not len(df):
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    cand = [m for m in markets_records(df) if m.get("closed") and resolved_yes(m) is None
            and (m.get("endDate") or "") >= cutoff]
    cand = cand[:limit]
    n = 0
    for m in cand:
        d = get_json(c, f"{GAMMA}/markets/{m['id']}")
        if isinstance(d, dict) and d.get("id"):
            sink[str(d["id"])] = d
            n += 1
        time.sleep(0.03)
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--since-days", type=int)
    ap.add_argument("--min-volume", type=float, default=5000)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--pending-limit", type=int, default=3000)
    a, _ = ap.parse_known_args()

    today = datetime.now(timezone.utc).date()
    if a.since_days:
        start, end = today - timedelta(days=a.since_days), today + timedelta(days=2)
    else:
        start = date.fromisoformat(a.start or "2025-09-01")
        end = date.fromisoformat(a.end or today.isoformat())

    sink: dict[str, dict] = {}
    with client() as c:
        lo = start
        while lo < end:
            hi = min(lo + timedelta(days=a.window_days), end)
            fetch_window(c, lo, hi, a.min_volume, None, sink)
            lo = hi
        if a.since_days:
            since_ts = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
            n_rc = fetch_recently_closed(c, since_ts, a.min_volume, sink)
            n_pd = refetch_pending(c, 60, a.pending_limit, sink)
            print(f"recently closed: {n_rc}, pending re-fetched: {n_pd}", file=sys.stderr)
    now = int(time.time())
    n_new, n_upd = upsert_markets([market_row(m, now) for m in sink.values()])
    print(f"fetched {len(sink)} markets: {n_new} new, {n_upd} updated; store={len(load_markets())}", file=sys.stderr)


if __name__ == "__main__":
    main()
