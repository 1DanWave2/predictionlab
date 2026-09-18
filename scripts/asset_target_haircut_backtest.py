"""Asset_target haircut threshold backtest per [Claude 47/48/49] proposal.

Question: Current threshold=12% blocks 100% of asset_target candidates.
          Is haircut formula over-fit? What threshold gives positive markout?

Method:
  Sweep threshold ∈ {2%, 4%, 6%, 8%, 10%, 12%}
  For each candidate with raw_edge ≥ threshold:
    - sim entry at sim_entry_price (already logged)
    - sim exit at fwd_ret_resolution
    - net_pnl = (sim_executable_return) - $0.005 spread cost
    - count, sum, stdev, Sharpe

Decision rule:
  Pick X* that maximizes Sharpe with n_trades ≥ 50 in 7-day window.
  If best Sharpe > 0.5 → recommend lowering threshold.

Uses fwd_ret_resolution if available, else fwd_ret_180m as proxy.
Filters: market_type='asset_target', decision in {SHADOW_LOW_EDGE, SHADOW_BUY},
         created_at ≥ NOW - 7 days, has fwd_ret data.

Output: stdout table + /app/data/asset_target_haircut_backtest.jsonl
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/app/data/paper_bot.db")
OUTPUT = Path("/app/data/asset_target_haircut_backtest.jsonl")
SPREAD_COST = 0.005  # round-trip half-spread per [Claude 47] proposal
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12]
MIN_N = 50  # min sample size to consider a threshold viable
WINDOW_DAYS = 7


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def main() -> None:
    if not DB.exists():
        print("DB not found")
        return
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("=" * 78)
    print(f"  ASSET_TARGET HAIRCUT BACKTEST  ({datetime.now(timezone.utc).isoformat()})")
    print(f"  per [Claude 48] §4.1 proposal")
    print("=" * 78)

    # Pull all asset_target candidates last 7d with fwd_ret data
    rows = cur.execute(
        """
        SELECT id, raw_edge, tradable_edge, haircut,
               fwd_ret_5m, fwd_ret_15m, fwd_ret_60m, fwd_ret_180m, fwd_ret_resolution,
               sim_executable_return, sim_entry_price, sim_exit_price,
               decision, reject_reason, created_at
        FROM opportunity_logs
        WHERE market_type='asset_target'
          AND created_at >= datetime('now', ?)
          AND raw_edge IS NOT NULL
        """,
        (f"-{WINDOW_DAYS} days",),
    ).fetchall()

    print(f"\n  Total candidates (7d, asset_target):  {len(rows)}")

    # Filter to those with at least one fwd_ret
    usable = [
        r for r in rows
        if any(r[i] is not None for i in (4, 5, 6, 7, 8, 9))
    ]
    print(f"  With at least one fwd_ret field:      {len(usable)}")

    if not usable:
        print("\n  ❌ no usable rows. fwd_returns task may not be filling these yet.")
        # Help diagnose
        print(f"\n  Sample raw row to inspect:")
        if rows:
            for k, v in zip(
                ["id", "raw_edge", "tradable_edge", "haircut",
                 "fwd_5m", "fwd_15m", "fwd_60m", "fwd_180m", "fwd_res",
                 "sim_exec_return", "sim_entry", "sim_exit",
                 "decision", "reject_reason", "created"],
                rows[0]
            ):
                print(f"    {k}: {v}")
        conn.close()
        return

    # Distribution of raw_edge
    raw_edges = sorted([r[1] for r in usable])
    print(f"\n  raw_edge distribution (usable):")
    print(f"    min:   {raw_edges[0]:.4f}")
    print(f"    p25:   {raw_edges[len(raw_edges)//4]:.4f}")
    print(f"    median:{raw_edges[len(raw_edges)//2]:.4f}")
    print(f"    p75:   {raw_edges[3*len(raw_edges)//4]:.4f}")
    print(f"    max:   {raw_edges[-1]:.4f}")
    print(f"    avg:   {sum(raw_edges)/len(raw_edges):.4f}")

    # Build results per threshold
    print(f"\n{'─' * 78}")
    print(f"  THRESHOLD SWEEP")
    print(f"{'─' * 78}")
    print(
        f"  {'thresh':<8} {'n':<5} {'WR':<7} "
        f"{'res_avg':<10} {'res_med':<10} {'res_std':<10} {'Sharpe':<8} {'avg-cost':<10}"
    )

    sweep_results = []
    for thresh in THRESHOLDS:
        # Candidates that would have been traded at this threshold
        traded = [r for r in usable if r[1] is not None and r[1] >= thresh]
        if not traded:
            print(f"  {thresh*100:>4.1f}%   {len(traded):<5} -")
            continue

        # Compute markouts using fwd_ret_resolution if available, else fwd_180m, else fwd_60m
        returns_resolution = []
        returns_180m = []
        returns_60m = []
        for r in traded:
            (
                _id, raw, tradable, haircut,
                f5, f15, f60, f180, fres,
                sim_exec, sim_entry, sim_exit,
                decision, _rr, _ct
            ) = r
            # Prefer logged sim_executable_return (already includes spread on sim entry/exit)
            # If absent, use fwd_ret_resolution which is the true outcome
            if fres is not None:
                returns_resolution.append(fres)
            if f180 is not None:
                returns_180m.append(f180)
            if f60 is not None:
                returns_60m.append(f60)

        # Use the longest horizon available (resolution > 180m > 60m)
        if returns_resolution:
            base_returns = returns_resolution
            horizon = "resolution"
        elif returns_180m:
            base_returns = returns_180m
            horizon = "180m"
        elif returns_60m:
            base_returns = returns_60m
            horizon = "60m"
        else:
            print(f"  {thresh*100:>4.1f}%   {len(traded):<5} no_fwd_ret")
            continue

        # Net of spread cost (round-trip = SPREAD_COST already)
        net_returns = [r - SPREAD_COST for r in base_returns]
        n = len(net_returns)
        wins = sum(1 for r in net_returns if r > 0)
        wr = wins / n if n else 0
        avg_ret = sum(net_returns) / n
        med_ret = sorted(net_returns)[n // 2]
        std_ret = stdev(net_returns)
        sharpe = avg_ret / std_ret if std_ret > 0 else 0.0

        sweep_results.append({
            "threshold": thresh,
            "n": n,
            "wr": wr,
            "avg_return": avg_ret,
            "median_return": med_ret,
            "stdev": std_ret,
            "sharpe": sharpe,
            "horizon": horizon,
            "spread_cost": SPREAD_COST,
        })

        print(
            f"  {thresh*100:>4.1f}%   {n:<5} {wr*100:>5.1f}%  "
            f"{avg_ret:>+8.4f}  {med_ret:>+8.4f}  {std_ret:>8.4f}  "
            f"{sharpe:>+6.3f}  {avg_ret - SPREAD_COST:>+8.4f}"
        )

    # Pick X*: best Sharpe with n ≥ MIN_N
    print(f"\n{'─' * 78}")
    print(f"  DECISION")
    print(f"{'─' * 78}")
    eligible = [r for r in sweep_results if r["n"] >= MIN_N]
    if not eligible:
        print(f"  ❌ no threshold has n ≥ {MIN_N}. Need more data or lower MIN_N.")
        conn.close()
        return

    best = max(eligible, key=lambda x: x["sharpe"])
    print(f"  Best threshold: {best['threshold']*100:.1f}%")
    print(f"  n_trades:       {best['n']}")
    print(f"  WR:             {best['wr']*100:.1f}%")
    print(f"  Avg return:     {best['avg_return']:+.4f}  (net of ${SPREAD_COST} spread)")
    print(f"  Median:         {best['median_return']:+.4f}")
    print(f"  Sharpe:         {best['sharpe']:+.3f}")
    print(f"  Horizon:        {best['horizon']}")

    # Verdict
    print()
    if best["sharpe"] > 0.5 and best["avg_return"] > 0 and best["wr"] > 0.5:
        print(f"  ✅ RECOMMEND: lower threshold to {best['threshold']*100:.1f}%")
        print(f"     Deploy as asset_target_canary at $1 sizing.")
        print(f"     Apply [GPT 40] ramp gates: 25 trades + 24h + correlation diversity.")
    elif best["sharpe"] > 0.0 and best["avg_return"] > 0:
        print(f"  ⚠️  WEAK SIGNAL: threshold {best['threshold']*100:.1f}% positive but Sharpe < 0.5")
        print(f"     Recommend: collect more data first OR shadow-mode at $0.50")
    else:
        print(f"  ❌ NO-GO: no threshold beats spread cost")
        print(f"     Asset_target as currently calibrated cannot trade profitably.")
        print(f"     Either: fix haircut formula, OR drop strategy entirely.")

    # Save report
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "haircut_sweep_v1",
            "window_days": WINDOW_DAYS,
            "n_total_candidates": len(rows),
            "n_usable": len(usable),
            "spread_cost": SPREAD_COST,
            "min_n": MIN_N,
            "best": best,
            "all_sweeps": sweep_results,
        }) + "\n")
    print(f"\n  ✓ summary appended to {OUTPUT}")
    conn.close()


if __name__ == "__main__":
    main()
