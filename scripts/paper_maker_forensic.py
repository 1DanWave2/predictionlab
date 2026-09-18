"""Paper Maker Forensic Report per [GPT 33] — answers 8 specific questions.

Required output:
  1. 117 strict fills across how many distinct events?
  2. Top 1 event share of PnL?
  3. Top 3 events share of PnL?
  4. Realized PnL (after resolution) vs markout PnL (15-30min snapshot)?
  5. Worst simulated inventory drawdown if all fills were live?
  6. PnL by time-to-resolution bucket?
  7. PnL by ask price bucket?
  8. Queue priority assumption — explicit statement.
  9. GO/NO-GO for $1 canary with kill criteria.

Reads: /app/data/paper_maker_sim.jsonl
Output: stdout report + /app/data/paper_maker_forensic.jsonl (rolling history)

Usage: docker exec polymarket-bot python3 -m scripts.paper_maker_forensic
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
import time

DATA_DIR = Path("/app/data")
SIM_FILE = DATA_DIR / "paper_maker_sim.jsonl"
RESOLUTIONS_FILE = DATA_DIR / "weather_resolutions.jsonl"
OUTPUT = DATA_DIR / "paper_maker_forensic.jsonl"

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


def hist_buckets(values, bins, format_label=lambda lo, hi: f"{lo}-{hi}"):
    out = []
    for lo, hi, label in bins:
        n = sum(1 for v in values if lo <= v < hi)
        out.append((label, n))
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true", help="append summary to forensic.jsonl")
    args = parser.parse_args()

    sim_records = load_jsonl(SIM_FILE)
    snaps = [r for r in sim_records if r.get("kind") == "quote_snapshot"]
    fills = [r for r in sim_records if r.get("kind") == "markout"]

    print(f"\n{'═' * 75}")
    print(f"  PAPER MAKER FORENSIC REPORT  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print(f"  Per [GPT 33] — answers 8 specific questions before any live canary")
    print(f"{'═' * 75}")

    print(f"\nRaw counts:")
    print(f"  quote snapshots:  {len(snaps)}")
    print(f"  fills evaluated:  {len(fills)}")

    # Strict fills only — what GPT cares about
    strict_fills = [f for f in fills if f.get("strict_filled")]
    proxy_fills = [f for f in fills if f.get("crossed_quote_proxy") and not f.get("strict_filled")]

    if not strict_fills:
        print("\n⚠️  NO strict fills yet. Cannot answer GPT questions. Re-run after more cron cycles.")
        return

    # Q1 — events
    section("Q1. Strict fills across how many distinct events?")
    by_event = defaultdict(list)
    for f in strict_fills:
        by_event[f.get("event_id", "?")].append(f)
    print(f"  Total strict fills:     {len(strict_fills)}")
    print(f"  Distinct events:        {len(by_event)}")
    print(f"  Avg fills per event:    {len(strict_fills) / max(1, len(by_event)):.1f}")

    # Q2-Q3 — concentration
    section("Q2/Q3. Top-1 / Top-3 event share of PnL")
    event_pnl = {
        eid: sum(
            (f.get("markout_pnl_per_share_strict") or 0) * (f.get("shares") or 0)
            for f in fs
        )
        for eid, fs in by_event.items()
    }
    total_pnl = sum(event_pnl.values())
    sorted_events = sorted(event_pnl.items(), key=lambda x: -x[1])
    print(f"  Total strict paper PnL:    ${total_pnl:.2f}")
    if sorted_events:
        top1 = sorted_events[0]
        top3 = sum(p for _, p in sorted_events[:3])
        top1_pct = top1[1] / total_pnl * 100 if total_pnl else 0
        top3_pct = top3 / total_pnl * 100 if total_pnl else 0
        print(f"  Top-1 event:               {top1[0]}  ${top1[1]:.2f}  ({top1_pct:.1f}%)")
        print(f"  Top-3 events combined:     ${top3:.2f}  ({top3_pct:.1f}%)")
        print()
        print(f"  All events sorted by PnL:")
        print(f"    {'event_id':<10} {'fills':<6} {'pnl':<10} {'pct':<6}")
        for eid, p in sorted_events[:15]:
            n = len(by_event[eid])
            pct = p / total_pnl * 100 if total_pnl else 0
            print(f"    {str(eid)[:10]:<10} {n:<6} ${p:<8.2f} {pct:<5.1f}%")
        # GPT kill: top 1 > 50%
        flag1 = top1_pct > 50
        flag3 = top3_pct > 80
        print()
        print(f"  [GPT 33] kill: top-1 > 50%       → {'❌ FAIL' if flag1 else '✅ pass'}")
        print(f"  [GPT 33] kill: top-3 > 80%       → {'❌ FAIL' if flag3 else '✅ pass'}")

    # Q4 — realized vs markout
    section("Q4. Realized (after resolution) vs Markout (next-snapshot) PnL")
    resolutions = load_jsonl(RESOLUTIONS_FILE)
    resolved_winners = {
        r["event_id"]: r["winner_market_id"]
        for r in resolutions if r.get("kind") == "weather_resolution"
    }
    realized_pnl = 0.0
    realized_count = 0
    for f in strict_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        sim_ask = f.get("prev_sim_ask", 0)
        shares = f.get("shares", 0)
        if eid not in resolved_winners:
            continue
        # Maker is short YES at sim_ask. If the bucket WON → owe $1 per share, lose (1 - sim_ask).
        # If the bucket LOST → keep sim_ask premium, $0 obligation.
        won = (mid == resolved_winners[eid])
        if won:
            pnl = (sim_ask - 1.0) * shares  # likely big negative
        else:
            pnl = sim_ask * shares  # we collected the premium, kept $sim_ask
        realized_pnl += pnl
        realized_count += 1
    if realized_count > 0:
        markout_pnl_for_resolved = sum(
            (f.get("markout_pnl_per_share_strict") or 0) * (f.get("shares") or 0)
            for f in strict_fills
            if f.get("event_id") in resolved_winners
        )
        print(f"  Strict fills with resolution:    {realized_count}")
        print(f"  Realized PnL (post-resolution):  ${realized_pnl:.2f}")
        print(f"  Markout PnL (15-30m snapshot):   ${markout_pnl_for_resolved:.2f}")
        print(f"  Markout overstated by:           ${markout_pnl_for_resolved - realized_pnl:.2f}")
        if realized_pnl < 0 and markout_pnl_for_resolved > 0:
            print(f"  ⚠️  FLAG: markout positive but realized NEGATIVE. Adverse selection real.")
    else:
        print(f"  No strict fills yet on resolved events. Wait for more resolutions.")

    # Q5 — worst inventory drawdown
    section("Q5. Worst simulated inventory drawdown (live exposure)")
    # Inventory = sum of shares we'd be short across all open events
    # Worst case: all events resolve YES on our short side simultaneously
    shorts_open = defaultdict(float)
    shorts_value = defaultdict(float)
    for f in strict_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        if eid in resolved_winners:
            continue  # already resolved, not open
        shares = f.get("shares", 0)
        sim_ask = f.get("prev_sim_ask", 0)
        shorts_open[(eid, mid)] += shares
        shorts_value[(eid, mid)] += sim_ask * shares
    total_shares = sum(shorts_open.values())
    total_value_collected = sum(shorts_value.values())
    worst_case_obligation = total_shares * 1.0  # if every short bucket wins
    worst_drawdown = total_value_collected - worst_case_obligation
    print(f"  Open simulated shorts:           {len(shorts_open)} positions")
    print(f"  Total open share count:          {total_shares:.0f}")
    print(f"  Premium collected on shorts:     ${total_value_collected:.2f}")
    print(f"  Worst-case payout if all win:    ${worst_case_obligation:.2f}")
    print(f"  Worst-case drawdown:             ${worst_drawdown:.2f}")
    print(f"  [GPT 33] kill: drawdown < -$5    → {'❌ FAIL' if worst_drawdown < -5 else '✅ pass'}")

    # Q6 — PnL by time-to-resolution
    section("Q6. PnL by time-to-resolution bucket")
    # We don't store hours_to_resolution per fill. Approximate via prev snapshot lookup.
    # Quick approach: bucket by elapsed_min between snapshots (less informative but available).
    ttr_buckets_strict = defaultdict(lambda: {"count": 0, "pnl": 0})
    for f in strict_fills:
        em = f.get("elapsed_min", 0) or 0
        if em <= 5:
            label = "0-5m"
        elif em <= 20:
            label = "5-20m"
        else:
            label = "20m+"
        ttr_buckets_strict[label]["count"] += 1
        ttr_buckets_strict[label]["pnl"] += (f.get("markout_pnl_per_share_strict") or 0) * (f.get("shares") or 0)
    print(f"  (proxy: elapsed between snapshots — true TTR requires snapshot enrichment)")
    print(f"  {'bucket':<10} {'count':<8} {'pnl':<10}")
    for k, v in sorted(ttr_buckets_strict.items()):
        print(f"  {k:<10} {v['count']:<8} ${v['pnl']:<8.2f}")

    # Q7 — PnL by ask price bucket
    section("Q7. PnL by ask price bucket (where we sat in the book)")
    price_buckets_strict = defaultdict(lambda: {"count": 0, "pnl": 0})
    for f in strict_fills:
        p = f.get("prev_sim_ask", 0) or 0
        if p <= 0.01:
            label = "≤$0.01"
        elif p <= 0.02:
            label = "$0.01-0.02"
        elif p <= 0.03:
            label = "$0.02-0.03"
        elif p <= 0.05:
            label = "$0.03-0.05"
        else:
            label = ">$0.05"
        price_buckets_strict[label]["count"] += 1
        price_buckets_strict[label]["pnl"] += (f.get("markout_pnl_per_share_strict") or 0) * (f.get("shares") or 0)
    print(f"  {'bucket':<14} {'count':<8} {'pnl':<10}")
    for k in ["≤$0.01", "$0.01-0.02", "$0.02-0.03", "$0.03-0.05", ">$0.05"]:
        if k in price_buckets_strict:
            v = price_buckets_strict[k]
            print(f"  {k:<14} {v['count']:<8} ${v['pnl']:<8.2f}")

    # Q8 — queue assumption
    section("Q8. Queue priority assumption (explicit)")
    print("""  Current strict fill model assumption:

      We sit at sim_ask = best_ask + 1 tick.
      A 'fill' counts only if:
        - Trades occurred at price >= sim_ask in the snapshot interval
        - AND  taker_volume_at_or_above_sim_ask > size_below_sim_ask

      'size_below_sim_ask' = sum of asks at lower price levels at quote time.
      We assume QUEUE BEHIND existing displayed asks at our level (none, since
      we're 1 tick above), but takers must clear all asks below us first.

      KNOWN UNDERESTIMATE: if other makers post at our same level (sim_ask)
      simultaneously, queue position is unknown — we assume worst case
      (fill only if taker volume > size_below + queue_at_our_level), which
      we currently simplify to size_below.

      KNOWN OVERESTIMATE: we don't model partial fills cancelled before our
      ticker; we assume full target shares fill if taker crosses.

      Net: model is moderately optimistic on fill counts, conservative on
      timing. Worth a $1 live canary only if forensic shows top-1 < 50%
      AND realized PnL ≈ markout PnL.""")

    # Q9 — GO/NO-GO
    section("Q9. GO / NO-GO for $1 live canary")
    go = True
    reasons = []
    if total_pnl <= 0:
        go = False
        reasons.append(f"total strict PnL ${total_pnl:.2f} not positive")
    if sorted_events and sorted_events[0][1] / total_pnl > 0.5:
        go = False
        reasons.append(f"top-1 event = {sorted_events[0][1] / total_pnl * 100:.0f}% of PnL (>50% threshold)")
    if worst_drawdown < -5:
        go = False
        reasons.append(f"worst drawdown ${worst_drawdown:.2f} (< -$5)")
    if realized_count > 0 and realized_pnl < 0:
        go = False
        reasons.append(f"realized PnL ${realized_pnl:.2f} negative on {realized_count} resolved fills")
    if len(by_event) < 5:
        go = False
        reasons.append(f"only {len(by_event)} distinct events — too concentrated")

    if go:
        print(f"  ✅ GO conditions met (preliminary). Recommend $1 canary on next 5 fills.")
    else:
        print(f"  ❌ NO-GO. Reasons:")
        for r in reasons:
            print(f"    - {r}")

    # Save summary
    if args.save:
        summary = {
            "ts": int(time.time()),
            "kind": "forensic_report",
            "strict_fills": len(strict_fills),
            "distinct_events": len(by_event),
            "total_pnl": round(total_pnl, 2),
            "top1_event_pct": round(sorted_events[0][1] / total_pnl * 100, 1) if sorted_events and total_pnl else 0,
            "top3_event_pct": round(sum(p for _, p in sorted_events[:3]) / total_pnl * 100, 1) if total_pnl else 0,
            "realized_pnl": round(realized_pnl, 2),
            "realized_count": realized_count,
            "worst_drawdown": round(worst_drawdown, 2),
            "go_no_go": "GO" if go else "NO-GO",
            "no_go_reasons": reasons,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("a") as fh:
            fh.write(json.dumps(summary) + "\n")
        print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
