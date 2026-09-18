"""Stage 2: daily price history of the YES token for every cleanly resolved market.

The CLOB keeps only daily points (fidelity=1440, interval=max) for closed markets.
Output: data/labs/longshot/history/<market_id>.json  ({"history":[{"t":..,"p":..},..]})

Usage: python3 fetch_history.py [--workers 8]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import CLOB, HIST, MARKETS_JSONL, client, get_json, loads_list, read_jsonl, resolved_yes

_lock = threading.Lock()
_done = 0
_tls = threading.local()


def _client():
    c = getattr(_tls, "c", None)
    if c is None:
        c = client()
        _tls.c = c
    return c


def fetch_one(m: dict) -> str:
    global _done
    mid = str(m["id"])
    path = HIST / f"{mid}.json"
    if path.exists():
        return "skip"
    toks = loads_list(m.get("clobTokenIds"))
    if not toks:
        return "no_token"
    data = get_json(_client(), f"{CLOB}/prices-history",
                    {"market": toks[0], "interval": "max", "fidelity": 1440})
    if not isinstance(data, dict) or "history" not in data:
        return "error"
    path.write_text(json.dumps(data))
    with _lock:
        _done += 1
        if _done % 500 == 0:
            print(f"  fetched {_done}", file=sys.stderr)
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--min-life-days", type=float, default=1.5)
    a = ap.parse_args()
    from build_obs import event_time
    from common import parse_ts
    markets = []
    short = 0
    for m in read_jsonl(MARKETS_JSONL):
        if resolved_yes(m) is None:
            continue
        t_event, _ = event_time(m)
        t_start = parse_ts(m.get("acceptingOrdersTimestamp")) or parse_ts(m.get("startDate")) or parse_ts(m.get("createdAt"))
        if t_event and t_start and (t_event - t_start) < a.min_life_days * 86400:
            short += 1  # daily history cannot give a pre-event point for these
            continue
        markets.append(m)
    print(f"{len(markets)} cleanly resolved markets to fetch ({short} skipped: life < {a.min_life_days}d)", file=sys.stderr)
    stats: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(fetch_one, m) for m in markets]
        for f in as_completed(futs):
            try:
                k = f.result()
            except Exception as e:  # noqa: BLE001
                k = f"exc:{type(e).__name__}"
            stats[k] = stats.get(k, 0) + 1
    print("done:", stats, file=sys.stderr)


if __name__ == "__main__":
    main()
