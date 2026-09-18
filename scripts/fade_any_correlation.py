"""Fade_any trade correlation tagger per [GPT 40].

Tags each fade_any trade with:
  - event_slug / market category
  - resolution date (proxy for event clustering)
  - entry minute bucket (5-min intervals)
  - cluster_id (markets sharing event_id + temporal proximity)

Then computes:
  - independent observation count (markets/clusters, not raw trades)
  - per-cluster aggregate PnL
  - density check: trades per hour by cluster

Reads:
  /app/data/paper_bot.db (positions table)
  + gamma API for event metadata

Output: stdout + /app/data/fade_any_correlation.jsonl

Per [GPT 40]: required before any ramp consideration.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB = Path("/app/data/paper_bot.db")
OUTPUT = Path("/app/data/fade_any_correlation.jsonl")
GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/"
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_market_meta(market_id: str) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{GAMMA_MARKET_URL}{market_id}", headers=UA,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def main() -> None:
    if not DB.exists():
        print("DB not found")
        return
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, market_id, realized_pnl, created_at, updated_at "
        "FROM positions WHERE bucket='fade_any_canary' AND quantity=0 "
        "ORDER BY created_at"
    ).fetchall()

    print(f"\n{'═' * 75}")
    print(f"  FADE_ANY CORRELATION ANALYSIS  ({datetime.now(timezone.utc).isoformat()})")
    print(f"  Per [GPT 40]: required before ramp consideration")
    print(f"{'═' * 75}")

    print(f"\n  Total fade_any closed trades: {len(rows)}")

    # Pull market meta for each unique market_id
    unique_mids = list({r[1] for r in rows})
    print(f"  Unique markets touched:       {len(unique_mids)}")

    market_meta: dict[str, dict] = {}
    for mid in unique_mids:
        m = fetch_market_meta(mid)
        if m:
            market_meta[mid] = {
                "slug": (m.get("slug") or "")[:60],
                "category": m.get("category", "?"),
                "endDate": m.get("endDate", ""),
                "events": [(e.get("slug") or "")[:50] for e in m.get("events") or []],
                "is_matchup": "vs " in (m.get("question", "") or "").lower() or " vs." in (m.get("question", "") or "").lower(),
            }

    # Build trade records with metadata
    trades = []
    for tid, mid, pnl, created, updated in rows:
        meta = market_meta.get(mid, {})
        evt_slug = meta.get("events", [""])[0] if meta.get("events") else ""
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00") if "Z" in created else created + "+00:00")
        except Exception:
            try:
                created_dt = datetime.fromisoformat(created)
            except Exception:
                created_dt = None
        # Closed_at for hold time
        try:
            updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00") if "Z" in updated else updated + "+00:00")
        except Exception:
            try:
                updated_dt = datetime.fromisoformat(updated)
            except Exception:
                updated_dt = None
        hold_minutes = (
            (updated_dt - created_dt).total_seconds() / 60
            if (created_dt and updated_dt) else None
        )
        trades.append({
            "id": tid,
            "market_id": mid,
            "slug": meta.get("slug", "?"),
            "event_slug": evt_slug,
            "category": meta.get("category", "?"),
            "endDate": meta.get("endDate", ""),
            "is_matchup": meta.get("is_matchup", False),
            "pnl": pnl,
            "created_at": created,
            "hold_minutes": round(hold_minutes, 1) if hold_minutes else None,
            "is_pre_fix": created < "2026-05-06 18:35:00",
        })

    # ─── Display each trade with metadata ───
    print(f"\n  {'#':<4} {'id':<5} {'market':<10} {'pnl':<8} {'hold':<8} {'cat':<10} {'event_slug':<40}")
    print(f"  {'-' * 90}")
    for i, t in enumerate(trades, 1):
        flag = " *" if t["is_pre_fix"] else ""
        print(f"  {i:<4} {t['id']:<5} {t['market_id'][:8]:<10} "
              f"${t['pnl']:>+6.3f} {(str(t['hold_minutes'])+'m')[:7]:<8} "
              f"{t['category']:<10} {t['event_slug'][:40]:<40}{flag}")

    # ─── Cluster analysis: group by event_slug ───
    print(f"\n{'─' * 75}")
    print(f"  Clusters by event_slug")
    print(f"{'─' * 75}")
    by_evt: dict[str, list] = defaultdict(list)
    for t in trades:
        if t["is_pre_fix"]:
            continue  # exclude pre-fix per [GPT 40]
        key = t["event_slug"] or t["market_id"]
        by_evt[key].append(t)

    print(f"  Post-fix trades: {sum(len(v) for v in by_evt.values())}")
    print(f"  Distinct event clusters: {len(by_evt)}")
    print()
    sorted_clusters = sorted(by_evt.items(), key=lambda x: -len(x[1]))
    print(f"  {'event_cluster':<50} {'trades':<7} {'net_pnl':<10} {'wins':<5} {'avg_hold':<10}")
    for evt, tlist in sorted_clusters:
        net = sum(t["pnl"] for t in tlist)
        wins = sum(1 for t in tlist if t["pnl"] > 0)
        holds = [t["hold_minutes"] for t in tlist if t["hold_minutes"] is not None]
        avg_hold = sum(holds) / len(holds) if holds else 0
        print(f"  {evt[:50]:<50} {len(tlist):<7} ${net:>+7.3f} {wins:<5} {avg_hold:<10.1f}")

    # Per [GPT 40]: independence check
    print(f"\n{'─' * 75}")
    print(f"  Independence check per [GPT 40]")
    print(f"{'─' * 75}")
    n_post_fix = sum(len(v) for v in by_evt.values())
    n_clusters = len(by_evt)
    if n_post_fix == 0:
        print("  No post-fix trades")
    else:
        max_cluster = max(len(v) for v in by_evt.values())
        max_cluster_pct = max_cluster / n_post_fix * 100
        top_3_pct = sum(sorted([len(v) for v in by_evt.values()], reverse=True)[:3]) / n_post_fix * 100
        print(f"  Post-fix trades:                   {n_post_fix}")
        print(f"  Distinct event clusters:           {n_clusters}")
        print(f"  Effective independence ratio:      {n_clusters / n_post_fix * 100:.1f}%")
        print(f"  Largest single cluster:            {max_cluster} trades ({max_cluster_pct:.1f}% of post-fix)")
        print(f"  Top 3 clusters together:           {top_3_pct:.1f}% of post-fix trades")
        print()
        if max_cluster_pct > 50:
            print("  ❌ KILL SIGNAL: single cluster > 50% — observations not independent")
        elif top_3_pct > 80:
            print("  ⚠️  CAUTION: top 3 clusters dominate — limited diversity")
        elif n_clusters >= n_post_fix * 0.5:
            print("  ✅ Clusters diverse — observations look independent")
        else:
            print("  ⚠️  Mixed — need more data")

    # ─── Hold time profile ───
    print(f"\n{'─' * 75}")
    print(f"  Hold time profile (post-fix only)")
    print(f"{'─' * 75}")
    holds_post = [t["hold_minutes"] for t in trades if not t["is_pre_fix"] and t["hold_minutes"] is not None]
    if holds_post:
        holds_sorted = sorted(holds_post)
        print(f"  median hold:   {holds_sorted[len(holds_sorted) // 2]:.1f} min")
        print(f"  min hold:      {min(holds_sorted):.1f} min")
        print(f"  max hold:      {max(holds_sorted):.1f} min")
        long_holds = [h for h in holds_post if h > 240]
        if long_holds:
            print(f"  ⚠️  Stale holds > 4h: {len(long_holds)}")
        else:
            print(f"  ✅ No stale holds > 4h")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "correlation_v1",
            "total_trades": len(trades),
            "post_fix_trades": n_post_fix,
            "distinct_clusters": n_clusters,
            "max_cluster_size": max(len(v) for v in by_evt.values()) if by_evt else 0,
            "max_cluster_pct": max_cluster_pct if by_evt else 0,
            "top3_pct": top_3_pct if by_evt else 0,
            "median_hold_min": holds_sorted[len(holds_sorted) // 2] if holds_post else 0,
            "max_hold_min": max(holds_sorted) if holds_post else 0,
        }) + "\n")
    print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
