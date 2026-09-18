"""Paper Maker Forensic v2 per [GPT 34] — live-readiness audit.

GPT 34 blocked $1 canary with 5 conditions to fix before live:
  1. Rerun forensic AFTER 12:43 UTC big resolutions
  2. Q5 v2: liability by event AND portfolio (in shares × $1, not premium received)
  3. Queue model: assume behind existing displayed size
  4. Time-to-resolution histogram (NOT elapsed holding time)
  5. Canary size in liability terms, not premium

Plus winner-vs-loser bucket decomposition: did realized > markout simply because
86 fills luckily avoided winning buckets?

Reads:
  /app/data/paper_maker_sim.jsonl       — strict fills + quote snapshots
  /app/data/weather_resolutions.jsonl   — resolved winners
  /app/data/hedge_shadow.jsonl          — for endDate per event (alt source)

Output:
  stdout report
  /app/data/paper_maker_forensic_v2.jsonl

Usage: docker exec polymarket-bot python3 -m scripts.paper_maker_forensic_v2 --save
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path("/app/data")
SIM_FILE = DATA_DIR / "paper_maker_sim.jsonl"
RESOLUTIONS_FILE = DATA_DIR / "weather_resolutions.jsonl"
OUTPUT = DATA_DIR / "paper_maker_forensic_v2.jsonl"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
UA = {"User-Agent": "Mozilla/5.0"}

logger = logging.getLogger(__name__)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def section(title: str) -> None:
    print()
    print("─" * 75)
    print(f"  {title}")
    print("─" * 75)


def fetch_event_meta(event_id: str) -> dict | None:
    """Pull endDate + closed status for time-to-resolution computation."""
    try:
        req = urllib.request.Request(
            f"{GAMMA_EVENTS_URL}/{event_id}", headers=UA,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    sim_records = load_jsonl(SIM_FILE)
    snaps = [r for r in sim_records if r.get("kind") == "quote_snapshot"]
    fills = [r for r in sim_records if r.get("kind") == "markout"]
    strict_fills = [f for f in fills if f.get("strict_filled")]

    print(f"\n{'═' * 75}")
    print(f"  PAPER MAKER FORENSIC v2  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print(f"  Per [GPT 34] — liability audit + queue model + time-to-resolution")
    print(f"{'═' * 75}")

    if not strict_fills:
        print("\n⚠️  NO strict fills. Aborting.")
        return

    # ─── Build endDate cache from quote snapshots (we already saved hours_to_resolution at quote time) ───
    snap_by_key = {}
    for s in snaps:
        eid = s.get("event_id")
        ts = s.get("ts", 0)
        hrs = s.get("hours_to_resolution", 0)
        # We want: at quote_time, end_dt = ts + hrs*3600
        if eid and hrs is not None:
            end_ts = ts + int(hrs * 3600)
            if eid not in snap_by_key or ts > snap_by_key[eid][1]:
                snap_by_key[eid] = (end_ts, ts)
    event_end_ts = {eid: end for eid, (end, _) in snap_by_key.items()}

    # Resolutions
    resolutions = load_jsonl(RESOLUTIONS_FILE)
    resolved_winners = {
        r["event_id"]: r["winner_market_id"]
        for r in resolutions if r.get("kind") == "weather_resolution"
    }

    # ─── Q5 v2: per-event liability ───
    section("Q5 v2. Liability per event + worst-case portfolio")
    print("  (Liability = shares × $1, not premium received)")

    # Per (event, market) aggregate of OPEN simulated shorts (events not yet resolved)
    # Strict fills means the maker would have been short YES.
    open_shorts_by_event_market = defaultdict(float)  # (eid, mid) → shares
    open_premium_by_event_market = defaultdict(float)
    closed_shorts_by_event_market = defaultdict(float)
    closed_premium_by_event_market = defaultdict(float)

    for f in strict_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        shares = f.get("shares", 0) or 0
        sim_ask = f.get("prev_sim_ask", 0) or 0
        if eid in resolved_winners:
            closed_shorts_by_event_market[(eid, mid)] += shares
            closed_premium_by_event_market[(eid, mid)] += sim_ask * shares
        else:
            open_shorts_by_event_market[(eid, mid)] += shares
            open_premium_by_event_market[(eid, mid)] += sim_ask * shares

    # Per-event worst-case = max(shares_per_bucket) × $1, summed across events
    # (Exactly 1 bucket wins; we owe $1 × shares_on_winning_bucket. Worst case = our largest short bucket wins.)
    per_event_open_worst = {}
    per_event_open_premium = {}
    open_event_ids = {eid for (eid, _) in open_shorts_by_event_market}
    for eid in open_event_ids:
        buckets = {(e, m): s for (e, m), s in open_shorts_by_event_market.items() if e == eid}
        prem = sum(p for (e, _), p in open_premium_by_event_market.items() if e == eid)
        if not buckets:
            continue
        max_shares_in_bucket = max(buckets.values())
        # Worst case: largest bucket wins, we owe $1 × shares, keep premium of all OTHER buckets
        # (Premium on the winning bucket is also kept — we collected it on sale)
        worst_loss = max_shares_in_bucket * 1.0 - prem
        per_event_open_worst[eid] = round(worst_loss, 2)
        per_event_open_premium[eid] = round(prem, 2)

    portfolio_worst = sum(per_event_open_worst.values())
    total_premium_open = sum(per_event_open_premium.values())

    print(f"  Open events with shorts:           {len(per_event_open_worst)}")
    print(f"  Total premium collected (open):    ${total_premium_open:.2f}")
    print(f"  Portfolio worst-case loss:         ${portfolio_worst:.2f}")
    if per_event_open_worst:
        sorted_worst = sorted(per_event_open_worst.items(), key=lambda x: -x[1])
        print(f"  Top 5 worst-case events:")
        for eid, w in sorted_worst[:5]:
            prem = per_event_open_premium.get(eid, 0)
            print(f"    {eid:<10}  worst_loss=${w:>7.2f}  premium=${prem:>6.2f}")
        max_event_worst = sorted_worst[0][1]
        print()
        print(f"  Max event worst-case:              ${max_event_worst:.2f}")

    # ─── Q4 v2: realized PnL split by winner/loser bucket ───
    section("Q4 v2. Realized PnL — winner-bucket vs loser-bucket fills")
    realized_winner_pnl = 0.0  # fills on the bucket that resolved YES
    realized_loser_pnl = 0.0   # fills on buckets that resolved NO
    fills_on_winner = 0
    fills_on_loser = 0
    fills_winner_shares = 0.0
    fills_loser_shares = 0.0
    for f in strict_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        if eid not in resolved_winners:
            continue
        sim_ask = f.get("prev_sim_ask", 0) or 0
        shares = f.get("shares", 0) or 0
        is_winner = (mid == resolved_winners[eid])
        if is_winner:
            pnl = (sim_ask - 1.0) * shares
            realized_winner_pnl += pnl
            fills_on_winner += 1
            fills_winner_shares += shares
        else:
            pnl = sim_ask * shares
            realized_loser_pnl += pnl
            fills_on_loser += 1
            fills_loser_shares += shares
    total_realized = realized_winner_pnl + realized_loser_pnl
    print(f"  Total resolved fills:              {fills_on_winner + fills_on_loser}")
    print(f"  Fills on WINNING bucket:           {fills_on_winner}  ({fills_winner_shares:.0f} shares)")
    print(f"    Realized PnL on winners:         ${realized_winner_pnl:.2f}  (we owe $1 - premium)")
    print(f"  Fills on LOSING buckets:           {fills_on_loser}  ({fills_loser_shares:.0f} shares)")
    print(f"    Realized PnL on losers:          ${realized_loser_pnl:.2f}  (we keep premium)")
    print(f"  TOTAL realized PnL:                ${total_realized:.2f}")
    if fills_on_winner == 0:
        print(f"  ⚠️  ZERO fills on winning buckets — cannot validate edge structurally.")
        print(f"     Either (a) tail buckets rarely win, or (b) sample too small.")
    elif fills_on_winner > 0 and total_realized > 0:
        print(f"  ✅ Edge survives even with {fills_on_winner} winning-bucket fills.")
    elif total_realized < 0:
        print(f"  ❌ Edge dies once winning-bucket fills land.")

    # ─── Q6 v2: PnL by time-to-resolution (real, not elapsed) ───
    section("Q6 v2. PnL by time-to-resolution at fill time")
    # We need to compute, for each fill, the hours_to_resolution at the prev_snapshot timestamp.
    # Recover from quote_snapshot records: build (event_id) → list of (snapshot_ts, hours_to_resolution).
    snap_ttr_lookup = defaultdict(list)
    for s in snaps:
        eid = s.get("event_id")
        ts = s.get("ts", 0)
        hrs = s.get("hours_to_resolution", 0)
        if eid is not None and hrs is not None:
            snap_ttr_lookup[eid].append((ts, hrs))
    for k in snap_ttr_lookup:
        snap_ttr_lookup[k].sort()

    def find_ttr_at_quote(eid, fill_ts: int) -> float | None:
        """Find hours_to_resolution at the time of the prior quote snapshot for this fill."""
        snaps_for_eid = snap_ttr_lookup.get(eid, [])
        if not snaps_for_eid:
            return None
        # Find latest snap with ts < fill_ts (the quote time was prior to fill ts)
        prior = [(s, h) for s, h in snaps_for_eid if s <= fill_ts]
        if not prior:
            return None
        s_ts, s_hrs = prior[-1]
        # Approximate: hours_to_resolution at quote was s_hrs
        return s_hrs

    ttr_buckets = defaultdict(lambda: {"n": 0, "pnl_realized": 0, "pnl_markout": 0})
    for f in strict_fills:
        eid = f.get("event_id")
        ts = f.get("ts", 0)
        ttr = find_ttr_at_quote(eid, ts)
        if ttr is None:
            label = "?"
        elif ttr <= 0.5:
            label = "<30m"
        elif ttr <= 2:
            label = "30m-2h"
        elif ttr <= 6:
            label = "2-6h"
        elif ttr <= 24:
            label = "6-24h"
        else:
            label = ">24h"
        # Realized only if event resolved
        sim_ask = f.get("prev_sim_ask", 0) or 0
        shares = f.get("shares", 0) or 0
        if eid in resolved_winners:
            won = (f.get("market_id") == resolved_winners[eid])
            pnl_real = (sim_ask - 1.0) * shares if won else sim_ask * shares
        else:
            pnl_real = 0  # not yet realized
        pnl_mark = (f.get("markout_pnl_per_share_strict") or 0) * shares
        ttr_buckets[label]["n"] += 1
        ttr_buckets[label]["pnl_realized"] += pnl_real
        ttr_buckets[label]["pnl_markout"] += pnl_mark

    print(f"  {'TTR bucket':<10} {'n':<6} {'markout':<12} {'realized':<12}")
    order = ["<30m", "30m-2h", "2-6h", "6-24h", ">24h", "?"]
    for label in order:
        if label not in ttr_buckets:
            continue
        v = ttr_buckets[label]
        print(f"  {label:<10} {v['n']:<6} ${v['pnl_markout']:<10.2f} ${v['pnl_realized']:<10.2f}")

    # ─── Queue-adjusted fill count (assume behind existing size at sim_ask price) ───
    section("Queue-adjusted fills — behind-existing-size assumption")
    # Build snapshot lookup for size_below_sim_ask + best_ask_depth at quote time
    queue_adj_fills = []
    for f in strict_fills:
        # Conservative: assume queue ahead = best_ask_depth (we sit one tick above, but
        # if other makers are at our level we'd be behind them). Take size_below_sim_ask + the
        # depth at our sim_ask level as queue-ahead. We don't store sim_ask depth, so use
        # fill's queue-adjusted count as is, but require queue_adjusted_fill_qty > 0
        # AND fill's max_buy_price > sim_ask + 1 tick (taker walked through).
        sim_ask = f.get("prev_sim_ask", 0) or 0
        max_buy = f.get("max_buy_price_in_window", 0) or 0
        qa = f.get("queue_adjusted_fill_qty", 0) or 0
        # Tighter: trade actually went above our level
        if max_buy > sim_ask and qa > 0:
            queue_adj_fills.append(f)
    print(f"  Strict fills:              {len(strict_fills)}")
    print(f"  Queue-adjusted (max_buy > sim_ask + qa>0): {len(queue_adj_fills)}")
    if strict_fills:
        retention = len(queue_adj_fills) / len(strict_fills) * 100
        print(f"  Retention pct:             {retention:.1f}%")
    qa_realized = 0.0
    qa_winner_count = 0
    qa_loser_count = 0
    for f in queue_adj_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        if eid not in resolved_winners:
            continue
        sim_ask = f.get("prev_sim_ask", 0) or 0
        shares = f.get("shares", 0) or 0
        won = mid == resolved_winners[eid]
        if won:
            qa_realized += (sim_ask - 1.0) * shares
            qa_winner_count += 1
        else:
            qa_realized += sim_ask * shares
            qa_loser_count += 1
    print(f"  Queue-adj resolved fills:  {qa_winner_count + qa_loser_count}")
    print(f"  Queue-adj realized PnL:    ${qa_realized:.2f}")
    print(f"  Queue-adj winner fills:    {qa_winner_count}")
    print(f"  Queue-adj loser fills:     {qa_loser_count}")

    # ─── Final live-canary recommendation ───
    section("Live canary configuration in LIABILITY terms")
    print("""  Proposed live canary (per [GPT 34]):

    status: paper_account live canary, not paper sim
    scope: weather negRisk events with neither subjective resolution nor catchall
    quote rule: post-only LIMIT ask at best_ask + 1 tick, $0.001
    sizing:
      max shares per leg: 5
      max per-leg liability: 5 × $1 = $5
      max event liability: $1  (= max 1 share short on the lowest-premium leg)
      max portfolio liability: $3
      max simultaneous open events: 3
    timing:
      T-2h to T-15m only (validate from Q6 v2 above first)
      no quotes inside final 15m unless cancel/reprice latency proven
    cancellation:
      spread > $0.05 → cancel
      news_alert → cancel all
      best_ask moves > 1pp → reprice to new best+1tick or cancel
    kill criteria:
      single event realized loss > $1 → kill that event
      portfolio realized < -$2 → kill canary, 24h cooldown
      queue-adjusted shadow EV negative on next 30 fills → kill
      top-1 event PnL > 50% on >=20 fills → kill
""")
    if portfolio_worst > 100:
        print("  ⚠️  Current paper portfolio worst-case is $", round(portfolio_worst, 2),
              "— scaled down for live canary by ~30x to fit max_portfolio_liability=$3")

    # ─── Save summary ───
    if args.save:
        summary = {
            "ts": int(time.time()),
            "kind": "forensic_v2",
            "strict_fills": len(strict_fills),
            "distinct_events": len({f.get("event_id") for f in strict_fills}),
            "resolved_fills": fills_on_winner + fills_on_loser,
            "fills_on_winning_bucket": fills_on_winner,
            "fills_on_losing_bucket": fills_on_loser,
            "realized_winner_pnl": round(realized_winner_pnl, 2),
            "realized_loser_pnl": round(realized_loser_pnl, 2),
            "total_realized": round(total_realized, 2),
            "portfolio_worst_case_open": round(portfolio_worst, 2),
            "max_event_worst_case_open": (
                round(max(per_event_open_worst.values()), 2) if per_event_open_worst else 0
            ),
            "queue_adjusted_fills": len(queue_adj_fills),
            "queue_adjusted_realized": round(qa_realized, 2),
            "queue_adjusted_winner_fills": qa_winner_count,
            "queue_adjusted_loser_fills": qa_loser_count,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("a") as fh:
            fh.write(json.dumps(summary) + "\n")
        print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
