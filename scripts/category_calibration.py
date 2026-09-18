"""Category Calibration Overlay per [GPT 26].

Compute per-bucket realized event_strategy edge to identify which market
buckets deserve our +6% entry signal vs which should be filtered out.

Buckets:
  category_keyword (musk/f1/politics/sports/crypto)
  entry_price_bucket (0.10-0.30 / 0.30-0.50 / 0.50-0.70 / 0.70-0.90)
  hours_to_resolution_bucket (<24 / 24-72 / 72-168 / 168+)

Output:
  Per-bucket: count, win_rate, avg_pnl%
  Filter recommendation:
    BUCKET_BLACKLIST = [...]  buckets with WR<40% AND n>=3
    BUCKET_FAVORED = [...]    buckets with WR>=70% AND n>=3
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.db import db_session
from app.models import PaperOrder, MarketSnapshot


def categorize(text: str) -> str:
    """Coarse categorization from order note (only signal we have)."""
    t = text.lower()
    if "musk" in t or "tweets" in t:
        return "musk_tweets"
    if "antonelli" in t or "f1" in t or "grand prix" in t:
        return "f1"
    if "politic" in t or "trump" in t or "iran" in t or "putin" in t:
        return "politics"
    if "btc" in t or "bitcoin" in t or "wti" in t or "oil" in t or "ethereum" in t:
        return "crypto_or_commodity"
    if "vs" in t or "lakers" in t or "thunder" in t:
        return "matchup"
    return "other"


def price_bucket(price: float) -> str:
    if price < 0.30:
        return "p_lt_30"
    if price < 0.50:
        return "p_30_50"
    if price < 0.70:
        return "p_50_70"
    return "p_70_90"


def main() -> None:
    print("=" * 76)
    print("Category Calibration Overlay (per [GPT 26])")
    print("=" * 76)

    with db_session() as s:
        orders = s.execute(select(PaperOrder).order_by(PaperOrder.id)).scalars().all()
        # Title lookup per market_id from latest MarketSnapshot
        snaps = s.execute(select(MarketSnapshot)).scalars().all()
        title_map: dict[str, str] = {}
        for sn in snaps:
            title_map[sn.market_id] = sn.question or sn.slug or ""

    # Build cycles
    open_buys = {}
    cycles = []
    for o in orders:
        if o.side.upper() == "BUY":
            open_buys[o.market_id] = o
        elif o.side.upper() == "SELL" and o.market_id in open_buys:
            buy = open_buys.pop(o.market_id)
            cycles.append((buy, o))

    print(f"\n[1] {len(cycles)} closed cycles")

    # Filter to event_strategy only
    event_cycles = [(b, s_) for b, s_ in cycles if b.strategy == "event_strategy"]
    print(f"[2] {len(event_cycles)} event_strategy cycles")

    # Aggregate per (category, price_bucket)
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for buy, sell in event_cycles:
        title = title_map.get(buy.market_id, "")
        cat = categorize(title + " " + buy.note + " " + (sell.note or ""))
        pb = price_bucket(buy.price)
        pnl_pct = (sell.price - buy.price) / buy.price * 100
        buckets[(cat, pb)].append(pnl_pct)
        buckets[("CATEGORY", cat)].append(pnl_pct)
        buckets[("PRICE", pb)].append(pnl_pct)

    print("\n[3] Per-bucket event_strategy realized edge:")
    print(f"  {'bucket1':<12} {'bucket2':<14} {'n':<4} {'wins':<5} {'WR%':<5} {'avg_pnl%':<10} {'med_pnl%':<10}")

    items = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    favored = []
    blacklist = []
    for (b1, b2), pnls in items:
        if len(pnls) < 2:
            continue
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        avg = statistics.mean(pnls)
        med = statistics.median(pnls)
        flag = ""
        if wr >= 70 and len(pnls) >= 3 and avg > 0:
            favored.append((b1, b2, wr, avg, len(pnls)))
            flag = " ⭐FAVORED"
        elif wr < 40 and len(pnls) >= 3:
            blacklist.append((b1, b2, wr, avg, len(pnls)))
            flag = " ❌BLACKLIST"
        print(f"  {b1:<12} {b2:<14} {len(pnls):<4} {wins:<5} {wr:<5.0f} {avg:+9.2f}  {med:+9.2f}{flag}")

    print(f"\n[4] FAVORED buckets ({len(favored)}):")
    for b1, b2, wr, avg, n in favored:
        print(f"  ⭐ {b1}={b2}  WR={wr:.0f}%  avg={avg:+.2f}%  n={n}")

    print(f"\n[5] BLACKLIST buckets ({len(blacklist)}):")
    for b1, b2, wr, avg, n in blacklist:
        print(f"  ❌ {b1}={b2}  WR={wr:.0f}%  avg={avg:+.2f}%  n={n}")

    # Save filter rules
    output = {
        "favored": [{"dim": b1, "value": b2, "wr": wr, "avg": avg, "n": n} for b1, b2, wr, avg, n in favored],
        "blacklist": [{"dim": b1, "value": b2, "wr": wr, "avg": avg, "n": n} for b1, b2, wr, avg, n in blacklist],
    }
    out_path = Path("/app/data/calibration_filter.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n[6] Saved: {out_path}")


if __name__ == "__main__":
    main()
