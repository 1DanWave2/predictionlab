"""Asset_target DOLLAR PnL backtest per [Claude 54] / [GPT 43-45].

Available data: 6077 asset_target rows have fwd_ret_60m populated.

Method:
  1. Pull rows with fwd_ret_60m AND raw_edge >= 0.02
  2. Sweep thresholds {0.02, 0.04, 0.06, 0.08, 0.10, 0.12}
  3. For each threshold: simulate "would I trade at this threshold?"
        entry: poly_ask (or sim_entry_price if available)
        exit_60m: poly_bid_at_T (= entry * (1 + fwd_ret_60m))
        size: $1
        spread cost: $0.005
        dollar PnL = (exit - entry - spread) * size_shares
  4. Aggregate per cluster (asset + window_bucket)
  5. Train/validation split: 70/30 by chronology
  6. Report Sharpe + worst-decile + median + best threshold
  7. Apply [GPT 43] strict validation gates

Output: stdout + /app/data/asset_target_dollar_backtest.jsonl
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path('/app/data/paper_bot.db')
OUTPUT = Path('/app/data/asset_target_dollar_backtest.jsonl')
SPREAD_COST = 0.005
SIZE_USD = 1.0
THRESHOLDS = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12]
TRAIN_PCT = 0.70


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def simulate_dollar_pnl(entry_price, fwd_ret_pct, size_usd=SIZE_USD, spread=SPREAD_COST):
    """Compute dollar PnL of buying $size at entry, exiting at entry*(1+fwd_ret).

    Returns dollar PnL (after spread cost).
    Note: shares = size / entry. exit = entry * (1 + fwd_ret).
          gross_pnl = (exit - entry) * shares = fwd_ret * size
          spread_cost = shares * spread = size/entry * spread
    """
    if entry_price <= 0:
        return None
    gross_pnl = fwd_ret_pct * size_usd
    spread_cost_total = (size_usd / entry_price) * spread
    return round(gross_pnl - spread_cost_total, 4)


def main():
    print("=" * 78)
    print("  ASSET_TARGET DOLLAR PNL BACKTEST")
    print(f"  ts={datetime.now(timezone.utc).isoformat()}")
    print(f"  per [Claude 54] dollar framework + [GPT 43] strict validation")
    print("=" * 78)

    if not DB.exists():
        print("DB missing")
        return

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Pull eligible rows
    rows = c.execute(
        """SELECT id, raw_edge, tradable_edge, haircut, poly_ask, poly_bid,
                  sim_entry_price, fwd_ret_60m, fwd_ret_180m, slug, league,
                  market_type, decision, created_at
           FROM opportunity_logs
           WHERE market_type='asset_target'
             AND fwd_ret_60m IS NOT NULL
             AND raw_edge >= 0.02
           ORDER BY created_at"""
    ).fetchall()

    print(f"\n  eligible rows: {len(rows)}")
    if len(rows) < 50:
        print("  ❌ insufficient sample, abort")
        return

    # Convert to dicts
    trades = []
    for r in rows:
        rid, raw_edge, tr_edge, hr, p_ask, p_bid, sim_ent, f60, f180, slug, league, mt, dec, ct = r
        entry = sim_ent or p_ask
        if not entry or entry <= 0:
            continue
        pnl_60 = simulate_dollar_pnl(entry, f60)
        if pnl_60 is None:
            continue
        trades.append({
            "id": rid,
            "raw_edge": raw_edge,
            "entry": entry,
            "fwd_60m": f60,
            "fwd_180m": f180,
            "pnl_60m": pnl_60,
            "slug": slug or "?",
            "league": league or "?",
            "ct": ct,
        })
    print(f"  trades scored: {len(trades)}")

    # Train/validation split (chronological)
    cutoff_idx = int(len(trades) * TRAIN_PCT)
    train = trades[:cutoff_idx]
    val = trades[cutoff_idx:]
    print(f"  train: {len(train)} rows | val: {len(val)} rows")

    # ── 20 example trades ──
    print(f"\n{'─' * 78}")
    print("  20 EXAMPLE TRADES (showing dollar PnL)")
    print(f"{'─' * 78}")
    print(f"  {'#':<3} {'raw_edge':<10} {'entry':<8} {'fwd_60m':<10} {'pnl_$':<10} {'slug':<35}")
    sample = trades[::max(1, len(trades)//20)][:20]
    for i, t in enumerate(sample, 1):
        print(f"  {i:<3} {t['raw_edge']:<10.4f} ${t['entry']:<7.4f} "
              f"{t['fwd_60m']:<+10.4f} ${t['pnl_60m']:<+9.4f} {t['slug'][:35]}")

    # ── Threshold sweep on TRAIN ──
    print(f"\n{'─' * 78}")
    print(f"  THRESHOLD SWEEP — TRAIN ({len(train)} rows)")
    print(f"{'─' * 78}")
    print(f"  {'thresh':<8} {'n':<5} {'WR':<7} {'avg_$':<10} {'med_$':<10} {'std':<8} {'sharpe':<8} {'worst':<10}")
    train_results = []
    for th in THRESHOLDS:
        sample = [t for t in train if t["raw_edge"] >= th]
        if not sample:
            continue
        pnls = [t["pnl_60m"] for t in sample]
        wins = sum(1 for p in pnls if p > 0)
        n = len(pnls)
        avg = sum(pnls) / n
        med = sorted(pnls)[n // 2]
        sd = stdev(pnls)
        sharpe = avg / sd if sd > 0 else 0
        worst = min(pnls)
        train_results.append({
            "threshold": th,
            "n": n,
            "wr": wins / n,
            "avg_dollar": avg,
            "median_dollar": med,
            "stdev": sd,
            "sharpe": sharpe,
            "worst": worst,
        })
        print(f"  {th*100:>4.1f}%   {n:<5} {wins*100/n:>5.1f}%  ${avg:<+8.4f}  ${med:<+8.4f}  {sd:<8.4f} {sharpe:<+7.3f} ${worst:<+8.4f}")

    # ── Pick best threshold from train ──
    if not train_results:
        print("  no train results")
        return
    best_train = max(train_results, key=lambda x: x["sharpe"])
    print(f"\n  TRAIN BEST: threshold={best_train['threshold']*100:.1f}% "
          f"sharpe={best_train['sharpe']:+.3f} avg=${best_train['avg_dollar']:+.4f}")

    # ── Validate on holdout ──
    print(f"\n{'─' * 78}")
    print(f"  VALIDATION (best threshold {best_train['threshold']*100:.1f}% on val set)")
    print(f"{'─' * 78}")
    val_sample = [t for t in val if t["raw_edge"] >= best_train["threshold"]]
    if val_sample:
        val_pnls = [t["pnl_60m"] for t in val_sample]
        val_wins = sum(1 for p in val_pnls if p > 0)
        val_n = len(val_pnls)
        val_avg = sum(val_pnls) / val_n
        val_med = sorted(val_pnls)[val_n // 2]
        val_sd = stdev(val_pnls)
        val_sharpe = val_avg / val_sd if val_sd > 0 else 0
        val_worst = min(val_pnls)
        print(f"  val_n:        {val_n}")
        print(f"  val_WR:       {val_wins*100/val_n:.1f}%")
        print(f"  val_avg_$:    ${val_avg:+.4f}")
        print(f"  val_med_$:    ${val_med:+.4f}")
        print(f"  val_sharpe:   {val_sharpe:+.3f}")
        print(f"  val_worst_$:  ${val_worst:+.4f}")
    else:
        print(f"  ❌ 0 val sample at threshold {best_train['threshold']*100:.1f}%")
        val_sharpe = -999
        val_avg = -999
        val_n = 0

    # ── Cluster contribution check (per [GPT 43]: no single asset >30% PnL) ──
    print(f"\n{'─' * 78}")
    print(f"  CLUSTER CONCENTRATION (top 5 leagues by row count)")
    print(f"{'─' * 78}")
    by_league = defaultdict(list)
    for t in trades:
        if t["raw_edge"] >= best_train["threshold"]:
            by_league[t["league"]].append(t["pnl_60m"])
    sorted_leagues = sorted(by_league.items(), key=lambda x: -len(x[1]))
    total_pnl = sum(sum(v) for v in by_league.values())
    for lg, pnls in sorted_leagues[:5]:
        sum_pnl = sum(pnls)
        pct = sum_pnl / total_pnl * 100 if total_pnl else 0
        print(f"  {lg[:25]:<27} n={len(pnls):>4}  sum=${sum_pnl:+.4f} ({pct:+.1f}% of total)")

    # ── Verdict per [GPT 43] gates ──
    print(f"\n{'═' * 78}")
    print(f"  VERDICT (per [GPT 43] strict validation gates)")
    print(f"{'═' * 78}")
    gates = {
        "validation_sample >= 50": val_n >= 50,
        "validation median > 0": val_n >= 50 and sorted([t['pnl_60m'] for t in val_sample])[val_n//2] > 0 if val_n else False,
        "validation avg > $0.005 (after $0.005 spread)": val_n >= 50 and val_avg > 0.005 if val_n else False,
        "validation sharpe > 0.5": val_n >= 50 and val_sharpe > 0.5 if val_n else False,
        "no single league >30% positive PnL": all(
            sum(v) <= total_pnl * 0.30 if total_pnl > 0 else True
            for k, v in sorted_leagues[:5]
        ),
    }
    all_pass = all(gates.values())
    for gate, ok in gates.items():
        print(f"  {'✅' if ok else '❌'} {gate}")
    print()
    if all_pass:
        print(f"  ✅ THRESHOLD {best_train['threshold']*100:.1f}% PASSES validation")
        print(f"  Recommend: deploy asset_target_canary at $1 with this threshold")
    else:
        print(f"  ❌ FAILS one or more gates — keep current threshold (12%) or higher")
        print(f"  Best train threshold {best_train['threshold']*100:.1f}% does not survive holdout.")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "asset_target_dollar_v1",
            "n_eligible": len(trades),
            "n_train": len(train),
            "n_val": len(val),
            "best_train_threshold": best_train["threshold"],
            "best_train_sharpe": best_train["sharpe"],
            "val_n": val_n,
            "val_avg_dollar": val_avg if val_n else None,
            "val_sharpe": val_sharpe if val_n else None,
            "all_gates_pass": all_pass,
            "train_results": train_results,
        }) + "\n")
    print(f"\n  saved to {OUTPUT}")


if __name__ == "__main__":
    main()
