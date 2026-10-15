"""Lab 3, stage 4: settlement and fee metadata for every collected pair.

    python3 settle.py --db data/labs/xvenue/xvenue.db [--out data/labs/xvenue/meta.json]

Kalshi: GET /markets?tickers=... in URL-sized batches -> status, result (yes/no), close_time,
settlement_ts. Polymarket: GET gamma /markets/{id} per market -> closed, outcomes, outcomePrices,
clobTokenIds (to find which outcome our token is), feeSchedule / feesEnabled, umaResolutionStatus.
Answers are cached in the output file; markets already settled on both sides are not re-asked,
so re-runs only fetch what is still open. Nothing here needs keys.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets"
GAMMA = "https://gamma-api.polymarket.com/markets/"


def ticker_chunks(tickers: list[str], limit: int = 3800) -> list[list[str]]:
    out, cur, n = [], [], 0
    for t in tickers:
        if cur and n + len(t) + 1 > limit:
            out.append(cur); cur, n = [], 0
        cur.append(t); n += len(t) + 1
    if cur:
        out.append(cur)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    db = Path(a.db); out = Path(a.out) if a.out else db.parent / "meta.json"
    meta = json.loads(out.read_text()) if out.exists() else {"kalshi": {}, "poly": {}}
    c = sqlite3.connect(db)
    pairs = c.execute("select pair_id, kalshi_ticker, poly_id, poly_token from pairs").fetchall()
    s = httpx.Client(timeout=30)
    # Kalshi, batched; re-ask anything not finalized
    todo = sorted({k for _, k, _, _ in pairs if meta["kalshi"].get(k, {}).get("status") != "finalized"})
    for i, chunk in enumerate(ticker_chunks(todo)):
        try:
            r = s.get(KALSHI, params={"tickers": ",".join(chunk), "limit": 1000}); r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"kalshi chunk {i}: {e}", file=sys.stderr); continue
        for m in r.json().get("markets", []):
            meta["kalshi"][m["ticker"]] = {k: m.get(k) for k in ("status", "result", "close_time", "settlement_ts", "expiration_time", "title", "yes_sub_title")}
        time.sleep(0.2)
    print(f"kalshi: {len(todo)} asked, {sum(1 for v in meta['kalshi'].values() if v.get('status') == 'finalized')} finalized", file=sys.stderr)
    # Polymarket, one call per market; re-ask anything not closed
    todo = sorted({p for _, _, p, _ in pairs if not meta["poly"].get(p, {}).get("closed")})
    for i, pid in enumerate(todo):
        try:
            r = s.get(GAMMA + pid); r.raise_for_status(); m = r.json()
        except Exception as e:  # noqa: BLE001
            print(f"gamma {pid}: {e}", file=sys.stderr); time.sleep(1); continue
        def lst(v):
            return json.loads(v) if isinstance(v, str) else (v or [])
        meta["poly"][pid] = {"closed": m.get("closed"), "outcomes": lst(m.get("outcomes")), "outcomePrices": lst(m.get("outcomePrices")),
                             "clobTokenIds": lst(m.get("clobTokenIds")), "feesEnabled": m.get("feesEnabled"), "feeSchedule": m.get("feeSchedule"),
                             "feeType": m.get("feeType"), "umaResolutionStatus": m.get("umaResolutionStatus"), "endDate": m.get("endDate"), "question": m.get("question")}
        if i % 100 == 99:
            print(f"  gamma {i + 1}/{len(todo)}", file=sys.stderr); out.write_text(json.dumps(meta))
        time.sleep(0.05)
    out.write_text(json.dumps(meta))
    print(f"poly: {len(todo)} asked, {sum(1 for v in meta['poly'].values() if v.get('closed'))} closed; written {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
