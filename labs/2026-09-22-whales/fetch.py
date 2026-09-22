"""Lab 4 (whales), stage 1: snapshot the weekly Polymarket leaderboard and the top wallets' activity.

    python3 fetch.py [--top 25] [--days 7]      # -> data/labs/whales/<YYYY-MM-DD>/*.parquet, leaderboard_history.parquet

Public Data API, no keys:
  /v1/leaderboard?timePeriod=WEEK|MONTH&orderBy=PNL|VOL  (50 rows per page, offset)
  /activity?user=<wallet>       every fill, redeem and rebate with usdcSize (500 per page, offset)
  /closed-positions?user=       resolved or fully exited positions: avgPrice, totalBought, realizedPnl, timestamp
  /positions?user=              open positions: size, avgPrice, curPrice, cashPnl (100 per page)
Wallets are pseudonymous proxy addresses; userName is what the trader chose to show publicly.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = Path(os.environ.get("WHALES_DATA_DIR") or ROOT / "data" / "labs" / "whales")
D = "https://data-api.polymarket.com"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def get(c: httpx.Client, path: str, params: dict) -> list:
    for attempt in range(4):
        try:
            r = c.get(D + path, params=params)
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, list) else []
            if r.status_code not in (429, 500, 502, 503, 504):
                log(f"{path} {params} -> {r.status_code} {r.text[:100]}")
                return []
        except httpx.HTTPError as e:
            log(f"{path} -> {e}")
        time.sleep(1.5 * (attempt + 1))
    return []


def leaderboard(c: httpx.Client, period: str, order: str, n: int) -> list[dict]:
    rows = []
    for off in range(0, n, 50):
        page = get(c, "/v1/leaderboard", {"timePeriod": period, "orderBy": order, "limit": 50, "offset": off})
        rows += page
        if len(page) < 50:
            break
        time.sleep(0.1)
    return [{"period": period, "order": order, "rank": int(r["rank"]), "wallet": r["proxyWallet"], "name": r.get("userName") or "",
             "x": r.get("xUsername") or "", "verified": bool(r.get("verifiedBadge")), "vol": float(r.get("vol") or 0), "pnl": float(r.get("pnl") or 0)} for r in rows]


def activity(c: httpx.Client, wallet: str, since: int) -> list[dict]:
    rows = []
    for off in range(0, 5000, 500):
        page = get(c, "/activity", {"user": wallet, "limit": 500, "offset": off})
        rows += page
        if len(page) < 500 or (page and min(int(a.get("timestamp") or 0) for a in page) < since):
            break
        time.sleep(0.1)
    return [{"wallet": wallet, "ts": int(a.get("timestamp") or 0), "type": a.get("type"), "condition_id": a.get("conditionId"), "asset": a.get("asset"),
             "side": a.get("side"), "outcome": a.get("outcome"), "size": float(a.get("size") or 0), "usdc": float(a.get("usdcSize") or 0),
             "price": float(a.get("price") or 0), "title": a.get("title"), "slug": a.get("slug"), "event_slug": a.get("eventSlug"), "tx": a.get("transactionHash")}
            for a in rows if int(a.get("timestamp") or 0) >= since]


def closed(c: httpx.Client, wallet: str, since: int = 0) -> list[dict]:
    """50 per page regardless of `limit`; newest first, so stop once a page is older than `since`."""
    rows = []
    for off in range(0, 2000, 50):
        page = get(c, "/closed-positions", {"user": wallet, "limit": 50, "offset": off})
        rows += page
        if len(page) < 50 or (since and page and min(int(x.get("timestamp") or 0) for x in page) < since):
            break
        time.sleep(0.1)
    return [{"wallet": wallet, "ts": int(p.get("timestamp") or 0), "condition_id": p.get("conditionId"), "asset": p.get("asset"), "outcome": p.get("outcome"),
             "avg_price": float(p.get("avgPrice") or 0), "total_bought": float(p.get("totalBought") or 0), "realized_pnl": float(p.get("realizedPnl") or 0),
             "cur_price": float(p.get("curPrice") or 0), "title": p.get("title"), "slug": p.get("slug"), "event_slug": p.get("eventSlug"), "end_date": p.get("endDate")} for p in rows]


def positions(c: httpx.Client, wallet: str) -> list[dict]:
    rows = []
    for off in range(0, 1000, 100):
        page = get(c, "/positions", {"user": wallet, "limit": 100, "offset": off})
        rows += page
        if len(page) < 100:
            break
    return [{"wallet": wallet, "condition_id": p.get("conditionId"), "asset": p.get("asset"), "outcome": p.get("outcome"), "size": float(p.get("size") or 0),
             "avg_price": float(p.get("avgPrice") or 0), "cur_price": float(p.get("curPrice") or 0), "initial_value": float(p.get("initialValue") or 0),
             "current_value": float(p.get("currentValue") or 0), "cash_pnl": float(p.get("cashPnl") or 0), "realized_pnl": float(p.get("realizedPnl") or 0),
             "title": p.get("title"), "slug": p.get("slug"), "event_slug": p.get("eventSlug"), "end_date": p.get("endDate"), "redeemable": bool(p.get("redeemable"))} for p in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="wallets (by weekly PnL) to pull in full")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = OUT / today
    day_dir.mkdir(parents=True, exist_ok=True)
    since = int(time.time()) - a.days * 86400
    with httpx.Client(timeout=60, headers={"User-Agent": "predictionlab-labs/0.1"}) as c:
        lb = leaderboard(c, "WEEK", "PNL", 100) + leaderboard(c, "WEEK", "VOL", 50) + leaderboard(c, "MONTH", "PNL", 50) + leaderboard(c, "MONTH", "VOL", 50)
        lbdf = pd.DataFrame(lb)
        lbdf["snapshot"] = today
        lbdf.to_parquet(day_dir / "leaderboard.parquet", index=False)
        hist = OUT / "leaderboard_history.parquet"
        h = pd.concat([pd.read_parquet(hist), lbdf]) if hist.exists() else lbdf
        h = h.drop_duplicates(["snapshot", "period", "order", "wallet"], keep="last")
        h.to_parquet(hist, index=False)
        log(f"leaderboard: {len(lbdf)} rows; history {h['snapshot'].nunique()} snapshots")
        top = lbdf[(lbdf["period"] == "WEEK") & (lbdf["order"] == "PNL")].sort_values("rank").head(a.top)
        acts, cls, pos = [], [], []
        for _, r in top.iterrows():
            w = r["wallet"]
            acts += activity(c, w, since)
            cls += closed(c, w, since - 21 * 86400)     # three extra weeks for hit-rate context
            pos += positions(c, w)
            log(f"  #{r['rank']:>2} {r['name'][:22]:22s} pnl ${r['pnl']:>12,.0f}  activity {sum(1 for x in acts if x['wallet'] == w):4d}  closed {sum(1 for x in cls if x['wallet'] == w):3d}  open {sum(1 for x in pos if x['wallet'] == w):3d}")
            time.sleep(0.2)
        pd.DataFrame(acts).to_parquet(day_dir / "activity.parquet", index=False)
        pd.DataFrame(cls).to_parquet(day_dir / "closed.parquet", index=False)
        pd.DataFrame(pos).to_parquet(day_dir / "positions.parquet", index=False)
    log(f"saved {day_dir}")


if __name__ == "__main__":
    main()
