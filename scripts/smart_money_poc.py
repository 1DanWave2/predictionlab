"""Smart Money MVP PoC per [GPT 18].

Goal: prove that wallet fills predict positive forward returns AFTER realistic lag.
If yes → build production pipeline. If no → scrap and try other strategies.

Pipeline:
1. Pick N hot markets (high volume, mid-tier prices).
2. Fetch last 24h fills for each market.
3. Fetch prices-history for each token (outcome).
4. For each fill: compute forward return at 30m/2h after entry,
   simulating copy-entry with 5-10s lag.
5. Aggregate per wallet: hit_rate, median_fwd_return, total_volume.
6. Identify eligible wallets per [GPT 18] gates:
   - fills_last_24h >= 3
   - volume_24h >= $500
   - median_fwd_2h_after_lag > 1.5%
   - hit_rate_2h > 55%
7. Print summary report.

Run: python3 scripts/smart_money_poc.py
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.integrations.polymarket_data_api import PMFill, PolymarketDataApiClient


SECONDS_24H = 86400
SECONDS_30M = 1800
SECONDS_2H = 7200
COPY_LAG_SECONDS = 8  # GPT 18: realistic latency


async def get_top_markets(limit: int = 15) -> list[dict]:
    """Fetch active short-term markets (resolution next 7 days), sorted by 24h volume."""
    from datetime import datetime, timedelta, timezone
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 200,
        "order": "volume24hr",
        "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()

    cutoff = datetime.now(timezone.utc) + timedelta(days=7)
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
        if end_dt > cutoff or end_dt < datetime.now(timezone.utc):
            continue  # skip long-term and already-resolved
        # mid-tier price filter
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
            "liquidity": float(m.get("liquidityNum", 0) or 0),
            "volume_24h": float(m.get("volume24hr", 0) or 0),
            "yes_price": yes_price,
            "title": m.get("question", "")[:60],
            "slug": m.get("slug", ""),
            "end_date": end_raw,
        })
        if len(out) >= limit:
            break
    return out


def price_at_time(history: list[tuple[int, float]], target_ts: int) -> float | None:
    """Linear-interp lookup price at given timestamp from history."""
    if not history:
        return None
    if target_ts <= history[0][0]:
        return history[0][1]
    if target_ts >= history[-1][0]:
        return history[-1][1]
    # binary search
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


def assess_fill(
    fill: PMFill,
    yes_history: list[tuple[int, float]],
    no_history: list[tuple[int, float]],
    yes_token: str,
) -> dict | None:
    """Compute forward return after copy lag.

    Логика: wallet bought asset X at price P at ts T.
    We "copy" by buying same asset at T+lag → entry price = price at T+lag.
    Forward return at T+lag+30m = (price_at_T+lag+30m / entry_price) - 1
    """
    is_yes = fill.asset == yes_token
    history = yes_history if is_yes else no_history
    if not history:
        return None

    entry_ts = fill.timestamp + COPY_LAG_SECONDS
    entry_price = price_at_time(history, entry_ts)
    if entry_price is None or entry_price <= 0:
        return None

    fwd_30m_price = price_at_time(history, entry_ts + SECONDS_30M)
    fwd_2h_price = price_at_time(history, entry_ts + SECONDS_2H)
    if fwd_30m_price is None and fwd_2h_price is None:
        return None

    direction = 1.0 if fill.side == "BUY" else -1.0
    fwd_30m_ret = (fwd_30m_price - entry_price) / entry_price * direction if fwd_30m_price else None
    fwd_2h_ret = (fwd_2h_price - entry_price) / entry_price * direction if fwd_2h_price else None
    entry_lag_drift = (entry_price - fill.price) / max(fill.price, 0.01) * direction

    return {
        "wallet": fill.wallet,
        "side": fill.side,
        "fill_price": fill.price,
        "entry_price": entry_price,
        "entry_lag_drift": entry_lag_drift,  # how much price moved adverse to us in lag
        "fwd_30m_ret": fwd_30m_ret,
        "fwd_2h_ret": fwd_2h_ret,
        "notional": fill.notional,
        "ts": fill.timestamp,
    }


async def analyze_market(client: PolymarketDataApiClient, market: dict, since_ts: int) -> list[dict]:
    """For one market: fetch fills + price history → assessments."""
    print(f"  Market: {market['title'][:50]}... liq=${market['liquidity']:.0f}")
    fills = await client.iterate_market_fills(
        market["condition_id"], since_ts=since_ts, page_size=500, max_pages=20
    )
    print(f"    fills: {len(fills)}")
    if not fills:
        return []
    yes_hist = await client.fetch_prices_history(market["yes_token"])
    no_hist = await client.fetch_prices_history(market["no_token"])
    print(f"    yes_history points: {len(yes_hist)}, no_history points: {len(no_hist)}")

    out: list[dict] = []
    for f in fills:
        a = assess_fill(f, yes_hist, no_hist, market["yes_token"])
        if a:
            a["market_title"] = market["title"]
            out.append(a)
    return out


def aggregate_wallets(assessments: list[dict]) -> dict[str, dict]:
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for a in assessments:
        by_wallet[a["wallet"]].append(a)

    result = {}
    for wallet, assess_list in by_wallet.items():
        rets_2h = [a["fwd_2h_ret"] for a in assess_list if a["fwd_2h_ret"] is not None]
        rets_30m = [a["fwd_30m_ret"] for a in assess_list if a["fwd_30m_ret"] is not None]
        lags = [a["entry_lag_drift"] for a in assess_list]
        notionals = [a["notional"] for a in assess_list]
        if not rets_2h:
            continue
        wins_2h = sum(1 for r in rets_2h if r > 0)
        result[wallet] = {
            "wallet": wallet,
            "fills": len(assess_list),
            "volume": sum(notionals),
            "median_fill_size": statistics.median(notionals),
            "median_fwd_30m": statistics.median(rets_30m) if rets_30m else 0.0,
            "median_fwd_2h": statistics.median(rets_2h),
            "hit_rate_2h": wins_2h / len(rets_2h),
            "median_lag_drift": statistics.median(lags),
        }
    return result


def filter_eligible(wallets: dict[str, dict]) -> list[dict]:
    eligible = []
    for w in wallets.values():
        if w["fills"] < 3:  # MVP softer than 7d's >=10
            continue
        if w["volume"] < 500:  # softer than 7d's >=$2000
            continue
        if w["median_fwd_2h"] < 0.015:  # GPT 18: >1.5%
            continue
        if w["hit_rate_2h"] < 0.55:
            continue
        eligible.append(w)
    eligible.sort(key=lambda w: w["median_fwd_2h"], reverse=True)
    return eligible


async def main() -> None:
    print("=" * 70)
    print("Smart Money MVP PoC — per [GPT 18]")
    print("=" * 70)
    now = int(time.time())
    since = now - SECONDS_24H

    print(f"\n[1] Fetching top 15 markets by liquidity...")
    client = PolymarketDataApiClient()
    markets = await get_top_markets(limit=15)
    print(f"    got {len(markets)} markets")

    print(f"\n[2] Fetching fills + price history for each market (24h window)...")
    all_assessments: list[dict] = []
    for m in markets:
        try:
            r = await analyze_market(client, m, since_ts=since)
            all_assessments.extend(r)
        except Exception as e:
            print(f"    !!! error on market {m['title'][:40]}: {e}")
        await asyncio.sleep(0.3)  # respectful pacing
    print(f"\n  TOTAL assessments: {len(all_assessments)}")
    print(f"  TOTAL wallets seen: {len(set(a['wallet'] for a in all_assessments))}")

    print(f"\n[3] Aggregating per wallet...")
    by_wallet = aggregate_wallets(all_assessments)
    print(f"    wallets with >=1 fill+fwd_data: {len(by_wallet)}")

    print(f"\n[4] Filtering eligible (per [GPT 18] softened gates)...")
    eligible = filter_eligible(by_wallet)
    print(f"    eligible wallets: {len(eligible)}")

    if eligible:
        print(f"\n[5] Top 20 eligible wallets:")
        print(f"    {'wallet':<14} {'fills':<6} {'vol':<10} {'med_2h':<10} {'hit%':<6} {'lag_drift':<10}")
        for w in eligible[:20]:
            print(
                f"    {w['wallet'][:12]:<14} {w['fills']:<6} ${w['volume']:<9.0f} "
                f"{w['median_fwd_2h']:+.2%}     {w['hit_rate_2h']:.0%}    {w['median_lag_drift']:+.2%}"
            )

    print(f"\n[6] Population summary:")
    if all_assessments:
        all_2h = [a["fwd_2h_ret"] for a in all_assessments if a["fwd_2h_ret"] is not None]
        all_lags = [a["entry_lag_drift"] for a in all_assessments]
        print(f"    median forward 2h return (all fills): {statistics.median(all_2h):+.2%}")
        print(f"    mean forward 2h return (all fills):   {statistics.mean(all_2h):+.2%}")
        print(f"    median entry lag drift:               {statistics.median(all_lags):+.2%}")
        wins = sum(1 for r in all_2h if r > 0)
        print(f"    overall hit rate 2h:                  {wins/len(all_2h):.1%}")

    out_path = Path("/tmp/smart_money_poc_assessments.jsonl")
    with out_path.open("w") as fh:
        for a in all_assessments:
            fh.write(json.dumps(a, default=str) + "\n")
    print(f"\n[7] Saved raw assessments: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
