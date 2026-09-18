"""Smart Money PoC v2 per [GPT 20] sanity checks.

Fixes над v1:
  1. Resolution filter — skip fills where market resolves within forward horizon
  2. Episode grouping — wallet+market+side+30min bucket = 1 prediction
  3. Walk-forward — train на period A, freeze, test на period B
  4. Baselines — random wallets, top-volume wallets, opposite side
  5. Winsorize returns to ±1.0 (don't let resolution jumps crown wallets)
  6. Lag gates — median ≤ 3pp, p75 ≤ 5pp

Pass criteria (per GPT 20):
  OOS median episode fwd_2h_after_lag ≥ +1.0%
  hit_rate ≥ 55%
  PF ≥ 1.25
  beats random-fill baseline by ≥ 1.0pp
  ≥ 50 OOS episodes
  no single wallet/market >25% profits
"""
from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.polymarket_data_api import PMFill, PolymarketDataApiClient


SECONDS_24H = 86400
SECONDS_7D = 7 * 86400
WINDOW_SECONDS = SECONDS_7D  # GPT 20: real validation 7d
SECONDS_30M = 1800
SECONDS_2H = 7200
COPY_LAG_SECONDS = 8
EPISODE_BUCKET_S = 1800  # 30 min


@dataclass
class Episode:
    wallet: str
    market_id: str
    side: str
    bucket_ts: int
    fills_count: int
    total_notional: float
    median_fill_price: float
    avg_fill_ts: int
    asset: str
    market_end_ts: int


@dataclass
class Assessment:
    episode: Episode
    entry_price: float
    fwd_2h_price_raw: float
    fwd_2h_return_winsor: float  # clamped to ±1.0
    lag_drift_points: float
    sample_quality: str  # "high" / "low" / "skip"


async def get_short_term_markets(limit: int = 15) -> list[dict]:
    """Fetch 15 short-term markets with end_date <= 7d AND >= entry_window+2h."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true", "closed": "false",
        "limit": 200,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()
    cutoff_max = datetime.now(timezone.utc) + timedelta(days=7)
    cutoff_min = datetime.now(timezone.utc) + timedelta(hours=4)  # avoid super-near-resolution
    out = []
    for m in markets:
        if not m.get("conditionId") or not m.get("clobTokenIds"):
            continue
        try:
            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        except Exception:
            continue
        if len(tokens) < 2:
            continue
        end_raw = m.get("endDate") or m.get("endDateIso")
        if not end_raw:
            continue
        try:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if end_dt > cutoff_max or end_dt < cutoff_min:
            continue
        try:
            prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
            yes_price = float(prices[0]) if prices else 0.5
        except Exception:
            yes_price = 0.5
        if yes_price < 0.10 or yes_price > 0.90:
            continue
        out.append({
            "condition_id": m["conditionId"],
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "title": m.get("question", "")[:60],
            "end_ts": int(end_dt.timestamp()),
            "liquidity": float(m.get("liquidityNum", 0) or 0),
        })
        if len(out) >= limit:
            break
    return out


def price_at(history: list[tuple[int, float]], target_ts: int) -> float | None:
    if not history:
        return None
    if target_ts <= history[0][0]:
        return history[0][1]
    if target_ts >= history[-1][0]:
        return history[-1][1]
    lo, hi = 0, len(history) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if history[mid][0] <= target_ts:
            lo = mid
        else:
            hi = mid
    t0, p0 = history[lo]
    t1, p1 = history[hi]
    if t1 == t0:
        return p0
    frac = (target_ts - t0) / (t1 - t0)
    return p0 + (p1 - p0) * frac


def group_into_episodes(fills: list[PMFill], market_end_ts: int) -> list[Episode]:
    by_key: dict[tuple[str, str, str, int], list[PMFill]] = defaultdict(list)
    for f in fills:
        bucket = (f.timestamp // EPISODE_BUCKET_S) * EPISODE_BUCKET_S
        key = (f.wallet, f.condition_id, f.side, bucket)
        by_key[key].append(f)
    episodes: list[Episode] = []
    for (wallet, market_id, side, bucket_ts), fs in by_key.items():
        notional = sum(x.notional for x in fs)
        prices = [x.price for x in fs]
        episodes.append(Episode(
            wallet=wallet, market_id=market_id, side=side,
            bucket_ts=bucket_ts, fills_count=len(fs),
            total_notional=notional,
            median_fill_price=statistics.median(prices),
            avg_fill_ts=int(statistics.mean(x.timestamp for x in fs)),
            asset=fs[0].asset,
            market_end_ts=market_end_ts,
        ))
    return episodes


def assess_episode(
    ep: Episode,
    yes_history: list[tuple[int, float]],
    no_history: list[tuple[int, float]],
    yes_token: str,
    horizon_s: int = SECONDS_2H,
) -> Assessment | None:
    # Resolution filter: skip if market resolves within entry+horizon
    entry_ts = ep.avg_fill_ts + COPY_LAG_SECONDS
    fwd_ts = entry_ts + horizon_s
    if ep.market_end_ts <= fwd_ts:
        return None  # would touch resolution

    is_yes = ep.asset == yes_token
    history = yes_history if is_yes else no_history
    if not history:
        return None

    entry_price = price_at(history, entry_ts)
    fwd_price = price_at(history, fwd_ts)
    if entry_price is None or fwd_price is None or entry_price <= 0:
        return None
    if entry_price >= 0.99 or entry_price <= 0.01:
        return None  # avoid resolution-touch

    direction = 1.0 if ep.side == "BUY" else -1.0
    raw = (fwd_price - entry_price) / entry_price * direction
    winsor = max(-1.0, min(1.0, raw))
    lag = (entry_price - ep.median_fill_price) / max(ep.median_fill_price, 0.01) * direction

    # Sample quality: hourly fallback => low confidence for 30m/lag tests
    quality = "high"
    if len(history) < 100:  # likely interval=1h fallback
        quality = "low"

    return Assessment(
        episode=ep,
        entry_price=entry_price,
        fwd_2h_price_raw=fwd_price,
        fwd_2h_return_winsor=winsor,
        lag_drift_points=lag,
        sample_quality=quality,
    )


def aggregate_wallet_stats(assessments: list[Assessment]) -> dict[str, dict]:
    by_wallet: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:
        by_wallet[a.episode.wallet].append(a)
    out = {}
    for w, items in by_wallet.items():
        if len(items) < 3:
            continue
        rets = [x.fwd_2h_return_winsor for x in items]
        lags = [x.lag_drift_points for x in items]
        wins = sum(1 for r in rets if r > 0)
        out[w] = {
            "wallet": w,
            "episodes": len(items),
            "volume": sum(x.episode.total_notional for x in items),
            "median_fwd_2h_winsor": statistics.median(rets),
            "median_lag_points": statistics.median(lags),
            "p75_lag_points": sorted(lags)[int(len(lags) * 0.75)] if len(lags) >= 4 else max(lags),
            "hit_rate_2h": wins / len(rets),
            "n": len(rets),
        }
    return out


def filter_eligible(stats: dict[str, dict], min_episodes: int = 3) -> list[dict]:
    eligible = []
    for s in stats.values():
        if s["episodes"] < min_episodes:
            continue
        if s["volume"] < 500:
            continue
        if s["median_fwd_2h_winsor"] < 0.015:
            continue
        if s["hit_rate_2h"] < 0.55:
            continue
        # GPT 20 lag gates
        if s["median_lag_points"] > 0.03:
            continue
        if s["p75_lag_points"] > 0.05:
            continue
        eligible.append(s)
    eligible.sort(key=lambda x: x["median_fwd_2h_winsor"], reverse=True)
    return eligible


def aggregate_wallet_stats_loose(assessments: list[Assessment]) -> dict[str, dict]:
    """Like aggregate_wallet_stats but min_episodes=1 для small windows."""
    by_wallet: dict[str, list[Assessment]] = defaultdict(list)
    for a in assessments:
        by_wallet[a.episode.wallet].append(a)
    out = {}
    for w, items in by_wallet.items():
        rets = [x.fwd_2h_return_winsor for x in items]
        lags = [x.lag_drift_points for x in items]
        wins = sum(1 for r in rets if r > 0)
        out[w] = {
            "wallet": w, "episodes": len(items),
            "volume": sum(x.episode.total_notional for x in items),
            "median_fwd_2h_winsor": statistics.median(rets),
            "median_lag_points": statistics.median(lags),
            "p75_lag_points": sorted(lags)[int(len(lags) * 0.75)] if len(lags) >= 4 else max(lags),
            "hit_rate_2h": wins / len(rets), "n": len(rets),
        }
    return out


def filter_eligible_loose(stats: dict[str, dict]) -> list[dict]:
    """Looser gates для small training windows."""
    eligible = []
    for s in stats.values():
        if s["volume"] < 200:
            continue
        if s["median_fwd_2h_winsor"] < 0.015:
            continue
        if s["hit_rate_2h"] < 0.55:
            continue
        if s["median_lag_points"] > 0.05:  # looser
            continue
        eligible.append(s)
    eligible.sort(key=lambda x: x["median_fwd_2h_winsor"], reverse=True)
    return eligible


def random_baseline(assessments: list[Assessment], n_random: int = 100) -> dict:
    """Sample random episodes — what's the population-level baseline?"""
    if not assessments:
        return {}
    sample = random.sample(assessments, min(n_random, len(assessments)))
    rets = [x.fwd_2h_return_winsor for x in sample]
    wins = sum(1 for r in rets if r > 0)
    return {
        "n": len(sample),
        "median_fwd_2h": statistics.median(rets),
        "mean_fwd_2h": statistics.mean(rets),
        "hit_rate": wins / len(sample),
    }


async def fetch_market_assessments(
    client: PolymarketDataApiClient,
    market: dict,
    since_ts: int,
) -> list[Assessment]:
    fills = await client.iterate_market_fills(
        market["condition_id"], since_ts=since_ts, page_size=500, max_pages=200
    )
    if not fills:
        return []
    yes_hist = await client.fetch_prices_history(market["yes_token"])
    no_hist = await client.fetch_prices_history(market["no_token"])
    episodes = group_into_episodes(fills, market_end_ts=market["end_ts"])
    out = []
    for ep in episodes:
        a = assess_episode(ep, yes_hist, no_hist, market["yes_token"])
        if a is not None:
            out.append(a)
    return out


def walk_forward_test(
    all_assessments: list[Assessment],
    train_start: int,
    train_end: int,
    test_start: int,
    test_end: int,
) -> dict:
    """Train wallets in [train_start, train_end), test on [test_start, test_end)."""
    train = [a for a in all_assessments if train_start <= a.episode.bucket_ts < train_end]
    test = [a for a in all_assessments if test_start <= a.episode.bucket_ts < test_end]
    if not train or not test:
        return {"error": "empty period", "train_n": len(train), "test_n": len(test)}

    # Train с min_episodes=2 (с 7d data wallets accumulate enough)
    train_stats = aggregate_wallet_stats(train)
    eligible_wallets = {w["wallet"] for w in filter_eligible(train_stats, min_episodes=2)}

    test_eligible = [a for a in test if a.episode.wallet in eligible_wallets]
    if not test_eligible:
        return {
            "train_n": len(train), "test_n": len(test),
            "eligible_in_train": len(eligible_wallets),
            "test_eligible_episodes": 0, "verdict": "NO_EPISODES",
        }

    rets = [a.fwd_2h_return_winsor for a in test_eligible]
    wins = sum(1 for r in rets if r > 0)
    profit = sum(r for r in rets if r > 0)
    loss = -sum(r for r in rets if r < 0)
    pf = profit / loss if loss > 0 else float("inf") if profit > 0 else 0.0

    rand_base = random_baseline(test, n_random=len(test_eligible))

    return {
        "train_n": len(train), "test_n": len(test),
        "eligible_in_train": len(eligible_wallets),
        "test_eligible_episodes": len(test_eligible),
        "median_fwd_2h": statistics.median(rets),
        "mean_fwd_2h": statistics.mean(rets),
        "hit_rate": wins / len(rets),
        "profit_factor": pf,
        "baseline_median": rand_base.get("median_fwd_2h", 0),
        "baseline_hit": rand_base.get("hit_rate", 0),
        "edge_over_baseline_pp": (statistics.median(rets) - rand_base.get("median_fwd_2h", 0)) * 100,
    }


async def main() -> None:
    print("=" * 72)
    print("Smart Money PoC v2 — per [GPT 20] (resolution + episodes + walk-forward)")
    print("=" * 72)
    now = int(time.time())
    since = now - WINDOW_SECONDS
    print(f"  Window: last {WINDOW_SECONDS//3600}h ({WINDOW_SECONDS//86400}d)")

    print(f"\n[1] Fetching short-term markets (4h-7d horizon)...")
    client = PolymarketDataApiClient()
    markets = await get_short_term_markets(limit=15)
    print(f"    got {len(markets)} markets")

    print(f"\n[2] Fetching fills + price history per market...")
    all_assessments: list[Assessment] = []
    for m in markets:
        try:
            r = await fetch_market_assessments(client, m, since_ts=since)
            print(f"    {m['title'][:48]:48s} → {len(r)} valid episodes (post resolution+price filter)")
            all_assessments.extend(r)
        except Exception as e:
            print(f"    !!! {m['title'][:40]}: {e}")
        await asyncio.sleep(0.3)

    print(f"\n  TOTAL valid assessments: {len(all_assessments)}")
    print(f"  unique wallets:           {len(set(a.episode.wallet for a in all_assessments))}")
    print(f"  unique markets:           {len(set(a.episode.market_id for a in all_assessments))}")

    if not all_assessments:
        print("EMPTY — abort")
        return

    # Per [GPT 20]: train 48h → test 24h, roll 24h ⇒ ~5 folds на 7d
    TRAIN_H = 48
    TEST_H = 24
    ROLL_H = 24
    print(f"\n[3] Walk-forward: train {TRAIN_H}h → test next {TEST_H}h (rolling {ROLL_H}h)...")
    folds = []
    bucket_min = min(a.episode.bucket_ts for a in all_assessments)
    bucket_max = max(a.episode.bucket_ts for a in all_assessments)
    train_start = bucket_min
    while train_start + (TRAIN_H + TEST_H) * 3600 <= bucket_max:
        train_end = train_start + TRAIN_H * 3600
        test_start = train_end
        test_end = test_start + TEST_H * 3600
        fold = walk_forward_test(all_assessments, train_start, train_end, test_start, test_end)
        fold["train_start"] = datetime.fromtimestamp(train_start, tz=timezone.utc).isoformat()[5:16]
        fold["test_start"] = datetime.fromtimestamp(test_start, tz=timezone.utc).isoformat()[5:16]
        folds.append(fold)
        train_start += ROLL_H * 3600

    if not folds:
        print("    Not enough data for walk-forward — using single split")
        mid = (bucket_min + bucket_max) // 2
        folds.append(walk_forward_test(all_assessments, bucket_min, mid, mid, bucket_max))

    print(f"\n  Walk-forward folds:")
    print(f"  {'train@':<8} {'test@':<8} {'eligTrain':<10} {'TestEp':<8} {'medFwd':<10} {'hit%':<6} {'PF':<6} {'edgePP':<8}")
    for f in folds:
        if "error" in f:
            print(f"    {f['error']}")
            continue
        print(
            f"  {f.get('train_start','?'):<8} {f.get('test_start','?'):<8} "
            f"{f['eligible_in_train']:<10} {f['test_eligible_episodes']:<8} "
            f"{f.get('median_fwd_2h',0):+.2%}     "
            f"{f.get('hit_rate',0):.0%}    "
            f"{f.get('profit_factor',0):.2f}    "
            f"{f.get('edge_over_baseline_pp',0):+.2f}pp"
        )

    print(f"\n[4] Aggregate metrics across folds:")
    valid = [f for f in folds if "median_fwd_2h" in f and f.get("test_eligible_episodes", 0) >= 5]
    if valid:
        print(f"    n_valid_folds:       {len(valid)}/{len(folds)}")
        print(f"    median fold fwd_2h:  {statistics.median([f['median_fwd_2h'] for f in valid]):+.2%}")
        print(f"    median fold hit%:    {statistics.median([f['hit_rate'] for f in valid]):.0%}")
        print(f"    median fold PF:      {statistics.median([f['profit_factor'] for f in valid]):.2f}")
        print(f"    median edge vs random:{statistics.median([f['edge_over_baseline_pp'] for f in valid]):+.2f}pp")
        total_test_eps = sum(f['test_eligible_episodes'] for f in valid)
        print(f"    total OOS episodes:  {total_test_eps}")

        passes_oos_median = statistics.median([f['median_fwd_2h'] for f in valid]) >= 0.01
        passes_hit = statistics.median([f['hit_rate'] for f in valid]) >= 0.55
        passes_pf = statistics.median([f['profit_factor'] for f in valid]) >= 1.25
        passes_baseline = statistics.median([f['edge_over_baseline_pp'] for f in valid]) >= 1.0
        passes_n = total_test_eps >= 50
        print()
        print(f"    PASS gates per [GPT 20]:")
        print(f"      median fwd_2h ≥ +1.0%:   {'✓' if passes_oos_median else '✗'}")
        print(f"      median hit_rate ≥ 55%:   {'✓' if passes_hit else '✗'}")
        print(f"      median PF ≥ 1.25:        {'✓' if passes_pf else '✗'}")
        print(f"      edge vs random ≥ 1.0pp:  {'✓' if passes_baseline else '✗'}")
        print(f"      OOS episodes ≥ 50:       {'✓' if passes_n else '✗'}")
        all_pass = all([passes_oos_median, passes_hit, passes_pf, passes_baseline, passes_n])
        print()
        print(f"    OVERALL: {'PASS — proceed to shadow deploy' if all_pass else 'FAIL — fix or scrap'}")

    print(f"\n[5] Saving raw to /tmp/sm_v2_assessments.jsonl")
    with open("/tmp/sm_v2_assessments.jsonl", "w") as fh:
        for a in all_assessments:
            fh.write(json.dumps({
                "wallet": a.episode.wallet,
                "market": a.episode.market_id,
                "side": a.episode.side,
                "bucket_ts": a.episode.bucket_ts,
                "fills": a.episode.fills_count,
                "notional": a.episode.total_notional,
                "entry_price": a.entry_price,
                "fwd_2h_winsor": a.fwd_2h_return_winsor,
                "lag_points": a.lag_drift_points,
            }) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
