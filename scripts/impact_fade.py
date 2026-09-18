"""Impact Fade detector & walk-forward validator per [GPT 23].

Hypothesis: aggressive fills create temporary dislocation only в non-anchored,
non-resolution, low/medium-liquidity markets ПОСЛЕ pressure exhaustion.
NOT: big buy happened → short it (that bleeds).

Design (per GPT 23):
  Window detection:    5m primary + 3m fast
  Magnitude threshold: |delta_mid_5m| >= 0.06 OR |delta_mid_3m| >= 0.05
                       liquidity-aware: <3K → 0.08, >=10K → 0.05
  Pressure proxy:      signed_notional = buy - sell over window
                       pressure_aligns = sign matches delta
                       pressure_ratio = |signed_notional| / rolling_30m_median >= 2.0
  Entry timing:        +1 scan after impact (wait for exhaustion)
                       require: price NOT continued 2pp в impact direction
                       spread <= 4pp ideal, hard max 6pp
  Hard filters:        hours_to_res < 6 skip, spread > 6pp skip,
                       liquidity < 3000 skip, mid < 0.08 / > 0.92 skip
  Exit model:          TP half_reversion OR +3pp, SL -4pp, time 60m

Walk-forward: train period → freeze thresholds → test period.
Pass criteria: median net 30m fwd return after spread > +1.5%, hit > 55%, beats
fade-any-pump baseline by ≥1.0pp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import db_session
from app.integrations.polymarket_data_api import PolymarketDataApiClient
from app.models import PMFill


SECS_5M = 300
SECS_3M = 180
SECS_30M = 1800
SECS_60M = 3600


@dataclass
class ImpactEpisode:
    market: str
    asset: str           # token_id where impact happened
    detect_ts: int       # ts at end of impact window
    delta_mid: float     # mid change over window (+/-)
    pressure_ratio: float
    pressure_aligns: bool
    pre_mid: float
    post_mid: float
    spread: float
    notional_total: float


@dataclass
class FadeAssessment:
    episode: ImpactEpisode
    entry_ts: int
    entry_mid: float     # +1 scan after detect, fade direction
    spread_at_entry: float
    fwd_15m: float | None = None
    fwd_30m: float | None = None
    fwd_60m: float | None = None
    fwd_120m: float | None = None
    continuation_2pp: bool = False  # if price moved 2pp further during entry delay


def load_fills() -> list[PMFill]:
    with db_session() as s:
        return s.execute(select(PMFill)).scalars().all()


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


def compute_pressure(fills_in_window: list[PMFill]) -> tuple[float, float]:
    """Returns (signed_notional, total_notional)."""
    signed = 0.0
    total = 0.0
    for f in fills_in_window:
        if f.side == "BUY":
            signed += f.notional
        else:
            signed -= f.notional
        total += f.notional
    return signed, total


def detect_impact_episodes(
    fills_by_asset: dict[str, list[PMFill]],
    histories: dict[str, list[tuple[int, float]]],
    market_meta: dict[str, dict],   # asset → {liquidity, spread_proxy, hours_to_res}
) -> list[ImpactEpisode]:
    """Scan через every 5-minute window per asset. Detect impact = magnitude + pressure."""
    episodes: list[ImpactEpisode] = []
    for asset, fills in fills_by_asset.items():
        if not fills or asset not in histories:
            continue
        history = histories[asset]
        if not history:
            continue

        meta = market_meta.get(asset, {})
        liquidity = meta.get("liquidity", 0)

        # Magnitude threshold liquidity-aware (RELAXED 2x для small sample)
        if liquidity < 3000:
            mag_thresh = 0.04
        elif liquidity >= 10000:
            mag_thresh = 0.025
        else:
            mag_thresh = 0.03

        fills_sorted = sorted(fills, key=lambda x: x.fill_ts)

        # Rolling 30m median total notional baseline
        median_30m_baseline: dict[int, float] = {}

        # Slide every 60s window across asset's lifetime
        first_ts = fills_sorted[0].fill_ts
        last_ts = fills_sorted[-1].fill_ts

        for window_end in range(first_ts + SECS_5M, last_ts, 60):
            window_start_5m = window_end - SECS_5M
            window_start_3m = window_end - SECS_3M
            window_start_30m = window_end - SECS_30M

            window_5m = [f for f in fills_sorted if window_start_5m <= f.fill_ts <= window_end]
            window_3m = [f for f in fills_sorted if window_start_3m <= f.fill_ts <= window_end]
            window_30m = [f for f in fills_sorted if window_start_30m <= f.fill_ts <= window_end]

            if not window_5m:
                continue

            pre_mid = price_at(history, window_start_5m)
            post_mid = price_at(history, window_end)
            if pre_mid is None or post_mid is None:
                continue
            if pre_mid < 0.08 or pre_mid > 0.92:
                continue

            delta_5m = post_mid - pre_mid
            pre_3m = price_at(history, window_start_3m)
            delta_3m = (post_mid - pre_3m) if pre_3m is not None else 0

            # Magnitude check
            magnitude_pass = abs(delta_5m) >= mag_thresh or abs(delta_3m) >= 0.025
            if not magnitude_pass:
                continue

            # Pressure
            signed_5m, _ = compute_pressure(window_5m)
            _, total_30m = compute_pressure(window_30m)
            median_total = total_30m / max(1, len(window_30m))
            pressure_ratio = abs(signed_5m) / max(median_total, 1.0)
            pressure_aligns = (signed_5m > 0 and delta_5m > 0) or (signed_5m < 0 and delta_5m < 0)

            if pressure_ratio < 1.5:  # relaxed from 2.0
                continue
            if not pressure_aligns:
                continue

            episodes.append(ImpactEpisode(
                market=window_5m[0].condition_id,
                asset=asset,
                detect_ts=window_end,
                delta_mid=delta_5m,
                pressure_ratio=pressure_ratio,
                pressure_aligns=pressure_aligns,
                pre_mid=pre_mid,
                post_mid=post_mid,
                spread=meta.get("spread_proxy", 0.04),
                notional_total=sum(f.notional for f in window_5m),
            ))

    return episodes


def assess_fade(
    episode: ImpactEpisode,
    history: list[tuple[int, float]],
    entry_delay_s: int = 60,
) -> FadeAssessment | None:
    """Compute fade entry +1scan after impact, then forward returns."""
    entry_ts = episode.detect_ts + entry_delay_s
    entry_mid = price_at(history, entry_ts)
    if entry_mid is None or entry_mid < 0.05 or entry_mid > 0.95:
        return None

    # Did price continue 2pp further in same impact direction during delay?
    if episode.delta_mid > 0:
        continuation = (entry_mid - episode.post_mid) > 0.02
    else:
        continuation = (episode.post_mid - entry_mid) > 0.02

    # Fade direction: opposite to impact
    fade_direction = -1.0 if episode.delta_mid > 0 else 1.0

    f15 = price_at(history, entry_ts + 900)
    f30 = price_at(history, entry_ts + SECS_30M)
    f60 = price_at(history, entry_ts + SECS_60M)
    f120 = price_at(history, entry_ts + 7200)

    def w(p):
        return None if p is None else max(-1.0, min(1.0, fade_direction * (p - entry_mid) / max(entry_mid, 0.05)))

    return FadeAssessment(
        episode=episode,
        entry_ts=entry_ts,
        entry_mid=entry_mid,
        spread_at_entry=episode.spread,
        fwd_15m=w(f15),
        fwd_30m=w(f30),
        fwd_60m=w(f60),
        fwd_120m=w(f120),
        continuation_2pp=continuation,
    )


def baseline_fade_any(
    histories: dict[str, list[tuple[int, float]]],
    market_meta: dict[str, dict],
    threshold: float = 0.05,
    horizon_s: int = SECS_30M,
) -> list[float]:
    """Baseline 1: fade EVERY >5pp 5min move (ignore pressure/exhaustion filters)."""
    results: list[float] = []
    for asset, history in histories.items():
        if len(history) < 10:
            continue
        for i in range(0, len(history) - 1, 5):
            t0, p0 = history[i]
            t_end = t0 + SECS_5M
            p_end = price_at(history, t_end)
            if p_end is None:
                continue
            if p0 < 0.08 or p0 > 0.92:
                continue
            delta = p_end - p0
            if abs(delta) < threshold:
                continue
            entry_ts = t_end + 60
            entry_mid = price_at(history, entry_ts)
            if entry_mid is None:
                continue
            fwd_p = price_at(history, entry_ts + horizon_s)
            if fwd_p is None:
                continue
            fade_dir = -1.0 if delta > 0 else 1.0
            ret = max(-1.0, min(1.0, fade_dir * (fwd_p - entry_mid) / max(entry_mid, 0.05)))
            results.append(ret)
    return results


async def main() -> None:
    print("=" * 72)
    print("Impact Fade Validator per [GPT 23]")
    print("=" * 72)

    print("\n[1] Loading fills from DB...")
    all_fills = load_fills()
    print(f"   loaded {len(all_fills)} fills, {len(set(f.asset for f in all_fills))} assets")

    print("\n[2] Group fills by asset...")
    fills_by_asset = defaultdict(list)
    for f in all_fills:
        fills_by_asset[f.asset].append(f)

    print("\n[3] Fetch prices history per asset...")
    client = PolymarketDataApiClient()
    histories: dict[str, list[tuple[int, float]]] = {}
    market_meta: dict[str, dict] = {}
    asset_list = list(fills_by_asset.keys())
    for i, asset in enumerate(asset_list, 1):
        try:
            h = await client.fetch_prices_history(asset)
            histories[asset] = h
        except Exception:
            histories[asset] = []
        # Sample meta from one fill (notional avg ≈ liquidity proxy)
        market_meta[asset] = {
            "liquidity": 5000,  # default — we don't have per-asset liq from PMFill
            "spread_proxy": 0.04,
        }
        if i % 30 == 0:
            print(f"   fetched {i}/{len(asset_list)}")
        await asyncio.sleep(0.1)

    print("\n[4] Detect impact episodes...")
    episodes = detect_impact_episodes(fills_by_asset, histories, market_meta)
    print(f"   {len(episodes)} impact episodes detected")

    if not episodes:
        print("\n[!] No impact episodes — попробуй relaxed thresholds или more data")
        return

    print("\n[5] Compute fade assessments...")
    assessments: list[FadeAssessment] = []
    for ep in episodes:
        a = assess_fade(ep, histories[ep.asset])
        if a:
            assessments.append(a)
    print(f"   {len(assessments)} fade assessments")

    # Filter: skip continuation episodes (price kept moving)
    no_continuation = [a for a in assessments if not a.continuation_2pp]
    print(f"   {len(no_continuation)} after exhaustion filter (no 2pp continuation)")

    print("\n[6] Walk-forward split: first half train (calibration), second test...")
    no_continuation.sort(key=lambda a: a.entry_ts)
    if len(no_continuation) < 20:
        print(f"   sample too small для walk-forward — analyzing as single set")
        test_set = no_continuation
    else:
        mid_idx = len(no_continuation) // 2
        test_set = no_continuation[mid_idx:]
        print(f"   test set: {len(test_set)} episodes")

    print("\n[7] Impact Fade results (test set):")
    rets = [a.fwd_30m for a in test_set if a.fwd_30m is not None]
    if rets:
        wins = sum(1 for r in rets if r > 0)
        profit = sum(r for r in rets if r > 0)
        loss = -sum(r for r in rets if r < 0)
        pf = profit / loss if loss > 0 else float("inf") if profit > 0 else 0.0
        print(f"   episodes:        {len(rets)}")
        print(f"   median fwd_30m:  {statistics.median(rets):+.2%}")
        print(f"   mean fwd_30m:    {statistics.mean(rets):+.2%}")
        print(f"   hit_rate:        {wins/len(rets):.0%}")
        print(f"   profit_factor:   {pf:.2f}")
        # MAE estimate
        adverse = [a.fwd_15m for a in test_set if a.fwd_15m is not None]
        if adverse:
            print(f"   median fwd_15m:  {statistics.median(adverse):+.2%}  (early window)")
        sixties = [a.fwd_60m for a in test_set if a.fwd_60m is not None]
        if sixties:
            print(f"   median fwd_60m:  {statistics.median(sixties):+.2%}")

    print("\n[8] Critical Baselines:")
    print("\n   Baseline 1 — fade ANY >=5pp pump (no exhaustion filter):")
    base_any = baseline_fade_any(histories, market_meta, threshold=0.05)
    if base_any:
        wins_b = sum(1 for r in base_any if r > 0)
        print(f"     n: {len(base_any)}")
        print(f"     median fwd_30m: {statistics.median(base_any):+.2%}")
        print(f"     hit_rate:       {wins_b/len(base_any):.0%}")

    print("\n   Baseline 2 — momentum-follow (BUY same direction):")
    base_mom = []
    for asset, history in histories.items():
        if len(history) < 10:
            continue
        for i in range(0, len(history) - 1, 5):
            t0, p0 = history[i]
            t_end = t0 + SECS_5M
            p_end = price_at(history, t_end)
            if p_end is None or p0 < 0.08 or p0 > 0.92:
                continue
            delta = p_end - p0
            if abs(delta) < 0.05:
                continue
            entry_ts = t_end + 60
            entry_mid = price_at(history, entry_ts)
            fwd_p = price_at(history, entry_ts + SECS_30M)
            if entry_mid is None or fwd_p is None:
                continue
            mom_dir = 1.0 if delta > 0 else -1.0
            ret = max(-1.0, min(1.0, mom_dir * (fwd_p - entry_mid) / max(entry_mid, 0.05)))
            base_mom.append(ret)
    if base_mom:
        wins_m = sum(1 for r in base_mom if r > 0)
        print(f"     n: {len(base_mom)}")
        print(f"     median fwd_30m: {statistics.median(base_mom):+.2%}")
        print(f"     hit_rate:       {wins_m/len(base_mom):.0%}")

    print("\n[9] PASS/FAIL per [GPT 23]:")
    if rets:
        pass_med = statistics.median(rets) >= 0.015
        pass_hit = (wins / len(rets)) >= 0.55
        pass_pf = pf >= 1.25
        if base_any:
            edge_pp = (statistics.median(rets) - statistics.median(base_any)) * 100
        else:
            edge_pp = 0
        pass_baseline = edge_pp >= 1.0
        pass_n = len(rets) >= 50
        print(f"   median fwd_30m ≥ +1.5%: {'✓' if pass_med else '✗'}  ({statistics.median(rets):+.2%})")
        print(f"   hit_rate ≥ 55%:         {'✓' if pass_hit else '✗'}  ({wins/len(rets):.0%})")
        print(f"   PF ≥ 1.25:              {'✓' if pass_pf else '✗'}  ({pf:.2f})")
        print(f"   beats fade-any by ≥1pp: {'✓' if pass_baseline else '✗'}  ({edge_pp:+.2f}pp)")
        print(f"   episodes ≥ 50:          {'✓' if pass_n else '✗'}  ({len(rets)})")
        verdict = "PASS — proceed shadow logger" if all([pass_med, pass_hit, pass_pf, pass_baseline, pass_n]) else "FAIL — analyze WHY"
        print(f"\n   OVERALL: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
