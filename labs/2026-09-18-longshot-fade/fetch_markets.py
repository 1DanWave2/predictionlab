"""Stage 1: collect closed Polymarket markets from Gamma.

Gamma caps `offset` at ~2000, so we walk the date range in windows on endDate and
split any window that overflows. Output: data/labs/longshot/markets.jsonl (raw market
objects, one per line, de-duplicated by id).

Usage: python3 fetch_markets.py [--start 2025-09-01] [--end 2026-09-17] [--min-volume 5000]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta

from common import GAMMA, MARKETS_JSONL, client, get_json

PAGE = 100
OFFSET_CAP = 2000  # last offset that Gamma accepts


def fetch_window(c, lo: date, hi: date, min_volume: float, seen: set[str], out) -> int:
    """Fetch closed markets with lo <= endDate < hi. Splits the window if it overflows."""
    rows = []
    offset = 0
    overflow = False
    while True:
        params = {
            "closed": "true", "limit": PAGE, "offset": offset,
            "end_date_min": lo.isoformat(), "end_date_max": hi.isoformat(),
            "volume_num_min": min_volume, "order": "id", "ascending": "true",
        }
        page = get_json(c, f"{GAMMA}/markets", params)
        if not isinstance(page, list):
            # offset too large or other validation error -> treat as overflow
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
        if span <= 1:
            print(f"  window {lo}..{hi} overflows even at 1 day; keeping first {len(rows)} rows", file=sys.stderr)
        else:
            mid = lo + timedelta(days=span // 2)
            n1 = fetch_window(c, lo, mid, min_volume, seen, out)
            n2 = fetch_window(c, mid, hi, min_volume, seen, out)
            return n1 + n2
    new = 0
    for m in rows:
        mid_ = str(m.get("id"))
        if mid_ in seen:
            continue
        seen.add(mid_)
        out.write(json.dumps(m, ensure_ascii=False) + "\n")
        new += 1
    print(f"  window {lo}..{hi}: {len(rows)} rows, {new} new", file=sys.stderr)
    return new


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-09-01")
    ap.add_argument("--end", default="2026-09-17")
    ap.add_argument("--min-volume", type=float, default=5000)
    ap.add_argument("--window-days", type=int, default=14)
    a = ap.parse_args()

    seen: set[str] = set()
    if MARKETS_JSONL.exists():
        with MARKETS_JSONL.open(encoding="utf-8") as f:
            for line in f:
                try:
                    seen.add(str(json.loads(line).get("id")))
                except Exception:
                    pass
        print(f"resuming: {len(seen)} markets already on disk", file=sys.stderr)

    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end)
    total = 0
    with client() as c, MARKETS_JSONL.open("a", encoding="utf-8") as out:
        lo = start
        while lo < end:
            hi = min(lo + timedelta(days=a.window_days), end)
            total += fetch_window(c, lo, hi, a.min_volume, seen, out)
            out.flush()
            lo = hi
    print(f"done: {total} new markets, {len(seen)} total in {MARKETS_JSONL}", file=sys.stderr)


if __name__ == "__main__":
    main()
