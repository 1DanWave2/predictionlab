"""MFE/MAE retrospective report per [GPT 25].

Analyzes closed event_strategy cycles. For each BUY → exit pair,
computes max favorable / adverse excursion from MarketSnapshot history.

Output: per-cycle MFE, MAE, gave_back_pct, peak_age_minutes.
Helps identify exit improvement rules.
"""
from __future__ import annotations

import statistics
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func
from app.db import db_session
from app.models import PaperOrder, MarketSnapshot


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def main() -> None:
    print("=" * 76)
    print("MFE/MAE retrospective report (closed event_strategy cycles)")
    print("=" * 76)

    with db_session() as s:
        orders = s.execute(
            select(PaperOrder).order_by(PaperOrder.id)
        ).scalars().all()

        # Pair: BUY entry → first SELL closing it
        cycles = []
        open_buys = {}  # market_id → BUY order
        for o in orders:
            if o.side.upper() == "BUY":
                open_buys[o.market_id] = o
            elif o.side.upper() == "SELL" and o.market_id in open_buys:
                buy = open_buys.pop(o.market_id)
                cycles.append((buy, o))

        print(f"\n[1] Found {len(cycles)} closed cycles\n")

        rows = []
        for buy, sell in cycles:
            # Fetch snapshots между entry and exit
            buy_ts = _as_utc(buy.created_at)
            sell_ts = _as_utc(sell.created_at)
            snaps = s.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.market_id == buy.market_id)
                .where(MarketSnapshot.created_at >= buy_ts)
                .where(MarketSnapshot.created_at <= sell_ts)
                .order_by(MarketSnapshot.created_at)
            ).scalars().all()

            if not snaps:
                continue

            entry = buy.price
            mids = [(_as_utc(sn.created_at), (sn.best_bid + sn.best_ask) / 2 if sn.best_bid > 0 and sn.best_ask > 0 else sn.last_price) for sn in snaps]
            mids = [(t, m) for t, m in mids if m > 0]
            if not mids:
                continue

            mfe_pct = max((m - entry) / entry for _, m in mids)
            mae_pct = min((m - entry) / entry for _, m in mids)
            mfe_at = next(t for t, m in mids if (m - entry) / entry == mfe_pct)
            age_to_mfe = (mfe_at - buy_ts).total_seconds() / 60.0

            exit_pct = (sell.price - entry) / entry
            gave_back_pct = (mfe_pct - exit_pct) / mfe_pct if mfe_pct > 0 else 0
            cycle_mins = (sell_ts - buy_ts).total_seconds() / 60.0

            rows.append({
                "buy_id": buy.id,
                "sell_id": sell.id,
                "market_id": buy.market_id,
                "strategy": buy.strategy,
                "entry": round(entry, 4),
                "mfe_pct": round(mfe_pct * 100, 1),
                "mae_pct": round(mae_pct * 100, 1),
                "exit_pct": round(exit_pct * 100, 1),
                "gave_back_pct": round(gave_back_pct * 100, 1),
                "age_to_mfe_min": round(age_to_mfe, 1),
                "cycle_min": round(cycle_mins, 1),
            })

        print(f"[2] {len(rows)} cycles with snapshot data\n")

        if rows:
            print("Per-cycle:")
            print(f"  {'#':<5} {'strat':<14} {'entry':<6} {'MFE%':<7} {'MAE%':<7} {'exit%':<7} {'gave%':<7} {'mfe_min':<8} {'total_min':<8}")
            for r in rows:
                print(
                    f"  {r['buy_id']}-{r['sell_id']:<3} {r['strategy'][:13]:<14} "
                    f"{r['entry']:<6.3f} {r['mfe_pct']:+6.1f}  {r['mae_pct']:+6.1f}  "
                    f"{r['exit_pct']:+6.1f}  {r['gave_back_pct']:5.0f}%  {r['age_to_mfe_min']:6.1f}  {r['cycle_min']:6.1f}"
                )

            print("\n[3] Aggregate stats:")
            mfes = [r["mfe_pct"] for r in rows]
            maes = [r["mae_pct"] for r in rows]
            exits = [r["exit_pct"] for r in rows]
            gave = [r["gave_back_pct"] for r in rows if r["mfe_pct"] > 0]
            print(f"  median MFE:     {statistics.median(mfes):+.1f}%")
            print(f"  median MAE:     {statistics.median(maes):+.1f}%")
            print(f"  median exit:    {statistics.median(exits):+.1f}%")
            if gave:
                print(f"  median gave_back: {statistics.median(gave):.0f}% (of peak)")

            # Lost edge analysis
            lost_edge = [r for r in rows if r["mfe_pct"] >= 12 and r["gave_back_pct"] >= 40]
            print(f"\n[4] Lost-edge cycles (MFE ≥ 12% AND gave back ≥ 40%): {len(lost_edge)}")
            for r in lost_edge:
                pct = r["mfe_pct"] - r["exit_pct"]
                print(f"  cycle #{r['buy_id']}-{r['sell_id']}: peak {r['mfe_pct']:+.1f}% → exit {r['exit_pct']:+.1f}% (lost {pct:.1f}pp)")

            # Test the GPT 25 rule
            print("\n[5] Test [GPT 25] rule (exit at peak retraced 40% if peak ≥ 12%):")
            simulated_pnl_total = 0.0
            real_pnl_total = 0.0
            for r in rows:
                real_pnl_total += r["exit_pct"]
                # Simulated rule: if MFE ≥ 12, exit at 60% of MFE (40% retracement)
                if r["mfe_pct"] >= 12:
                    sim_exit = r["mfe_pct"] * 0.6  # 60% of peak retained
                else:
                    sim_exit = r["exit_pct"]
                simulated_pnl_total += sim_exit
            print(f"  real total exit_pct (sum): {real_pnl_total:+.1f}%")
            print(f"  simulated rule:            {simulated_pnl_total:+.1f}%")
            delta = simulated_pnl_total - real_pnl_total
            print(f"  delta: {delta:+.1f}pp (positive = rule helps)")


if __name__ == "__main__":
    main()
