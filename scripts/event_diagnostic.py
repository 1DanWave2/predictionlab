"""Event strategy diagnostic per [GPT 25] / Path 2.

Segment closed cycles по market category, mid range, hours_to_resolution.
Find what subset event_strategy реально wins on.
"""
from __future__ import annotations

import re
import statistics
import sys
from collections import defaultdict
from datetime import UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.db import db_session
from app.models import PaperOrder


def categorize(title: str, slug: str) -> str:
    text = f"{title} {slug}".lower()
    if "musk" in text or "tweets" in text:
        return "musk_tweets"
    if "f1" in text or "grand prix" in text or "antonelli" in text:
        return "f1"
    if "nba" in text or "lakers" in text or "thunder" in text or "knicks" in text:
        return "nba"
    if "nhl" in text or "hurricanes" in text or "avalanche" in text:
        return "nhl"
    if "mlb" in text or "yankees" in text or "phillies" in text or "marlins" in text or "giants" in text or "rays" in text:
        return "mlb"
    if "atp" in text or "wta" in text or "tennis" in text or "abidjan" in text or "shymkent" in text:
        return "tennis"
    if "ufc" in text:
        return "ufc"
    if "cricket" in text or "ipl" in text:
        return "cricket"
    if "epl" in text or "manchester" in text or "liverpool" in text or "real madrid" in text or "premier league" in text:
        return "epl"
    if "lol" in text or "dota" in text or "valorant" in text or "counter-strike" in text:
        return "esports"
    if "election" in text or "trump" in text or "putin" in text or "iran" in text:
        return "politics"
    if "btc" in text or "bitcoin" in text or "eth" in text or "wti" in text:
        return "crypto_or_commodity"
    if "vs" in text:
        return "matchup_other"
    return "event_misc"


def main() -> None:
    print("=" * 76)
    print("Event Strategy Diagnostic")
    print("=" * 76)

    with db_session() as s:
        orders = s.execute(select(PaperOrder).order_by(PaperOrder.id)).scalars().all()

    open_buys = {}
    cycles = []
    for o in orders:
        if o.side.upper() == "BUY":
            open_buys[o.market_id] = o
        elif o.side.upper() == "SELL" and o.market_id in open_buys:
            buy = open_buys.pop(o.market_id)
            cycles.append((buy, o))

    print(f"\nFound {len(cycles)} closed cycles\n")

    by_strategy_cat = defaultdict(list)
    for buy, sell in cycles:
        # Extract from note: title heuristic via market_id (no title here, use note text)
        title = buy.note + " " + (sell.note or "")
        cat = categorize(title, "")
        pnl_pct = (sell.price - buy.price) / buy.price * 100
        key = (buy.strategy, cat)
        by_strategy_cat[key].append(pnl_pct)

    print("=== Per (strategy, category) ===")
    print(f"{'strategy':<22} {'category':<22} {'n':<4} {'wins':<5} {'losses':<7} {'WR%':<5} {'avg_pnl%':<10} {'med_pnl%':<10}")
    items = sorted(by_strategy_cat.items(), key=lambda kv: -len(kv[1]))
    overall_wins, overall_losses = 0, 0
    overall_wr_pnl_avg = []
    for (strat, cat), pnls in items:
        wins = sum(1 for p in pnls if p > 0)
        losses = len(pnls) - wins
        wr = wins / len(pnls) * 100
        avg_pnl = statistics.mean(pnls)
        med_pnl = statistics.median(pnls)
        print(f"{strat[:20]:<22} {cat[:20]:<22} {len(pnls):<4} {wins:<5} {losses:<7} {wr:<5.0f} {avg_pnl:+9.2f}  {med_pnl:+9.2f}")
        overall_wins += wins
        overall_losses += losses
        overall_wr_pnl_avg.extend(pnls)

    print()
    if overall_wr_pnl_avg:
        overall_wr = overall_wins / (overall_wins + overall_losses) * 100
        avg_all = statistics.mean(overall_wr_pnl_avg)
        med_all = statistics.median(overall_wr_pnl_avg)
        print(f"OVERALL: {overall_wins}W/{overall_losses}L  WR={overall_wr:.0f}%  avg={avg_all:+.2f}% median={med_all:+.2f}%")

    print("\n=== Profitable categories (WR ≥ 60%, n ≥ 2) ===")
    profitable = [(k, p) for k, p in items if len(p) >= 2 and (sum(1 for x in p if x > 0) / len(p)) >= 0.6]
    for (strat, cat), pnls in profitable:
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        avg = statistics.mean(pnls)
        print(f"  ✓ {strat[:18]:<20} {cat[:18]:<20} n={len(pnls)} WR={wr:.0f}% avg={avg:+.2f}%")

    print("\n=== Losing categories (WR < 40%, n ≥ 2) ===")
    losing = [(k, p) for k, p in items if len(p) >= 2 and (sum(1 for x in p if x > 0) / len(p)) < 0.4]
    for (strat, cat), pnls in losing:
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(pnls) * 100
        avg = statistics.mean(pnls)
        print(f"  ✗ {strat[:18]:<20} {cat[:18]:<20} n={len(pnls)} WR={wr:.0f}% avg={avg:+.2f}%")


if __name__ == "__main__":
    main()
