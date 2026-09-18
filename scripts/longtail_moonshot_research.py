"""LONG-TAIL MOONSHOT RESEARCH — per maintainer's pivot 2026-05-10.

Hypothesis (maintainer):
  Polymarket markets systematically misprice long-tail outcomes.
  Buying YES at $0.05 with TRUE prob > 0.05 means asymmetric payoff:
    win:  +$0.95 / share  (= 19x ROI on $0.05 entry)
    loss: -$0.05 / share
  Even with 90% loss rate, EV per trade can be huge.

Method:
  1. Pull all `pm_fills` where price ≤ $0.10 (long-tail BUY entries).
  2. For each unique market_id, query gamma to learn:
       - is the market resolved?
       - which outcome won (Yes=outcomePrices[0]==1 vs No=outcomePrices[1]==1)?
  3. For each resolved long-tail entry:
       - if outcome they bought == winner → payout $1, PnL = $1 - their_price
       - if outcome they bought != winner → PnL = -their_price
  4. Aggregate by:
       - price bucket: [0.01, 0.025, 0.05, 0.075, 0.10]
       - outcome side bought (Yes/No)
       - market category if extractable
  5. Report: actual_WR vs implied_WR (= price), realized $ PnL, ROI

If actual_WR > implied_WR significantly → systematic mispricing = REAL EDGE.

Output: stdout + /app/data/longtail_moonshot.jsonl
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path('/app/data/paper_bot.db')
OUTPUT = Path('/app/data/longtail_moonshot.jsonl')

PRICE_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.075, 0.10]


def fetch_all_closed_markets(max_pages: int = 20) -> dict[str, dict]:
    """Pull all closed markets via pagination, build conditionId → market map.

    gamma API filter by conditionId doesn't work, so we paginate `closed=true`
    sorted by endDate desc to get most recent resolutions first.
    """
    cmap: dict[str, dict] = {}
    for page in range(max_pages):
        offset = page * 500
        url = (
            f"https://gamma-api.polymarket.com/markets?"
            f"closed=true&limit=500&offset={offset}&order=endDate&ascending=false"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as exc:
            print(f"  page {page} fetch err: {exc}")
            break
        if not data:
            break
        for m in data:
            cid = (m.get("conditionId") or "").lower()
            if cid:
                cmap[cid] = m
        if len(data) < 500:
            break  # last page
    return cmap


def fetch_market_resolution(condition_id: str) -> dict | None:
    """Legacy single-market fetch (kept for backward compat)."""
    return None  # use cmap from fetch_all_closed_markets instead


def is_resolved(market: dict) -> tuple[bool, str | None]:
    """Determine if market resolved + which outcome won.

    pm_fills.outcome holds team/player names (e.g., "HANJIN BRION", "Yes",
    "Toronto Blue Jays"). Match to gamma `outcomes` array by index of '1.0'
    in outcomePrices.
    """
    if not market.get("closed", False):
        return False, None
    prices_raw = market.get("outcomePrices", "")
    outcomes_raw = market.get("outcomes", "")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        if not prices or not outcomes:
            return False, None
        for i, p in enumerate(prices):
            if abs(float(p) - 1.0) < 0.01 and i < len(outcomes):
                return True, str(outcomes[i])
        return False, None
    except Exception:
        return False, None


def main() -> None:
    print("=" * 78)
    print(f"  LONG-TAIL MOONSHOT RESEARCH")
    print(f"  ts={datetime.now(timezone.utc).isoformat()}")
    print(f"  thesis: Polymarket misprices long-tail (price ≤ $0.10) outcomes")
    print("=" * 78)

    if not DB.exists():
        print("DB missing")
        return

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # 1. Pull pm_fills with long-tail prices
    rows = c.execute(
        """SELECT condition_id, side, outcome, price, size, notional, fill_ts, title
           FROM pm_fills
           WHERE price <= 0.10 AND price > 0.005 AND side = 'BUY'
           ORDER BY fill_ts DESC LIMIT 5000"""
    ).fetchall()
    print(f"\n  pm_fills with price ≤ $0.10 BUY: {len(rows)} (capped 5000)")

    # Group by condition_id (each market has multiple fills)
    by_market: dict[str, list] = defaultdict(list)
    for cid, side, outcome, price, size, notional, fill_ts, title in rows:
        by_market[cid].append({
            "side": side, "outcome": outcome, "price": price,
            "size": size, "notional": notional, "fill_ts": fill_ts,
            "title": title,
        })
    print(f"  Unique markets: {len(by_market)}")

    # 2. Bulk-fetch closed markets via pagination, build cid→winner map
    print(f"\n  Pulling all closed markets via gamma pagination...")
    closed_cmap = fetch_all_closed_markets(max_pages=20)
    print(f"  closed markets retrieved: {len(closed_cmap)}")

    resolved_map: dict[str, tuple[bool, str | None, str]] = {}
    for cid in by_market.keys():
        cid_lower = (cid or "").lower()
        market = closed_cmap.get(cid_lower)
        if market is None:
            resolved_map[cid] = (False, None, "")
            continue
        resolved, winner = is_resolved(market)
        slug = (market.get("slug") or "")[:60]
        resolved_map[cid] = (resolved, winner, slug)
    n_resolved = sum(1 for v in resolved_map.values() if v[0])
    print(f"  resolved found: {n_resolved} / {len(by_market)}")

    # 3. For each resolved market, compute realized PnL of each fill
    pnl_events = []
    for cid, fills in by_market.items():
        if cid not in resolved_map:
            continue
        resolved, winner, slug = resolved_map[cid]
        if not resolved or winner is None:
            continue
        for f in fills:
            their_outcome = f["outcome"]  # 'Yes' or 'No'
            # Did they win?
            won = (their_outcome == winner)
            entry = f["price"]
            payout = 1.0 if won else 0.0
            pnl_per_share = payout - entry
            shares = f["size"]
            pnl_dollar = pnl_per_share * shares
            roi_pct = pnl_per_share / entry * 100 if entry > 0 else 0
            pnl_events.append({
                "cid": cid,
                "slug": slug,
                "outcome_bought": their_outcome,
                "winner": winner,
                "won": won,
                "entry": entry,
                "size": shares,
                "notional": f["notional"],
                "pnl_per_share": pnl_per_share,
                "pnl_dollar": pnl_dollar,
                "roi_pct": roi_pct,
                "fill_ts": f["fill_ts"],
            })

    print(f"\n  resolved fills scored: {len(pnl_events)}")
    if not pnl_events:
        print("  ❌ no resolved fills to analyze. Markets may not be closed yet.")
        return

    # 4. Aggregate by price bucket
    print(f"\n{'─' * 78}")
    print(f"  AGGREGATE BY ENTRY PRICE BUCKET")
    print(f"{'─' * 78}")
    print(f"  {'bucket':<12} {'n':<6} {'WR':<7} {'implied_p':<10} {'realized_$ avg':<14} {'ROI%':<10} {'cum_$':<12}")
    print(f"  {'-' * 78}")

    bucket_stats = []
    for i in range(len(PRICE_BUCKETS) - 1):
        lo, hi = PRICE_BUCKETS[i], PRICE_BUCKETS[i + 1]
        sub = [e for e in pnl_events if lo <= e["entry"] < hi]
        if not sub:
            continue
        n = len(sub)
        wins = sum(1 for e in sub if e["won"])
        wr = wins / n
        implied_p_avg = sum(e["entry"] for e in sub) / n
        avg_pnl = sum(e["pnl_per_share"] for e in sub) / n
        cum_pnl = sum(e["pnl_dollar"] for e in sub)
        avg_roi = sum(e["roi_pct"] for e in sub) / n
        bucket_stats.append({
            "bucket_lo": lo, "bucket_hi": hi,
            "n": n, "wr": wr, "implied_p": implied_p_avg,
            "avg_realized_pnl_per_share": avg_pnl,
            "avg_roi_pct": avg_roi,
            "cum_pnl_dollar": cum_pnl,
        })
        edge_marker = " ✅" if wr > implied_p_avg * 1.2 else (" ⚠️" if wr < implied_p_avg * 0.5 else "")
        print(f"  ${lo:.3f}-${hi:.3f}  {n:<6} {wr*100:>5.1f}%  {implied_p_avg*100:>6.2f}%   "
              f"${avg_pnl:<+13.4f} {avg_roi:>+8.1f}%  ${cum_pnl:<+10.2f}{edge_marker}")

    # 5. Report best wins / losses
    print(f"\n{'─' * 78}")
    print(f"  BIGGEST WINS (top 10)")
    print(f"{'─' * 78}")
    for e in sorted(pnl_events, key=lambda x: -x["pnl_dollar"])[:10]:
        print(f"  ${e['pnl_dollar']:>+9.2f} on {e['outcome_bought']} @${e['entry']:.4f} ({e['roi_pct']:+.0f}%) | {e['slug'][:50]}")

    print(f"\n  BIGGEST LOSSES (top 5)")
    for e in sorted(pnl_events, key=lambda x: x["pnl_dollar"])[:5]:
        print(f"  ${e['pnl_dollar']:>+9.2f} on {e['outcome_bought']} @${e['entry']:.4f} ({e['roi_pct']:+.0f}%) | {e['slug'][:50]}")

    # 6. Aggregate verdict
    total_n = len(pnl_events)
    total_wins = sum(1 for e in pnl_events if e["won"])
    total_pnl = sum(e["pnl_dollar"] for e in pnl_events)
    total_notional = sum(e["notional"] for e in pnl_events)
    avg_implied = sum(e["entry"] for e in pnl_events) / total_n
    avg_realized_wr = total_wins / total_n
    print(f"\n{'═' * 78}")
    print(f"  TOTAL VERDICT")
    print(f"{'═' * 78}")
    print(f"  resolved fills:    {total_n}")
    print(f"  total wins:        {total_wins} ({avg_realized_wr*100:.1f}%)")
    print(f"  avg implied prob:  {avg_implied*100:.2f}%")
    print(f"  realized WR vs implied:  {avg_realized_wr*100:.1f}% vs {avg_implied*100:.2f}%")
    print(f"  ratio:             {avg_realized_wr / max(avg_implied, 0.001):.2f}x")
    print(f"  total notional:    ${total_notional:.0f}")
    print(f"  total realized PnL: ${total_pnl:+.2f}")
    print(f"  ROI on notional:   {total_pnl / max(total_notional, 1) * 100:+.2f}%")

    if avg_realized_wr > avg_implied * 1.5:
        print(f"\n  ✅ SIGNIFICANT MISPRICING detected: {avg_realized_wr/avg_implied:.2f}x implied")
        print(f"     This is the long-tail edge thesis confirmed at aggregate level.")
    elif avg_realized_wr > avg_implied * 1.1:
        print(f"\n  🟡 mild mispricing: {avg_realized_wr/avg_implied:.2f}x — worth deeper analysis")
    else:
        print(f"\n  ❌ no aggregate mispricing — markets ~fairly priced or underprice tail")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "moonshot_v1",
            "n_fills": total_n,
            "n_wins": total_wins,
            "realized_wr": round(avg_realized_wr, 4),
            "implied_wr": round(avg_implied, 4),
            "ratio": round(avg_realized_wr / max(avg_implied, 0.001), 3),
            "total_pnl": round(total_pnl, 2),
            "total_notional": round(total_notional, 2),
            "buckets": bucket_stats,
        }) + "\n")
    print(f"\n  saved to {OUTPUT}")
    conn.close()


if __name__ == "__main__":
    main()
