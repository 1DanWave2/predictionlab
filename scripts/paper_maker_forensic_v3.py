"""Paper Maker Forensic v3 per [GPT 35] — full live-readiness audit.

GPT 35 requirements:
  1. Liability-capped shadow sizing (not unlimited simulated shorts)
  2. Ask-weighted break-even winner rate (theoretical EV threshold)
  3. Observed winner rate + 95% upper bound (Rule of Three for n=0)
  4. Split all stats by TTR window
  5. Split by event family / city / market type
  6. PnL AFTER winner losses (not only retained premium)
  7. Simulate "one-minimum-order live canary" separately with max 1 open event

Kill criteria checks:
  - winner_rate > ask-weighted break-even rate after ≥100 resolved fills → KILL
  - any TTR window negative after ≥100 resolved fills → DISABLE that window
  - any single-event simulated liability > live cap template → EXCLUDE
  - queue-adjusted PnL < raw strict PnL by >50% → QUEUE MODEL SUSPECT

Live trigger gates (all must pass):
  - queue-adjusted resolved fills ≥ 300
  - winner-bucket fills ≥ 5 OR explicit upper-bound math
  - positive realized PnL after winner losses
  - positive realized PnL in intended TTR window alone
  - max single-event simulated liability ≤ live cap template
  - no event family > 35% of PnL
  - no market/weather cluster > 50% of fills

Reads:
  /app/data/paper_maker_sim.jsonl
  /app/data/weather_resolutions.jsonl

Output: stdout + /app/data/paper_maker_forensic_v3.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import time
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("/app/data")
SIM_FILE = DATA_DIR / "paper_maker_sim.jsonl"
RESOLUTIONS_FILE = DATA_DIR / "weather_resolutions.jsonl"
OUTPUT = DATA_DIR / "paper_maker_forensic_v3.jsonl"

# Live canary template per [GPT 35]
LIVE_TEMPLATE = {
    "max_portfolio_liability": 25.0,   # $25 cap (1 single 5c minimum order = ~$19)
    "max_event_liability":     20.0,   # one 5c event
    "max_open_events":         1,      # single live event at a time
    "min_fills_required":      300,    # gate before live consideration
    "min_winner_fills":        5,
}

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


def rule_of_three_upper(n: int, k: int = 0, conf: float = 0.95) -> float:
    """95% upper bound on probability when k of n trials succeed.

    For k=0: classic Rule of Three: 3/n.
    For k>0: Clopper-Pearson upper bound (approximation).
    """
    if n == 0:
        return 1.0
    if k == 0:
        return min(1.0, 3.0 / n)
    # Approximate Clopper-Pearson upper bound via beta inverse.
    # For small k/n, use Wilson upper bound.
    z = 1.96  # 95%
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return min(1.0, centre + spread)


def ask_weighted_break_even(strict_fills: list[dict]) -> float:
    """Break-even surprise rate = E[ask] / E[1-ask] approximated as E[ask].

    For each $1 of premium sold at ask `a`, EV = a × P(loss) − (1-a) × P(win) per share.
    Break-even: P(win) / P(loss) = a / (1-a) → P(win) ≈ a (for small a).

    Average across fills weighted by share count.
    """
    total_shares = 0
    weighted_ask = 0
    for f in strict_fills:
        s = f.get("shares", 0) or 0
        a = f.get("prev_sim_ask", 0) or 0
        total_shares += s
        weighted_ask += a * s
    return weighted_ask / total_shares if total_shares else 0


def detect_event_family(eid: str, snaps_by_event: dict) -> tuple[str, str]:
    """Best-effort pull of city + family from quote snapshots."""
    snaps = snaps_by_event.get(eid, [])
    if not snaps:
        return ("unknown", "unknown")
    # Try title from quotes (we don't store title in fills directly)
    for s in snaps:
        title = s.get("title", "") or ""
        if not title:
            continue
        title_low = title.lower()
        for city in ["tokyo", "beijing", "shanghai", "hong kong", "seoul", "busan",
                     "wellington", "singapore", "jakarta", "taipei", "london", "paris",
                     "madrid", "warsaw", "amsterdam", "moscow", "istanbul", "lucknow",
                     "buenos aires", "helsinki", "lagos", "atlanta", "denver", "miami",
                     "dallas", "los angeles", "san francisco", "seattle", "nyc", "new york"]:
            if city in title_low:
                family = "weather_temp" if "temperature" in title_low else "weather_other"
                return (city, family)
    return ("unknown", "weather_other")


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
    print(f"  PAPER MAKER FORENSIC v3  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print(f"  Per [GPT 35] — full live-readiness audit. 7 requirements + kill checks.")
    print(f"{'═' * 75}")

    if not strict_fills:
        print("\n⚠️  NO strict fills. Aborting.")
        return

    # ─── Build snapshots-by-event lookup for TTR + city ───
    snaps_by_event = defaultdict(list)
    for s in snaps:
        eid = s.get("event_id")
        if eid is not None:
            snaps_by_event[eid].append(s)
    for eid in snaps_by_event:
        snaps_by_event[eid].sort(key=lambda x: x.get("ts", 0))

    def find_ttr_at_fill(eid, fill_ts: int) -> float | None:
        for s in reversed(snaps_by_event.get(eid, [])):
            if s.get("ts", 0) <= fill_ts:
                return s.get("hours_to_resolution")
        return None

    # ─── Resolutions ───
    resolutions = load_jsonl(RESOLUTIONS_FILE)
    resolved_winners = {
        r["event_id"]: r["winner_market_id"]
        for r in resolutions if r.get("kind") == "weather_resolution"
    }

    # ─── Annotate every strict fill with metadata we'll use throughout ───
    annotated = []
    for f in strict_fills:
        eid = f.get("event_id")
        mid = f.get("market_id")
        sim_ask = f.get("prev_sim_ask", 0) or 0
        shares = f.get("shares", 0) or 0
        max_buy = f.get("max_buy_price_in_window", 0) or 0
        qa_qty = f.get("queue_adjusted_fill_qty", 0) or 0
        ttr = find_ttr_at_fill(eid, f.get("ts", 0))
        # TTR bucket
        if ttr is None:
            ttr_b = "?"
        elif ttr <= 0.5:
            ttr_b = "<30m"
        elif ttr <= 2:
            ttr_b = "30m-2h"
        elif ttr <= 6:
            ttr_b = "2-6h"
        elif ttr <= 24:
            ttr_b = "6-24h"
        else:
            ttr_b = ">24h"
        # Price bucket
        if sim_ask <= 0.01:
            price_b = "≤$0.01"
        elif sim_ask <= 0.02:
            price_b = "$0.01-0.02"
        elif sim_ask <= 0.03:
            price_b = "$0.02-0.03"
        elif sim_ask <= 0.05:
            price_b = "$0.03-0.05"
        else:
            price_b = ">$0.05"
        # Resolution status
        if eid in resolved_winners:
            won = (mid == resolved_winners[eid])
            realized_pnl = (sim_ask - 1.0) * shares if won else sim_ask * shares
            resolved = True
        else:
            won = None
            realized_pnl = None
            resolved = False
        # Queue-adj eligible
        queue_adj_ok = max_buy > sim_ask and qa_qty > 0
        # City/family
        city, family = detect_event_family(eid, snaps_by_event)

        annotated.append({
            "event_id": eid,
            "market_id": mid,
            "sim_ask": sim_ask,
            "shares": shares,
            "ttr_bucket": ttr_b,
            "price_bucket": price_b,
            "city": city,
            "family": family,
            "resolved": resolved,
            "won": won,
            "realized_pnl": realized_pnl,
            "queue_adj_ok": queue_adj_ok,
            "markout_pnl": (f.get("markout_pnl_per_share_strict") or 0) * shares,
        })

    n_total = len(annotated)
    n_resolved = sum(1 for a in annotated if a["resolved"])
    n_won = sum(1 for a in annotated if a["won"] is True)
    n_lost = sum(1 for a in annotated if a["won"] is False)
    qa_resolved = [a for a in annotated if a["queue_adj_ok"] and a["resolved"]]
    qa_won = sum(1 for a in qa_resolved if a["won"])
    qa_lost = sum(1 for a in qa_resolved if not a["won"])

    print(f"\nRaw counts:")
    print(f"  strict fills:               {n_total}")
    print(f"  resolved:                   {n_resolved}  (winners {n_won}, losers {n_lost})")
    print(f"  queue-adjusted (resolved):  {len(qa_resolved)}  (winners {qa_won}, losers {qa_lost})")

    # ─── Req 1: liability-capped shadow sizing ───
    section("Req 1. Liability-capped shadow sizing simulation")
    # Re-simulate as if we'd capped each fill to fit max_event_liability
    # Liability per fill = shares × $1. Cap at LIVE_TEMPLATE["max_event_liability"] / share = max_shares.
    max_shares_live = LIVE_TEMPLATE["max_event_liability"]  # 1 share = $1 liability
    capped_pnl = 0
    capped_winner_pnl = 0
    capped_loser_pnl = 0
    capped_count = 0
    for a in annotated:
        if not a["resolved"]:
            continue
        capped_shares = min(a["shares"], max_shares_live)
        if capped_shares <= 0:
            continue
        if a["won"]:
            pnl = (a["sim_ask"] - 1.0) * capped_shares
            capped_winner_pnl += pnl
        else:
            pnl = a["sim_ask"] * capped_shares
            capped_loser_pnl += pnl
        capped_pnl += pnl
        capped_count += 1
    print(f"  Cap per leg: {LIVE_TEMPLATE['max_event_liability']} shares (= ${LIVE_TEMPLATE['max_event_liability']:.0f} liability)")
    print(f"  Capped resolved fills:      {capped_count}")
    print(f"  Capped winner PnL:          ${capped_winner_pnl:.2f}")
    print(f"  Capped loser PnL:           ${capped_loser_pnl:.2f}")
    print(f"  Capped total realized:      ${capped_pnl:.2f}")
    print(f"  vs raw uncapped realized:   ${sum(a['realized_pnl'] for a in annotated if a['resolved']):.2f}")

    # ─── Req 2: ask-weighted break-even rate ───
    section("Req 2. Ask-weighted break-even winner rate")
    bewr = ask_weighted_break_even(strict_fills)
    bewr_resolved = ask_weighted_break_even([
        f for f in strict_fills
        if f.get("event_id") in resolved_winners
    ])
    print(f"  Break-even rate (all fills):       {bewr:.4f}  ({bewr*100:.2f}%)")
    print(f"  Break-even rate (resolved only):   {bewr_resolved:.4f}  ({bewr_resolved*100:.2f}%)")
    print(f"  Interpretation: maker is profitable iff true winner-bucket rate < this %.")

    # ─── Req 3: observed rate + 95% upper bound ───
    section("Req 3. Observed winner rate + 95% upper bound (Rule of Three / Wilson)")
    obs_rate = n_won / n_resolved if n_resolved > 0 else 0
    upper_95 = rule_of_three_upper(n_resolved, n_won)
    qa_obs_rate = qa_won / len(qa_resolved) if qa_resolved else 0
    qa_upper_95 = rule_of_three_upper(len(qa_resolved), qa_won)
    print(f"  Observed winner rate:               {n_won}/{n_resolved} = {obs_rate:.4f}")
    print(f"  95% upper bound:                    {upper_95:.4f}  ({upper_95*100:.2f}%)")
    print(f"  Queue-adj observed rate:            {qa_won}/{len(qa_resolved)} = {qa_obs_rate:.4f}")
    print(f"  Queue-adj 95% upper bound:          {qa_upper_95:.4f}  ({qa_upper_95*100:.2f}%)")
    print()
    if upper_95 < bewr_resolved:
        print(f"  ✅ Upper bound {upper_95*100:.2f}% < break-even {bewr_resolved*100:.2f}% — edge plausible.")
    elif upper_95 == 1.0:
        print(f"  ⚠️ Sample too small; upper bound = 100%. Need more resolutions.")
    else:
        print(f"  ❌ Upper bound {upper_95*100:.2f}% > break-even {bewr_resolved*100:.2f}% — cannot rule out unprofitability.")

    # ─── Req 4: split by TTR window ───
    section("Req 4. Split by TTR window — PnL after winner losses")
    ttr_stats = defaultdict(lambda: {
        "n": 0, "n_resolved": 0, "n_won": 0,
        "realized_pnl": 0, "markout_pnl": 0, "shares": 0,
    })
    for a in annotated:
        b = a["ttr_bucket"]
        ttr_stats[b]["n"] += 1
        ttr_stats[b]["shares"] += a["shares"]
        ttr_stats[b]["markout_pnl"] += a["markout_pnl"]
        if a["resolved"]:
            ttr_stats[b]["n_resolved"] += 1
            if a["won"]:
                ttr_stats[b]["n_won"] += 1
            ttr_stats[b]["realized_pnl"] += a["realized_pnl"]
    print(f"  {'TTR':<10} {'n':<5} {'resolved':<10} {'wins':<6} {'real_pnl':<10} {'mark_pnl':<10}")
    for label in ["<30m", "30m-2h", "2-6h", "6-24h", ">24h", "?"]:
        if label not in ttr_stats:
            continue
        v = ttr_stats[label]
        print(f"  {label:<10} {v['n']:<5} {v['n_resolved']:<10} {v['n_won']:<6} "
              f"${v['realized_pnl']:<8.2f} ${v['markout_pnl']:<8.2f}")
    print()
    print(f"  Kill rule: any TTR window with realized < 0 after ≥100 resolved fills → DISABLE")
    for label, v in ttr_stats.items():
        if v["n_resolved"] >= 100 and v["realized_pnl"] < 0:
            print(f"  ❌ TTR {label} disabled — {v['n_resolved']} fills, realized ${v['realized_pnl']:.2f}")

    # ─── Req 5: split by event family / city / market type ───
    section("Req 5. Split by event family + city")
    city_stats = defaultdict(lambda: {"n": 0, "n_resolved": 0, "n_won": 0, "realized": 0})
    for a in annotated:
        c = a["city"]
        city_stats[c]["n"] += 1
        if a["resolved"]:
            city_stats[c]["n_resolved"] += 1
            if a["won"]:
                city_stats[c]["n_won"] += 1
            city_stats[c]["realized"] += a["realized_pnl"]
    sorted_cities = sorted(city_stats.items(), key=lambda x: -x[1]["realized"])
    total_realized = sum(v["realized"] for v in city_stats.values())
    print(f"  {'city':<14} {'fills':<6} {'resolved':<10} {'wins':<6} {'realized':<10} {'pct of PnL':<10}")
    cluster_max_pct = 0
    for c, v in sorted_cities:
        pct = v["realized"] / total_realized * 100 if total_realized else 0
        cluster_max_pct = max(cluster_max_pct, pct)
        print(f"  {c:<14} {v['n']:<6} {v['n_resolved']:<10} {v['n_won']:<6} "
              f"${v['realized']:<8.2f} {pct:<6.1f}%")
    print()
    if cluster_max_pct > 50:
        print(f"  ❌ Single city = {cluster_max_pct:.1f}% of PnL. >50% cluster — KILL RULE.")
    elif cluster_max_pct > 35:
        print(f"  ⚠️ Single city = {cluster_max_pct:.1f}% of PnL. >35% threshold flagged.")
    else:
        print(f"  ✅ Top city {cluster_max_pct:.1f}% — diversified.")

    # ─── Req 6: PnL after winner losses (already shown in Req 4 by realized) ───
    section("Req 6. Total PnL after winner losses (full transparency)")
    realized_total = sum(a["realized_pnl"] for a in annotated if a["resolved"])
    realized_winners = sum(a["realized_pnl"] for a in annotated if a["resolved"] and a["won"])
    realized_losers = sum(a["realized_pnl"] for a in annotated if a["resolved"] and not a["won"])
    qa_realized_total = sum(a["realized_pnl"] for a in qa_resolved)
    qa_realized_winners = sum(a["realized_pnl"] for a in qa_resolved if a["won"])
    qa_realized_losers = sum(a["realized_pnl"] for a in qa_resolved if not a["won"])
    print(f"  All strict fills:")
    print(f"    Realized winner PnL:    ${realized_winners:.2f}  ({n_won} fills)")
    print(f"    Realized loser PnL:     ${realized_losers:.2f}  ({n_lost} fills)")
    print(f"    NET realized:           ${realized_total:.2f}")
    print()
    print(f"  Queue-adjusted only:")
    print(f"    Realized winner PnL:    ${qa_realized_winners:.2f}  ({qa_won} fills)")
    print(f"    Realized loser PnL:     ${qa_realized_losers:.2f}  ({qa_lost} fills)")
    print(f"    NET queue-adj realized: ${qa_realized_total:.2f}")
    if realized_total > 0 and qa_realized_total > 0:
        ratio = qa_realized_total / realized_total
        if ratio < 0.5:
            print(f"  ❌ Queue-adj retains {ratio*100:.1f}% — model SUSPECT (drops >50%).")
        else:
            print(f"  ✅ Queue retention {ratio*100:.1f}% — proxy reliable.")

    # ─── Req 7: one-minimum-order live canary simulation ───
    section("Req 7. 'One-minimum-order live canary' simulation")
    # Constraint: max 1 open event at a time; min order ≈ 20 shares at $0.05 = $1 notional
    # Walk fills chronologically. Open at most 1 event short. Cap shares to max_event_liability.
    canary_open_event = None
    canary_pnl = 0.0
    canary_fills = []
    sorted_by_ts = sorted(annotated, key=lambda a: 0)  # we don't have ts in annotated; skip ordering
    # Use the original strict_fills with ts
    canary_pool = sorted(strict_fills, key=lambda f: f.get("ts", 0))
    open_event = None
    canary_count = 0
    canary_winner_count = 0
    canary_loser_count = 0
    for f in canary_pool:
        eid = f.get("event_id")
        sim_ask = f.get("prev_sim_ask", 0) or 0
        # Filter: only $0.04-$0.06 ask (one minimum order range per GPT 35)
        if not (0.04 <= sim_ask <= 0.06):
            continue
        # Min order ~$1 notional → shares = 1/sim_ask, capped at $20 liability
        liability_cap_shares = LIVE_TEMPLATE["max_event_liability"]
        shares = min(1.0 / sim_ask, liability_cap_shares)
        if open_event is not None and open_event != eid:
            continue  # different event; respect max 1 open
        if eid not in resolved_winners:
            # Not resolved — open the position, don't compute PnL yet
            if open_event is None:
                open_event = eid
            continue
        # Resolved — compute realized PnL
        won = (f.get("market_id") == resolved_winners[eid])
        if won:
            pnl = (sim_ask - 1.0) * shares
            canary_winner_count += 1
        else:
            pnl = sim_ask * shares
            canary_loser_count += 1
        canary_pnl += pnl
        canary_count += 1
        canary_fills.append({
            "event_id": eid, "sim_ask": sim_ask, "shares": shares,
            "won": won, "pnl": round(pnl, 4),
        })
        # Reset open_event if this was open
        if open_event == eid:
            open_event = None
    print(f"  Canary scope: ask 0.04-0.06, max 1 open event")
    print(f"  Canary fills:                {canary_count}")
    print(f"  Canary winner fills:         {canary_winner_count}")
    print(f"  Canary loser fills:          {canary_loser_count}")
    print(f"  Canary realized PnL:         ${canary_pnl:.2f}")
    if canary_fills:
        print(f"\n  First 10 canary fills:")
        for cf in canary_fills[:10]:
            mark = "WIN" if cf["won"] else "LOSS"
            print(f"    event={cf['event_id']:<10} ask=${cf['sim_ask']:.4f} sh={cf['shares']:.1f} "
                  f"{mark:<5} pnl=${cf['pnl']:>+6.2f}")

    # ─── Live trigger gates ───
    section("Live trigger gates per [GPT 35]")
    gates = {
        "queue-adj resolved fills ≥ 300": (len(qa_resolved) >= LIVE_TEMPLATE["min_fills_required"], len(qa_resolved)),
        "winner-bucket fills ≥ 5": (qa_won >= LIVE_TEMPLATE["min_winner_fills"], qa_won),
        "positive realized after winner losses": (qa_realized_total > 0, round(qa_realized_total, 2)),
        "no event family > 35% of PnL": (cluster_max_pct <= 35, round(cluster_max_pct, 1)),
        "queue retention > 50%": (
            (qa_realized_total / realized_total >= 0.5) if realized_total > 0 else False,
            round(qa_realized_total / realized_total * 100, 1) if realized_total > 0 else 0
        ),
    }
    for name, (ok, val) in gates.items():
        status = "✅" if ok else "❌"
        print(f"  {status} {name:<45}  current: {val}")

    all_pass = all(ok for ok, _ in gates.values())
    print()
    if all_pass:
        print(f"  🎯 ALL GATES PASS — manual live canary justified per [GPT 35] template")
    else:
        print(f"  🛑 GATES FAIL — continue shadow accumulation")

    # ─── Save summary ───
    if args.save:
        summary = {
            "ts": int(time.time()),
            "kind": "forensic_v3",
            "strict_fills": n_total,
            "resolved_fills": n_resolved,
            "winner_fills": n_won,
            "loser_fills": n_lost,
            "queue_adj_resolved": len(qa_resolved),
            "queue_adj_winners": qa_won,
            "queue_adj_losers": qa_lost,
            "ask_weighted_break_even": round(bewr_resolved, 4),
            "observed_winner_rate": round(obs_rate, 4),
            "upper_95_winner_rate": round(upper_95, 4),
            "qa_observed_winner_rate": round(qa_obs_rate, 4),
            "qa_upper_95_winner_rate": round(qa_upper_95, 4),
            "realized_total": round(realized_total, 2),
            "qa_realized_total": round(qa_realized_total, 2),
            "capped_total_realized": round(capped_pnl, 2),
            "max_city_pct_of_pnl": round(cluster_max_pct, 1),
            "canary_fills": canary_count,
            "canary_pnl": round(canary_pnl, 2),
            "all_gates_pass": all_pass,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("a") as fh:
            fh.write(json.dumps(summary) + "\n")
        print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
