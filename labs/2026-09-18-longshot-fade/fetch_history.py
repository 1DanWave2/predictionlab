"""Stage 2: daily YES-token price history for cleanly resolved markets, into the parquet store.

The CLOB keeps only daily points (fidelity=1440, interval=max) for closed markets.
Candidates: resolved 0/1, lifetime >= --min-life-days, no history stored yet, and not
already marked no_history/error three times (the CLOB publishes daily history with a lag).
"""
from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from build_obs import event_time
from common import CLOB, client, get_json, loads_list, parse_ts, resolved_yes
from store import append_histories, load_histories, load_status, markets_records, update_status

_tls = threading.local()


def _client():
    c = getattr(_tls, "c", None)
    if c is None:
        c = client()
        _tls.c = c
    return c


def fetch_one(m: dict) -> tuple[str, str, list[tuple[str, int, float]]]:
    mid = str(m["id"])
    toks = loads_list(m.get("clobTokenIds"))
    if not toks:
        return mid, "no_token", []
    try:
        data = get_json(_client(), f"{CLOB}/prices-history",
                        {"market": toks[0], "interval": "max", "fidelity": 1440})
    except Exception:  # noqa: BLE001
        return mid, "error", []
    if not isinstance(data, dict) or "history" not in data:
        return mid, "error", []
    pts = [(mid, int(h["t"]), float(h["p"])) for h in data["history"] if h.get("p") is not None]
    return mid, ("ok" if pts else "no_history"), pts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--min-life-days", type=float, default=1.5)
    ap.add_argument("--max-markets", type=int, default=250_000)
    a, _ = ap.parse_known_args()

    have = set(load_histories()["market_id"].astype(str))
    st = load_status()
    skip = set(st[(st["status"] != "ok") & (st["attempts"] >= 3)]["market_id"].astype(str)) if len(st) else set()
    cands, short = [], 0
    for m in markets_records():
        mid = str(m["id"])
        if mid in have or mid in skip or resolved_yes(m) is None:
            continue
        t_event, _ = event_time(m)
        t_start = parse_ts(m.get("acceptingOrdersTimestamp")) or parse_ts(m.get("startDate")) or parse_ts(m.get("createdAt"))
        if t_event and t_start and (t_event - t_start) < a.min_life_days * 86400:
            short += 1
            continue
        cands.append(m)
    cands = cands[: a.max_markets]
    print(f"{len(cands)} markets to fetch ({short} skipped: life < {a.min_life_days}d; {len(have)} already stored)", file=sys.stderr)

    rows, statuses, stats = [], [], {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(fetch_one, m) for m in cands]
        for i, f in enumerate(as_completed(futs), 1):
            mid, status, pts = f.result()
            statuses.append((mid, status))
            rows.extend(pts)
            stats[status] = stats.get(status, 0) + 1
            if i % 5000 == 0:
                print(f"  {i}/{len(cands)}", file=sys.stderr)
    n = append_histories(rows)
    update_status(statuses)
    print(f"done: {stats}; appended {n} points", file=sys.stderr)


if __name__ == "__main__":
    main()
