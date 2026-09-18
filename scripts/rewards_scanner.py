"""Polymarket Rewards Scanner per [GPT 24].

Pool per market * (our_score / total_score) = expected reward.

Scanner per [GPT 24] design:
  fetch active reward markets
  join CLOB orderbook (depth in reward band)
  estimate our share with $25/$50/$100 depth
  rank by expected_daily_reward / capital_required

Output: ranked table to stdout + /app/data/rewards_table.jsonl

Pass criterion (per [GPT 24]):
  estimated_net_reward >= $1/day
  adverse_selection_p95 <= $2/day
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


OUTPUT_PATH = Path("/app/data/rewards_table.jsonl")

logger = logging.getLogger(__name__)


async def fetch_reward_markets(min_hours: float = 12.0, max_hours: float = 720.0) -> list[dict]:
    """Pull markets с rewardsMaxSpread > 0. Endpoint: Gamma."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": 500, "order": "volume24hr", "ascending": "false"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()

    cutoff_min = datetime.now(timezone.utc) + timedelta(hours=min_hours)
    cutoff_max = datetime.now(timezone.utc) + timedelta(hours=max_hours)
    out = []
    for m in markets:
        if (m.get("rewardsMaxSpread") or 0) <= 0:
            continue
        if (m.get("rewardsMinSize") or 0) <= 0:
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
        if end_dt < cutoff_min or end_dt > cutoff_max:
            continue
        try:
            tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        except Exception:
            continue
        if len(tokens) < 2:
            continue
        try:
            prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
            yes_price = float(prices[0]) if prices else 0.5
        except Exception:
            yes_price = 0.5
        out.append({
            "condition_id": m["conditionId"],
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "title": m.get("question", "")[:200],
            "rewards_max_spread": float(m.get("rewardsMaxSpread") or 0),
            "rewards_min_size": float(m.get("rewardsMinSize") or 0),
            "best_bid": float(m.get("bestBid") or 0),
            "best_ask": float(m.get("bestAsk") or 0),
            "liquidity": float(m.get("liquidity") or 0),
            "volume24hr": float(m.get("volume24hr") or 0),
            "yes_price": yes_price,
            "end_dt": end_dt,
        })
    return out


async def fetch_orderbook(token_id: str) -> dict | None:
    url = "https://clob.polymarket.com/book"
    params = {"token_id": token_id}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def estimate_depth_in_band(book: dict, side: str, mid: float, max_spread_pp: float) -> float:
    """Sum existing maker depth (not us) within reward band."""
    if not book:
        return 0
    levels = book.get("bids" if side == "buy" else "asks", [])
    band_lo = mid - max_spread_pp / 100  # e.g. 2.5pp = 0.025
    band_hi = mid + max_spread_pp / 100
    total = 0.0
    for lvl in levels:
        try:
            p = float(lvl.get("price", 0))
            sz = float(lvl.get("size", 0))
        except Exception:
            continue
        if band_lo <= p <= band_hi:
            total += p * sz
    return total


async def analyze_market(client, m: dict, our_capital: float = 50.0) -> dict:
    """Per [GPT 25]: opportunity SCORE (0-100), не $/day fantasy.

    Score components:
      volume_factor:  log scale of volume24hr (more activity = more rewards)
      undercompetition_factor:  inverse of existing_depth_in_band (less competition = better share)
      spread_factor:  rewards_max_spread (wider band = easier qualify)
      capital_efficiency:  our_capital >= rewards_min_size (must clear)
      tail_penalty:  mid in [0.10, 0.90] (no extreme tails per [GPT 25])
      time_safety:  hours_to_resolution > 12 (avoid resolution risk)
    """
    book = await fetch_orderbook(m["yes_token"])
    if not book:
        return {**m, "skip_reason": "no_book"}

    mid = (m["best_bid"] + m["best_ask"]) / 2 if m["best_bid"] > 0 and m["best_ask"] > 0 else m["yes_price"]
    if mid <= 0:
        return {**m, "skip_reason": "no_mid"}

    bid_depth = estimate_depth_in_band(book, "buy", mid, m["rewards_max_spread"])
    ask_depth = estimate_depth_in_band(book, "sell", mid, m["rewards_max_spread"])
    existing_depth = bid_depth + ask_depth

    # Components 0-100 each (then weighted average)
    import math
    volume_score = min(100, max(0, math.log10(max(m["volume24hr"], 1)) * 20))  # 100 at $100K
    competition_score = max(0, 100 - (existing_depth / 100))  # 0 при $10K depth
    spread_score = min(100, m["rewards_max_spread"] * 20)  # 100 at 5pp
    capital_ok = our_capital >= m["rewards_min_size"]
    tail_safe = 0.10 <= mid <= 0.90
    time_safe = True  # already filtered к 12h+ в fetch
    if not capital_ok or not tail_safe or not time_safe:
        opportunity_score = 0
    else:
        opportunity_score = round(
            (volume_score * 0.4 + competition_score * 0.4 + spread_score * 0.2),
            1,
        )

    return {
        **m,
        "mid": round(mid, 4),
        "existing_depth": round(existing_depth, 0),
        "volume_score": round(volume_score, 1),
        "competition_score": round(competition_score, 1),
        "spread_score": round(spread_score, 1),
        "capital_ok": capital_ok,
        "tail_safe": tail_safe,
        "opportunity_score": opportunity_score,
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=50.0)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    print("=" * 80)
    print("Polymarket Rewards Scanner (per [GPT 24])")
    print("=" * 80)

    print("\n[1] Fetch reward-eligible markets...")
    markets = await fetch_reward_markets()
    print(f"   {len(markets)} markets с active rewards (rewardsMaxSpread > 0, hours_to_res 12-720)")

    print("\n[2] Sort by volume, analyze top 30 для depth estimation...")
    markets.sort(key=lambda m: m["volume24hr"], reverse=True)
    sample = markets[:30]
    analyzed = []
    async with httpx.AsyncClient() as client:
        for i, m in enumerate(sample, 1):
            r = await analyze_market(client, m, our_capital=args.capital)
            if "skip_reason" not in r:
                analyzed.append(r)
            if i % 10 == 0:
                print(f"   analyzed {i}/{len(sample)}")
            await asyncio.sleep(0.1)

    print(f"\n[3] {len(analyzed)} markets analyzed.")
    analyzed.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)

    print(f"\n[4] Top {args.top} by opportunity_score (0-100, не $/day):")
    print(f"   {'score':<6} {'spread':<8} {'mid':<7} {'depth':<10} {'vol_s':<6} {'comp_s':<6} {'spread_s':<8}  title")
    for r in analyzed[:args.top]:
        print(
            f"   {r['opportunity_score']:<6.1f} {r['rewards_max_spread']:<8.2f} {r['mid']:<7.3f} "
            f"${r['existing_depth']:<9.0f} {r['volume_score']:<6.1f} {r['competition_score']:<6.1f} "
            f"{r['spread_score']:<8.1f}  {r['title'][:50]}"
        )

    pass_threshold = 50  # opportunity score
    qualified = [r for r in analyzed if r.get("opportunity_score", 0) >= pass_threshold]
    print(f"\n[5] Qualified (score ≥ {pass_threshold}): {len(qualified)} markets")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as fh:
        for r in analyzed:
            r2 = {k: v for k, v in r.items() if k != "end_dt"}
            fh.write(json.dumps(r2, default=str) + "\n")
    print(f"\n[6] Saved analysis: {OUTPUT_PATH}")

    print("\n[7] Verdict (per [GPT 25] opportunity score, NOT PnL fantasy):")
    if qualified:
        avg_score = sum(r["opportunity_score"] for r in qualified) / len(qualified)
        print(f"   {len(qualified)} markets pass score ≥ 50")
        print(f"   Avg opportunity_score: {avg_score:.1f}")
        print("   These are RANKING priorities, not $$$ predictions.")
        print("   Real $$$ would require live deployment + actual reward distribution data.")
    else:
        print("   NONE qualify score ≥ 50")
        print("   Per [GPT 24]: 'rewards likely institution-scale, not us-scale'")
        print("   Per [GPT 25]: real verification requires live observation, не proxy.")


if __name__ == "__main__":
    asyncio.run(main())
