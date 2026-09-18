"""Fade-Any Replay Analyzer per [GPT 25] "brutally small report".

Reads /app/data/fade_signals.jsonl, for each signal computes actual forward
return по prices-history fade_token at signal_ts+30m.

Pass criteria per [GPT 25]:
  median fwd_30m > +2pp after spread
  hit_rate >= 53%
  no single market dominates
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.polymarket_data_api import PolymarketDataApiClient


SIGNALS_PATH = Path("/app/data/fade_signals.jsonl")
HORIZONS_S = {"15m": 900, "30m": 1800, "60m": 3600, "120m": 7200}


def load_signals() -> list[dict]:
    if not SIGNALS_PATH.exists():
        return []
    with SIGNALS_PATH.open() as fh:
        return [json.loads(l) for l in fh if l.strip()]


def price_at(history: list[tuple[int, float]], ts: int) -> float | None:
    if not history:
        return None
    if ts <= history[0][0]:
        return history[0][1]
    if ts >= history[-1][0]:
        return history[-1][1]
    lo, hi = 0, len(history) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if history[mid][0] <= ts:
            lo = mid
        else:
            hi = mid
    t0, p0 = history[lo]
    t1, p1 = history[hi]
    if t1 == t0:
        return p0
    return p0 + (p1 - p0) * (ts - t0) / (t1 - t0)


async def replay_signal(client: PolymarketDataApiClient, signal: dict) -> dict:
    """For a signal, compute forward returns at multiple horizons."""
    fade_token = signal["fade_token_id"]
    entry_price = signal["fade_entry_price"]
    signal_ts = signal["ts"]
    now = int(time.time())

    history = []
    try:
        history = await client.fetch_prices_history(fade_token)
    except Exception:
        pass

    out = {**signal}
    for label, horizon_s in HORIZONS_S.items():
        target_ts = signal_ts + horizon_s
        if target_ts > now:
            out[f"fwd_{label}"] = None
            out[f"fwd_{label}_status"] = "future"
            continue
        future_p = price_at(history, target_ts)
        if future_p is None:
            out[f"fwd_{label}"] = None
            out[f"fwd_{label}_status"] = "no_data"
            continue
        ret = (future_p - entry_price) / max(entry_price, 0.01)
        out[f"fwd_{label}"] = round(ret, 4)
        out[f"fwd_{label}_status"] = "ok"
    return out


async def main() -> None:
    print("=" * 76)
    print("Fade-Any Replay Analyzer per [GPT 25]")
    print("=" * 76)

    signals = load_signals()
    print(f"\n[1] Loaded {len(signals)} fade signals from {SIGNALS_PATH}")

    if len(signals) < 10:
        print(f"[!] Sample too small (<10) — wait for accumulation")
        return

    print(f"\n[2] Computing forward returns (15m/30m/60m/120m horizons)...")
    client = PolymarketDataApiClient()
    replayed = []
    for i, s in enumerate(signals, 1):
        try:
            r = await replay_signal(client, s)
            replayed.append(r)
        except Exception as e:
            print(f"  err signal #{i}: {e}")
        if i % 15 == 0:
            print(f"  replayed {i}/{len(signals)}")
        await asyncio.sleep(0.1)

    print(f"\n[3] Stats per horizon (filter status==ok):")
    print(f"  {'horizon':<10} {'n':<6} {'median':<10} {'mean':<10} {'hit%':<6} {'p25':<10} {'p75':<10}")
    by_horizon_stats = {}
    for label in HORIZONS_S:
        rets = [r[f"fwd_{label}"] for r in replayed if r.get(f"fwd_{label}_status") == "ok" and r.get(f"fwd_{label}") is not None]
        if not rets:
            print(f"  {label:<10} 0      n/a")
            continue
        wins = sum(1 for r in rets if r > 0)
        rets_sorted = sorted(rets)
        p25 = rets_sorted[int(len(rets_sorted) * 0.25)]
        p75 = rets_sorted[int(len(rets_sorted) * 0.75)]
        med = statistics.median(rets)
        mean = statistics.mean(rets)
        hit_rate = wins / len(rets)
        by_horizon_stats[label] = {
            "n": len(rets), "median": med, "mean": mean, "hit_rate": hit_rate,
        }
        print(f"  {label:<10} {len(rets):<6} {med:+8.2%}  {mean:+8.2%}  {hit_rate:.0%}    {p25:+7.2%}  {p75:+7.2%}")

    print(f"\n[4] Concentration check:")
    market_counts = Counter(r["market_id"] for r in replayed if r.get("fwd_30m") is not None)
    if market_counts:
        top_market, top_count = market_counts.most_common(1)[0]
        total = sum(market_counts.values())
        print(f"  Top market dominance: {top_count}/{total} = {top_count/total:.0%}")
        print(f"  Top market ID: {top_market[:18]}")
        if top_count / total > 0.40:
            print("  [!] CONCENTRATION RISK: 1 market > 40% of signals")

    print(f"\n[5] PASS/FAIL per [GPT 25]:")
    if "30m" in by_horizon_stats:
        s = by_horizon_stats["30m"]
        pass_med = s["median"] >= 0.02
        pass_hit = s["hit_rate"] >= 0.53
        pass_n = s["n"] >= 30
        print(f"  median fwd_30m ≥ +2.0%: {'✓' if pass_med else '✗'}  ({s['median']:+.2%})")
        print(f"  hit_rate ≥ 53%:         {'✓' if pass_hit else '✗'}  ({s['hit_rate']:.0%})")
        print(f"  episodes ≥ 30:          {'✓' if pass_n else '✗'}  ({s['n']})")
        verdict = "PASS — proceed live canary $2 stake" if all([pass_med, pass_hit, pass_n]) else "FAIL — analyze WHY or wait"
        print(f"\n  OVERALL: {verdict}")

    print(f"\n[6] Segmentation by external_confirmation (per [GPT 25]):")
    ec_buckets = {"no_match": [], "agrees": [], "diverges": []}
    for r in replayed:
        ec = r.get("external_confirmation")
        fwd = r.get("fwd_30m")
        if ec in ec_buckets and fwd is not None and r.get("fwd_30m_status") == "ok":
            ec_buckets[ec].append(fwd)

    print(f"  {'segment':<14} {'n':<5} {'median':<10} {'hit%':<6} {'p25':<10} {'p75':<10}")
    for label, rets in ec_buckets.items():
        if not rets:
            print(f"  {label:<14} 0     n/a")
            continue
        wins = sum(1 for r in rets if r > 0)
        rs = sorted(rets)
        p25 = rs[int(len(rs) * 0.25)] if len(rs) >= 4 else rs[0]
        p75 = rs[int(len(rs) * 0.75)] if len(rs) >= 4 else rs[-1]
        print(f"  {label:<14} {len(rets):<5} {statistics.median(rets):+8.2%}  {wins/len(rets):.0%}    {p25:+7.2%}  {p75:+7.2%}")

    print(f"\n[7] Pump magnitude segmentation:")
    mag_buckets = {"5-6pp": [], "6-10pp": [], "10pp+": []}
    for r in replayed:
        delta = abs(r.get("delta_5m", 0))
        fwd = r.get("fwd_30m")
        if fwd is None or r.get("fwd_30m_status") != "ok":
            continue
        if delta < 0.06:
            mag_buckets["5-6pp"].append(fwd)
        elif delta < 0.10:
            mag_buckets["6-10pp"].append(fwd)
        else:
            mag_buckets["10pp+"].append(fwd)

    print(f"  {'magnitude':<12} {'n':<5} {'median':<10} {'hit%':<6}")
    for label, rets in mag_buckets.items():
        if not rets:
            print(f"  {label:<12} 0     n/a")
            continue
        wins = sum(1 for r in rets if r > 0)
        print(f"  {label:<12} {len(rets):<5} {statistics.median(rets):+8.2%}  {wins/len(rets):.0%}")


if __name__ == "__main__":
    asyncio.run(main())
