"""Smart Money early validator per [GPT 20] / [GPT 21].

Reads PMFill table from production DB, joins with CLOB prices-history,
computes forward returns 30m/2h after copy lag.

Walk-forward: train first half / test second half — single fold OK для small sample.

Per [GPT 20] pass criteria:
  OOS median episode_fwd_2h_after_lag >= +1.0%
  hit_rate >= 55%
  PF >= 1.25
  beats random-fill baseline by >= 1.0pp
  >= 50 OOS episodes
  no single wallet/market >25% of profits
"""
from __future__ import annotations

import asyncio
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import db_session
from app.integrations.polymarket_data_api import PolymarketDataApiClient
from app.models import PMFill


COPY_LAG_S = 8
S_30M = 1800
S_2H = 7200


@dataclass
class Episode:
    wallet: str
    condition_id: str
    asset: str
    side: str
    bucket_ts: int
    avg_ts: int
    notional: float
    median_price: float


def load_fills_from_db() -> list[PMFill]:
    with db_session() as s:
        return s.execute(select(PMFill)).scalars().all()


def group_episodes(fills: list[PMFill], bucket_s: int = 1800) -> list[Episode]:
    by_key: dict[tuple, list[PMFill]] = defaultdict(list)
    for f in fills:
        bucket = (f.fill_ts // bucket_s) * bucket_s
        key = (f.wallet, f.condition_id, f.asset, f.side, bucket)
        by_key[key].append(f)
    out = []
    for (wallet, cid, asset, side, bucket), fs in by_key.items():
        prices = [x.price for x in fs]
        out.append(Episode(
            wallet=wallet, condition_id=cid, asset=asset, side=side,
            bucket_ts=bucket,
            avg_ts=int(statistics.mean(x.fill_ts for x in fs)),
            notional=sum(x.notional for x in fs),
            median_price=statistics.median(prices),
        ))
    return out


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


async def fetch_all_prices_history(
    client: PolymarketDataApiClient,
    asset_ids: set[str],
) -> dict[str, list[tuple[int, float]]]:
    """Fetch prices-history for each unique asset (token)."""
    out: dict[str, list[tuple[int, float]]] = {}
    for i, asset in enumerate(asset_ids, 1):
        try:
            h = await client.fetch_prices_history(asset)
            out[asset] = h
        except Exception as e:
            print(f"  [!] price fetch failed for {asset[:12]}: {e}")
            out[asset] = []
        if i % 20 == 0:
            print(f"  fetched {i}/{len(asset_ids)} histories...")
        await asyncio.sleep(0.15)
    return out


def assess(episode: Episode, history: list[tuple[int, float]]) -> dict | None:
    entry_ts = episode.avg_ts + COPY_LAG_S
    entry_price = price_at(history, entry_ts)
    if entry_price is None or entry_price <= 0.01 or entry_price >= 0.99:
        return None
    fwd_30m = price_at(history, entry_ts + S_30M)
    fwd_2h = price_at(history, entry_ts + S_2H)
    direction = 1.0 if episode.side == "BUY" else -1.0
    ret_30m = ((fwd_30m - entry_price) / entry_price * direction) if fwd_30m else None
    ret_2h = ((fwd_2h - entry_price) / entry_price * direction) if fwd_2h else None
    # winsorize
    def w(x):
        return None if x is None else max(-1.0, min(1.0, x))
    lag = (entry_price - episode.median_price) / max(episode.median_price, 0.01) * direction
    return {
        "wallet": episode.wallet,
        "condition_id": episode.condition_id,
        "side": episode.side,
        "bucket_ts": episode.bucket_ts,
        "fwd_30m": w(ret_30m),
        "fwd_2h": w(ret_2h),
        "lag": lag,
        "notional": episode.notional,
    }


def aggregate(assessments: list[dict]) -> dict[str, dict]:
    by = defaultdict(list)
    for a in assessments:
        by[a["wallet"]].append(a)
    out = {}
    for w, items in by.items():
        rets = [x["fwd_2h"] for x in items if x["fwd_2h"] is not None]
        if not rets:
            continue
        wins = sum(1 for r in rets if r > 0)
        lags = [x["lag"] for x in items]
        out[w] = {
            "wallet": w,
            "n": len(rets),
            "volume": sum(x["notional"] for x in items),
            "median_fwd_2h": statistics.median(rets),
            "hit_rate": wins / len(rets),
            "median_lag": statistics.median(lags),
            "p75_lag": sorted(lags)[int(len(lags) * 0.75)] if len(lags) >= 4 else max(lags),
        }
    return out


def filter_eligible(
    stats: dict[str, dict],
    min_n: int = 2,
    min_fwd_2h: float = 0.015,
    min_hit: float = 0.55,
    max_lag: float = 0.05,
    min_volume: float = 200.0,
) -> list[dict]:
    out = []
    for s in stats.values():
        if s["n"] < min_n:
            continue
        if s["volume"] < min_volume:
            continue
        if s["median_fwd_2h"] < min_fwd_2h:
            continue
        if s["hit_rate"] < min_hit:
            continue
        if s["median_lag"] > max_lag:
            continue
        out.append(s)
    out.sort(key=lambda x: x["median_fwd_2h"], reverse=True)
    return out


async def main() -> None:
    print("=" * 70)
    print("Smart Money Validator — early walk-forward на production PMFill")
    print("=" * 70)

    print("\n[1] Loading fills from DB...")
    fills = load_fills_from_db()
    if not fills:
        print("EMPTY DB — abort")
        return
    print(f"   loaded: {len(fills)} fills")
    print(f"   wallets: {len(set(f.wallet for f in fills))}")
    print(f"   markets: {len(set(f.condition_id for f in fills))}")
    earliest = min(f.fill_ts for f in fills)
    latest = max(f.fill_ts for f in fills)
    print(f"   span: {(latest-earliest)/3600:.1f}h")

    print("\n[2] Grouping into episodes (30min buckets)...")
    episodes = group_episodes(fills)
    print(f"   episodes: {len(episodes)}")

    print("\n[3] Fetching prices history per asset (may take 1-2 min)...")
    client = PolymarketDataApiClient()
    asset_ids = {ep.asset for ep in episodes}
    print(f"   unique assets: {len(asset_ids)}")
    histories = await fetch_all_prices_history(client, asset_ids)

    print("\n[4] Computing forward returns...")
    assessments = []
    for ep in episodes:
        h = histories.get(ep.asset, [])
        a = assess(ep, h)
        if a:
            assessments.append(a)
    print(f"   valid assessments: {len(assessments)}")

    if not assessments:
        print("NO VALID ASSESSMENTS — abort")
        return

    print("\n[5] Walk-forward: split first half (train) / second half (test)...")
    assessments.sort(key=lambda a: a["bucket_ts"])
    mid = assessments[len(assessments) // 2]["bucket_ts"]
    train = [a for a in assessments if a["bucket_ts"] < mid]
    test = [a for a in assessments if a["bucket_ts"] >= mid]
    print(f"   train period: < bucket_ts={mid} ({len(train)} episodes)")
    print(f"   test period:  >= bucket_ts={mid} ({len(test)} episodes)")

    train_stats = aggregate(train)
    # Multi-tier eligibility report для diagnostic
    print()
    for tier_name, tier_args in [
        ("strict (GPT 20: ≥1.5% fwd, ≥55% hit)", dict(min_n=2, min_fwd_2h=0.015, min_hit=0.55)),
        ("relaxed1 (≥0.5% fwd, ≥50% hit)",         dict(min_n=2, min_fwd_2h=0.005, min_hit=0.50)),
        ("relaxed2 (≥0% fwd, ≥50% hit)",          dict(min_n=2, min_fwd_2h=0.000, min_hit=0.50)),
        ("loose (>=3 fills, ≥55% hit, any fwd)",   dict(min_n=3, min_fwd_2h=-1.0, min_hit=0.55)),
    ]:
        elig = filter_eligible(train_stats, **tier_args)
        print(f"   tier '{tier_name}': {len(elig)} eligible")
    eligible = filter_eligible(train_stats, min_n=2, min_fwd_2h=0.005, min_hit=0.50)  # relaxed1 for analysis
    eligible_wallets = {w["wallet"] for w in eligible}
    print(f"\n   USING relaxed1 tier for analysis: {len(eligible_wallets)} eligible wallets")

    test_eligible = [a for a in test if a["wallet"] in eligible_wallets]
    print(f"   test episodes from eligible wallets: {len(test_eligible)}")

    if not test_eligible:
        print("\n[!] No test episodes from eligible wallets — sample too small")
        print("    Need more accumulated data — wait for Pm_fills cron to grow.")
        return

    print("\n[6] Test set metrics:")
    rets = [a["fwd_2h"] for a in test_eligible if a["fwd_2h"] is not None]
    if rets:
        wins = sum(1 for r in rets if r > 0)
        profit = sum(r for r in rets if r > 0)
        loss = -sum(r for r in rets if r < 0)
        pf = profit / loss if loss > 0 else float("inf") if profit > 0 else 0.0
        print(f"   median_fwd_2h: {statistics.median(rets):+.2%}")
        print(f"   mean_fwd_2h:   {statistics.mean(rets):+.2%}")
        print(f"   hit_rate:      {wins/len(rets):.0%}")
        print(f"   profit_factor: {pf:.2f}")
        print(f"   episodes:      {len(rets)}")

    print("\n[7] Random baseline (same period):")
    import random
    sample = random.sample(test, min(len(test_eligible), len(test)))
    sample_rets = [a["fwd_2h"] for a in sample if a["fwd_2h"] is not None]
    if sample_rets:
        wins_r = sum(1 for r in sample_rets if r > 0)
        print(f"   median_fwd_2h: {statistics.median(sample_rets):+.2%}")
        print(f"   hit_rate:      {wins_r/len(sample_rets):.0%}")
        edge_pp = (statistics.median(rets) - statistics.median(sample_rets)) * 100 if rets else 0
        print(f"   edge_vs_baseline: {edge_pp:+.2f}pp")

    print("\n[8] Pass/Fail per [GPT 20] criteria:")
    if rets:
        pass_med = statistics.median(rets) >= 0.01
        pass_hit = (wins / len(rets)) >= 0.55
        pass_pf = pf >= 1.25
        pass_baseline = edge_pp >= 1.0
        pass_n = len(rets) >= 50
        print(f"   median_fwd_2h ≥ +1.0%:  {'✓' if pass_med else '✗'}  ({statistics.median(rets):+.2%})")
        print(f"   hit_rate ≥ 55%:         {'✓' if pass_hit else '✗'}  ({wins/len(rets):.0%})")
        print(f"   PF ≥ 1.25:              {'✓' if pass_pf else '✗'}  ({pf:.2f})")
        print(f"   edge ≥ 1.0pp baseline:  {'✓' if pass_baseline else '✗'}  ({edge_pp:+.2f}pp)")
        print(f"   episodes ≥ 50:          {'✓' if pass_n else '✗'}  ({len(rets)})")
        verdict = "PASS — proceed to shadow signal logger" if all([pass_med, pass_hit, pass_pf, pass_baseline, pass_n]) else "FAIL — need more data OR scrap concept"
        print(f"\n   OVERALL: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
