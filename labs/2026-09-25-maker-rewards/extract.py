"""Stage 1: pull what Lab 2 needs out of the May 2026 production database into parquet.

    python3 extract.py [--db data/paper_bot_prod_2026-05-29.db]

Writes data/labs/maker/snapshots.parquet (one row per order-book snapshot with the
rewards parameters and best-level / total depth pulled out of the JSON payload) and
data/labs/maker/fills.parquet (taker fills). Streams the 6.5 GB database, so it runs in
constant memory; expect 10–20 minutes.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import orjson as json
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "labs" / "maker"
OUT.mkdir(parents=True, exist_ok=True)
SNAP_PQ = OUT / "snapshots.parquet"
FILLS_PQ = OUT / "fills.parquet"

SNAP_COLS = ["ts", "market_id", "condition_id", "slug", "category", "outcome", "question", "bid", "ask", "last",
             "best_bid_size", "best_ask_size", "total_bid_size", "total_ask_size",
             "reward_rate", "reward_min_size", "reward_max_spread", "tick", "neg_risk", "end_ts",
             "volume24h", "liquidity", "yes_token"]
SCHEMA = pa.schema([
    ("ts", pa.int64()), ("market_id", pa.string()), ("condition_id", pa.string()), ("slug", pa.string()),
    ("category", pa.string()), ("outcome", pa.string()), ("question", pa.string()),
    ("bid", pa.float64()), ("ask", pa.float64()), ("last", pa.float64()),
    ("best_bid_size", pa.float64()), ("best_ask_size", pa.float64()), ("total_bid_size", pa.float64()), ("total_ask_size", pa.float64()),
    ("reward_rate", pa.float64()), ("reward_min_size", pa.float64()), ("reward_max_spread", pa.float64()), ("tick", pa.float64()),
    ("neg_risk", pa.bool_()), ("end_ts", pa.int64()), ("volume24h", pa.float64()), ("liquidity", pa.float64()), ("yes_token", pa.string()),
])


def parse_ts(s) -> int | None:
    if not s:
        return None
    s = str(s).strip().replace(" ", "T", 1)
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def active_reward_rate(p: dict, ts: int) -> float:
    """Sum of rewardsDailyRate over clobRewards entries active at ts (dates are day-granular)."""
    total = 0.0
    for r in p.get("clobRewards") or []:
        rate = f(r.get("rewardsDailyRate")) or 0.0
        start, end = parse_ts(r.get("startDate")), parse_ts(r.get("endDate"))
        if start and ts < start:
            continue
        if end and ts > end + 86400:
            continue
        total += rate
    return total


def snapshot_row(r: tuple) -> dict | None:
    market_id, slug, category, outcome, bid, ask, last, payload, created_at = r
    ts = parse_ts(created_at)
    if ts is None:
        return None
    p = {}
    if payload:
        try:
            p = json.loads(payload)
        except (ValueError, TypeError):
            p = {}
    toks = p.get("clobTokenIds")
    if isinstance(toks, str):
        try:
            toks = json.loads(toks)
        except (ValueError, TypeError):
            toks = None
    return {
        "ts": ts, "market_id": str(market_id), "condition_id": p.get("conditionId"), "slug": slug,
        "category": category, "outcome": outcome, "question": p.get("question"),
        "bid": f(bid), "ask": f(ask), "last": f(last),
        "best_bid_size": f(p.get("best_bid_size")), "best_ask_size": f(p.get("best_ask_size")),
        "total_bid_size": f(p.get("total_bid_size")), "total_ask_size": f(p.get("total_ask_size")),
        "reward_rate": active_reward_rate(p, ts), "reward_min_size": f(p.get("rewardsMinSize")),
        "reward_max_spread": f(p.get("rewardsMaxSpread")), "tick": f(p.get("orderPriceMinTickSize")),
        "neg_risk": bool(p.get("negRisk")), "end_ts": parse_ts(p.get("endDate")),
        "volume24h": f(p.get("volume24hr")), "liquidity": f(p.get("liquidityClob") or p.get("liquidity")),
        "yes_token": (toks[0] if isinstance(toks, list) and toks else None),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "paper_bot_prod_2026-05-29.db"))
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)

    # --- snapshots, streamed in chunks straight into parquet row groups
    cur = con.execute("select market_id, slug, category, outcome, best_bid, best_ask, last_price, payload, created_at "
                      "from market_snapshots order by market_id, created_at")
    writer, n, bad = None, 0, 0
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        recs = []
        for r in rows:
            d = snapshot_row(r)
            if d is None:
                bad += 1
                continue
            recs.append(d)
        table = pa.Table.from_pylist(recs, schema=SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(SNAP_PQ, SCHEMA, compression="zstd")
        writer.write_table(table)
        n += len(recs)
        print(f"  snapshots {n}", file=sys.stderr)
    if writer:
        writer.close()
    print(f"snapshots: {n} rows ({bad} bad) -> {SNAP_PQ} {SNAP_PQ.stat().st_size/1e6:.0f} MB", file=sys.stderr)

    # --- fills
    fills = pd.read_sql_query(
        "select fill_ts as ts, condition_id, asset, outcome, side, price, size, notional, wallet, title from pm_fills", con)
    fills["ts"] = fills["ts"].astype("int64")
    fills.to_parquet(FILLS_PQ, index=False, compression="zstd")
    print(f"fills: {len(fills)} rows -> {FILLS_PQ} {FILLS_PQ.stat().st_size/1e6:.0f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
