"""SM mirror/fade DOLLAR-PNL backtest per [GPT 45] requirements.

Replaces sm_mirror_backtest.py per-share markout (which was decoration on
binary markets at P!=0.5).

Logic:
  1. Load weather_resolutions → map (city, date) → winner_temp
  2. For each SM signal (resolved), compute:
        wallet_yes_price       (their entry)
        our_no_ask_price      (= 1 - their_yes for symmetric proxy)
        shares_at_$1_stake    (= 1.0 / our_no_ask_price)
        spread_cost           ($0.005)
        outcome_payout        ($1 if NO wins, $0 if NO loses)
        dollars_pnl_per_$1    (= shares * payout - $1 - shares*spread_cost)
  3. Aggregate:
        per signal (raw)
        per event (one trade per city/date — first signal wins, $1 cap)
  4. Report:
        20 concrete examples
        worst event PnL
        worst day PnL
        near-total losses count (PnL < -$0.50)
        max simultaneous exposure
        denominator reconciliation (resolved vs scored vs skipped)

Output: stdout + /app/data/sm_dollar_backtest.jsonl
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RESOLUTIONS = Path("/app/data/weather_resolutions.jsonl")
SIGNALS = Path("/app/data/sm_weather_signals.jsonl")
OUTPUT = Path("/app/data/sm_dollar_backtest.jsonl")
SPREAD_COST = 0.005
SIZE_USD = 1.0


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def parse_temp(text):
    m = re.search(r"(\d{1,2})\s*°?\s*[cf]", text.lower())
    return int(m.group(1)) if m else None


def parse_city_date(text):
    t = text.lower()
    cities = ["tokyo", "taipei", "jakarta", "manila", "bangkok", "singapore",
              "hong kong", "seoul", "shanghai", "beijing", "mumbai", "delhi",
              "miami", "austin", "dallas", "phoenix", "new york", "los angeles"]
    city = next((c for c in cities if c in t), None)
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})", t)
    date = f"{m.group(1)} {m.group(2)}" if m else None
    return city, date


def fade_dollar_pnl(their_yes_price: float, is_winner: bool) -> dict:
    """Compute dollar PnL of fading wallet's BUY-Yes with our BUY-No on same bucket.

    is_winner: True if wallet's bucket WAS the winner (their YES paid out).
               In that case, NO outcome = $0 → we lose entry capital.
               Else: NO outcome = $1 → we win (1 - entry_price - spread).
    """
    our_no_ask = 1.0 - their_yes_price
    if our_no_ask <= 0 or our_no_ask >= 1:
        return None
    shares = SIZE_USD / our_no_ask  # buy as many shares of NO as $1 buys
    spread_total = shares * SPREAD_COST
    if is_winner:
        # NO loses: payout = 0
        payout = 0.0
    else:
        # NO wins: payout = $1 per share
        payout = shares * 1.0
    pnl = payout - SIZE_USD - spread_total
    return {
        "their_yes": their_yes_price,
        "our_no_ask": round(our_no_ask, 4),
        "shares": round(shares, 4),
        "spread_total": round(spread_total, 4),
        "is_winner": is_winner,
        "payout": round(payout, 4),
        "pnl_dollar": round(pnl, 4),
    }


def main():
    print("=" * 78)
    print(f"  SM DOLLAR-PNL BACKTEST  (proper per [GPT 45])")
    print(f"  ts={datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    if not RESOLUTIONS.exists() or not SIGNALS.exists():
        print("missing input files")
        return

    # Build resolution map
    resolutions = []
    with RESOLUTIONS.open() as fh:
        for ln in fh:
            try:
                resolutions.append(json.loads(ln))
            except Exception:
                pass
    res_map = {}
    for r in resolutions:
        city, date = parse_city_date(r.get("title", ""))
        wt = parse_temp(r.get("winner_bucket", ""))
        if city and date and wt:
            res_map[(city, date)] = wt
    print(f"\n  resolutions loaded: {len(resolutions)}, map keys: {len(res_map)}")

    # Process signals
    signals = []
    with SIGNALS.open() as fh:
        for ln in fh:
            try:
                signals.append(json.loads(ln))
            except Exception:
                pass

    print(f"  signals loaded: {len(signals)}")

    # Filter to FADE candidate (0xc80fa1fc) BUY-Yes signals on resolved events
    fade_wallet = "0xc80fa1fc5740dec6"
    by_wallet = defaultdict(list)
    skip_no_match = 0
    skip_unresolved = 0
    skip_other = 0
    for s in signals:
        wallet = s.get("wallet", "")
        if wallet[:18] != fade_wallet:
            continue
        if s.get("side") != "BUY" or s.get("outcome") != "Yes":
            skip_other += 1
            continue
        their_price = s.get("price", 0) or 0
        if their_price <= 0 or their_price >= 1:
            skip_other += 1
            continue
        title = (s.get("title") or "").lower()
        city, date = parse_city_date(title)
        bucket = parse_temp(title)
        if not (city and date and bucket):
            skip_no_match += 1
            continue
        if (city, date) not in res_map:
            skip_unresolved += 1
            continue
        winner = res_map[(city, date)]
        is_winner = bucket == winner
        result = fade_dollar_pnl(their_price, is_winner)
        if result is None:
            skip_other += 1
            continue
        result["title"] = title[:60]
        result["city"] = city
        result["date"] = date
        result["bucket"] = bucket
        result["winner"] = winner
        result["fill_ts"] = s.get("fill_ts", 0)
        by_wallet[wallet].append(result)

    fade_trades = list(by_wallet.values())[0] if by_wallet else []
    n = len(fade_trades)
    print(f"\n  scoring breakdown:")
    print(f"    fade_wallet trades scored:    {n}")
    print(f"    skip_no_city_date_bucket:     {skip_no_match}")
    print(f"    skip_unresolved (city,date):  {skip_unresolved}")
    print(f"    skip_other (SELL/non-Yes):    {skip_other}")

    if n == 0:
        print("\n  ❌ no scored trades")
        return

    # ── Print 20 concrete examples ──
    print(f"\n{'─' * 78}")
    print(f"  20 EXAMPLE TRADES (per [GPT 45] req 1)")
    print(f"{'─' * 78}")
    print(f"  {'#':<3} {'their_YES':<10} {'our_NO':<8} {'shares':<7} "
          f"{'win?':<5} {'payout':<8} {'pnl_$':<10} {'event':<25}")
    for i, t in enumerate(fade_trades[:20], 1):
        evt = f"{t['city']}/{t['date']}/{t['bucket']}"[:25]
        win_str = "YES" if t["is_winner"] else "no"
        print(f"  {i:<3} ${t['their_yes']:<9.4f} ${t['our_no_ask']:<7.4f} "
              f"{t['shares']:<7.2f} {win_str:<5} ${t['payout']:<7.4f} "
              f"${t['pnl_dollar']:<+9.4f} {evt}")

    # ── Per-signal totals ──
    print(f"\n{'─' * 78}")
    print(f"  PER-SIGNAL DOLLAR PNL (raw — naive $1 per signal sizing)")
    print(f"{'─' * 78}")
    pnls = [t["pnl_dollar"] for t in fade_trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    voids = sum(1 for p in pnls if p == 0)
    cum = sum(pnls)
    print(f"  n_trades:       {n}")
    print(f"  wins:           {wins} ({wins*100/n:.1f}%)")
    print(f"  losses:         {losses} ({losses*100/n:.1f}%)")
    print(f"  voids:          {voids}")
    print(f"  cum_pnl_$:      ${cum:+.2f}")
    print(f"  avg_$_per_trade: ${cum/n:+.4f}")
    print(f"  median_$:       ${sorted(pnls)[n//2]:+.4f}")
    print(f"  stdev_$:        ${stdev(pnls):.4f}")
    print(f"  worst_$:        ${min(pnls):+.4f}")
    print(f"  best_$:         ${max(pnls):+.4f}")
    near_total_losses = sum(1 for p in pnls if p < -0.50)
    print(f"  near_total_losses (<-$0.50): {near_total_losses}")

    # ── Per-event aggregation (one $1 per city/date) ──
    print(f"\n{'─' * 78}")
    print(f"  PER-EVENT DOLLAR PNL (one $1 stake per city/date — proper sizing)")
    print(f"{'─' * 78}")
    by_event = defaultdict(list)
    for t in fade_trades:
        by_event[(t["city"], t["date"])].append(t)

    # For each event, take FIRST signal as our $1 trade
    event_pnls = []
    print(f"  {'event':<30} {'n_signals':<10} {'first_pnl_$':<13} {'avg_pnl_$':<12}")
    for evt, trades in sorted(by_event.items()):
        first = trades[0]
        all_pnls = [t["pnl_dollar"] for t in trades]
        avg = sum(all_pnls) / len(all_pnls)
        event_pnls.append(first["pnl_dollar"])
        print(f"  {evt[0]}/{evt[1]:<20} {len(trades):<10} "
              f"${first['pnl_dollar']:<+12.4f} ${avg:<+11.4f}")
    n_evt = len(event_pnls)
    cum_evt = sum(event_pnls)
    print(f"\n  EVENT-LEVEL TOTALS:")
    print(f"    n_events:      {n_evt}")
    print(f"    cum_$:         ${cum_evt:+.2f}")
    print(f"    avg_$ per evt: ${cum_evt/n_evt:+.4f}")
    print(f"    median_$:      ${sorted(event_pnls)[n_evt//2]:+.4f}")
    print(f"    stdev_$:       ${stdev(event_pnls):.4f}")
    print(f"    worst_event:   ${min(event_pnls):+.4f}")

    # ── Tail report ──
    print(f"\n{'─' * 78}")
    print(f"  TAIL REPORT (per [GPT 45] req 4)")
    print(f"{'─' * 78}")
    sorted_pnls = sorted(event_pnls)
    p10 = sorted_pnls[max(0, len(sorted_pnls)//10 - 1)]
    p25 = sorted_pnls[max(0, len(sorted_pnls)//4 - 1)]
    p50 = sorted_pnls[len(sorted_pnls)//2]
    p75 = sorted_pnls[3*len(sorted_pnls)//4]
    p90 = sorted_pnls[max(0, 9*len(sorted_pnls)//10 - 1)]
    print(f"  p10_event:  ${p10:+.4f}")
    print(f"  p25_event:  ${p25:+.4f}")
    print(f"  p50_event:  ${p50:+.4f}")
    print(f"  p75_event:  ${p75:+.4f}")
    print(f"  p90_event:  ${p90:+.4f}")
    near_total_evts = sum(1 for p in event_pnls if p < -0.50)
    print(f"  events with PnL < -$0.50:  {near_total_evts}")

    # By calendar day
    by_day = defaultdict(list)
    for t in fade_trades:
        if t.get("fill_ts"):
            day = datetime.fromtimestamp(t["fill_ts"], timezone.utc).strftime("%Y-%m-%d")
            by_day[day].append(t["pnl_dollar"])
    print(f"\n  per-day pnl:")
    for day in sorted(by_day.keys())[-10:]:
        d_pnls = by_day[day]
        print(f"    {day}: n={len(d_pnls)}, sum=${sum(d_pnls):+.4f}")

    # ── Verdict ──
    print(f"\n{'═' * 78}")
    print(f"  VERDICT")
    print(f"{'═' * 78}")
    if cum_evt > 0 and p10 > -0.50 and stdev(event_pnls) < 0.50:
        print(f"  ✅ STRATEGY POSITIVE EV WITH BOUNDED TAIL")
        print(f"  cum_$={cum_evt:+.2f} on {n_evt} events, p10=${p10:+.4f}")
    elif cum_evt > 0:
        print(f"  ⚠️  POSITIVE BUT FAT TAIL — events with -$0.50+ losses present")
        print(f"  Need wider sample before live")
    else:
        print(f"  ❌ NEGATIVE EV — cum_$={cum_evt:+.2f}")
        print(f"  Strategy as constructed loses money. Kill or rethink.")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "dollar_backtest_v1",
            "n_trades": n,
            "n_events": n_evt,
            "cum_dollar_per_signal": round(cum, 4),
            "cum_dollar_per_event": round(cum_evt, 4),
            "avg_per_signal": round(cum / n, 4),
            "avg_per_event": round(cum_evt / n_evt, 4),
            "wins": wins, "losses": losses, "voids": voids,
            "p10_event": round(p10, 4),
            "p50_event": round(p50, 4),
            "near_total_losses_signal": near_total_losses,
            "near_total_losses_event": near_total_evts,
            "skip_no_match": skip_no_match,
            "skip_unresolved": skip_unresolved,
            "skip_other": skip_other,
        }) + "\n")
    print(f"\n  saved to {OUTPUT}")


if __name__ == "__main__":
    main()
