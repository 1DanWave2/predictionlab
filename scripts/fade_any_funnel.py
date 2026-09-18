"""Fade-Any Funnel Analyzer per [GPT 37] — diagnose why 0 trades since deploy.

Read-only. Classifies the canary state into exactly one of:
  - NO_SIGNALS         (scanner never produced a fade signal)
  - FILTERS_TOO_STRICT (signals existed but never reached risk_manager)
  - RISK_MANAGER_BLOCK (risk_manager rejected — show reason breakdown)
  - EXECUTION_DISABLED (orders attempted but execution path broken)
  - BUG_PIPELINE_BREAK (signals exist + risk passes but no orders)

Sources:
  /app/data/fade_signals.jsonl        — fade_shadow_scanner output
  /app/data/funnel.jsonl              — execution router stages
  /app/db/paper_orders / positions    — final orders/positions

Output: stdout + /app/data/fade_any_funnel.jsonl

Usage: docker exec polymarket-bot python3 -m scripts.fade_any_funnel
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path("/app/data")
DB = DATA_DIR / "paper_bot.db"
FADE_SIGNALS = DATA_DIR / "fade_signals.jsonl"
FUNNEL = DATA_DIR / "funnel.jsonl"
OUTPUT = DATA_DIR / "fade_any_funnel.jsonl"

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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    print(f"\n{'═' * 75}")
    print(f"  FADE_ANY FUNNEL ANALYSIS  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    print(f"  Per [GPT 37] — diagnose 0 trades since deploy. Read-only.")
    print(f"{'═' * 75}")

    # ─── Stage 1: scanner output (fade_shadow_scanner writes signals) ───
    section("Stage 1. Scanner — fade_signals.jsonl")
    sigs = load_jsonl(FADE_SIGNALS)
    print(f"  Total scanner records:        {len(sigs)}")
    if not sigs:
        print(f"  → NO_SIGNALS — scanner never produced anything")
        return
    # Real fade scanner uses `delta_5m` (signed) as magnitude field, not `magnitude`.
    real = [s for s in sigs if s.get("delta_5m") is not None]
    print(f"  With delta_5m field:          {len(real)}")
    # Magnitude distribution (abs value)
    mag_buckets = Counter()
    for s in real:
        m = abs(s.get("delta_5m", 0) or 0)
        if m < 0.04:
            mag_buckets["<4pp"] += 1
        elif m < 0.06:
            mag_buckets["4-6pp"] += 1
        elif m < 0.10:
            mag_buckets["6-10pp_sweet"] += 1
        elif m < 0.15:
            mag_buckets["10-15pp"] += 1
        else:
            mag_buckets["15pp+"] += 1
    for k, v in mag_buckets.most_common():
        print(f"    {k:<18} {v}")

    sweet_count = mag_buckets.get("6-10pp_sweet", 0)
    print(f"\n  Signals in 6-10pp sweet band: {sweet_count}")
    if sweet_count == 0:
        print(f"  → NO_SIGNALS in target band — fade_any deployed but market quiet")
        if args.save:
            _save_summary("NO_SIGNALS", {"sweet_count": 0, "total_scanner": len(sigs)})
        return

    # ─── Stage 2: funnel.jsonl — entry attempts by fade_any strategy ───
    section("Stage 2. Funnel — execution router stages")
    funnel = load_jsonl(FUNNEL)
    fade_funnel = [f for f in funnel if f.get("strategy") == "fade_any" or f.get("bucket") == "fade_any_canary"]
    print(f"  Total funnel records (all):     {len(funnel)}")
    print(f"  fade_any-tagged records:        {len(fade_funnel)}")

    if not fade_funnel:
        print(f"  ⚠️  Sweet-band signals exist ({sweet_count}) but ZERO funnel records for fade_any.")
        print(f"  → FILTERS_TOO_STRICT — signals never reached the entry pipeline")
        print(f"     (likely stuck in evaluate() filters: hours/liquidity/spread/mid range)")
        if args.save:
            _save_summary("FILTERS_TOO_STRICT", {
                "sweet_count": sweet_count,
                "fade_funnel_records": 0,
            })
        return

    stage_counts = Counter(f.get("stage", "?") for f in fade_funnel)
    print(f"\n  fade_any funnel by stage:")
    for stage, n in stage_counts.most_common():
        print(f"    {stage:<25} {n}")

    # ─── Stage 3: risk_rejected reasons ───
    section("Stage 3. Risk rejection breakdown")
    rejected = [f for f in fade_funnel if f.get("stage") == "risk_rejected"]
    print(f"  Total risk_rejected fade_any:   {len(rejected)}")
    if rejected:
        reason_counts = Counter()
        for r in rejected:
            reason = (r.get("reason") or "").strip()
            # Normalize: take first word/prefix for grouping
            if "matchup" in reason:
                key = "matchup_blocked"
            elif "kill_switch" in reason:
                key = "kill_switch"
            elif "setup_cooldown" in reason:
                key = "setup_cooldown"
            elif "cluster" in reason:
                key = "cluster_open"
            elif "DCA" in reason or "dca" in reason:
                key = "no_dca"
            elif "size_too_large" in reason:
                key = "size_too_large"
            elif "daily_stop" in reason:
                key = "daily_stop"
            elif "telemetry_disabled" in reason:
                key = "telemetry_auto_disable"
            elif "stop-loss" in reason or "sl_cooldown" in reason:
                key = "sl_cooldown"
            elif "profit_lock" in reason:
                key = "profit_lock"
            elif "high_gamma" in reason:
                key = "risk_score"
            elif "live trading is hard blocked" in reason:
                key = "live_hardblock"
            else:
                key = reason[:40] or "unknown"
            reason_counts[key] += 1
        print(f"\n  Rejection reasons (top 10):")
        for reason, n in reason_counts.most_common(10):
            print(f"    {reason:<30} {n}")

    # ─── Stage 4: orders + positions ───
    section("Stage 4. Paper orders + positions for fade_any_canary bucket")
    if DB.exists():
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        try:
            n_orders = c.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE bucket = 'fade_any_canary' OR strategy = 'fade_any'"
            ).fetchone()[0]
        except Exception:
            n_orders = c.execute(
                "SELECT COUNT(*) FROM paper_orders WHERE strategy = 'fade_any'"
            ).fetchone()[0]
        print(f"  Paper orders for fade_any:     {n_orders}")
        try:
            pos_rows = c.execute(
                "SELECT bucket, COUNT(*), SUM(realized_pnl), SUM(unrealized_pnl) "
                "FROM positions WHERE bucket = 'fade_any_canary' GROUP BY bucket"
            ).fetchall()
        except Exception:
            pos_rows = []
        if pos_rows:
            for b, n, rp, urp in pos_rows:
                print(f"    bucket={b}  n={n}  realized=${rp or 0:.2f}  unrealized=${urp or 0:.2f}")
        else:
            print(f"  No positions in bucket fade_any_canary")
        conn.close()

    # ─── Stage 5: classify ───
    section("Stage 5. Classification")
    n_filled = stage_counts.get("order_filled", 0)
    n_rejected = stage_counts.get("risk_rejected", 0)
    n_signals_router = stage_counts.get("signal_generated", 0)

    if n_filled > 0:
        verdict = "HEALTHY"
        details = f"fade_any has filled {n_filled} orders — pipeline working"
    elif n_rejected > 0 and n_signals_router > 0:
        verdict = "RISK_MANAGER_BLOCK"
        # Top rejection reason
        if rejected:
            top_reason, top_n = reason_counts.most_common(1)[0]
            details = f"signals reach router ({n_signals_router}), all blocked by risk_manager. Top reason: {top_reason} ({top_n})"
        else:
            details = "rejected without reason captured"
    elif n_signals_router > 0:
        verdict = "EXECUTION_DISABLED"
        details = f"signals reach router ({n_signals_router}) but never get evaluated by risk_manager"
    elif sweet_count > 0:
        verdict = "FILTERS_TOO_STRICT"
        details = f"sweet-band signals exist ({sweet_count}) but never reach the router"
    else:
        verdict = "NO_SIGNALS"
        details = "no sweet-band signals from scanner"

    print(f"\n  VERDICT: {verdict}")
    print(f"  {details}")

    if args.save:
        _save_summary(verdict, {
            "scanner_sweet_count": sweet_count,
            "router_signals": n_signals_router,
            "risk_rejected": n_rejected,
            "order_filled": n_filled,
            "top_rejection_reason": (
                reason_counts.most_common(1)[0] if rejected else None
            ),
        })


def _save_summary(verdict: str, data: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "fade_any_funnel",
            "verdict": verdict,
            **data,
        }) + "\n")
    print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
