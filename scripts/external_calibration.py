"""External calibration via Gamma resolved markets per [GPT 26].

Pull 1000+ resolved Polymarket markets, compute realized win rate
(YES outcome) by price bucket / time bucket / tag.

This gives us 200+ samples per bucket для real calibration, instead of
waiting for our own 100 cycles.

Output: /app/data/external_calibration.json with bias adjustments.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

OUTPUT_PATH = Path("/app/data/external_calibration.json")


async def fetch_resolved_markets(limit_total: int = 2000) -> list[dict]:
    """Pull recent resolved markets from Gamma."""
    url = "https://gamma-api.polymarket.com/markets"
    out = []
    offset = 0
    page_size = 200
    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(out) < limit_total:
            params = {
                "closed": "true",
                "active": "false",
                "archived": "false",
                "limit": page_size,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
            }
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                page = r.json()
                if not page:
                    break
                out.extend(page)
                offset += page_size
            except Exception as e:
                print(f"  err page offset={offset}: {e}")
                break
    return out[:limit_total]


def categorize_by_tags(market: dict) -> str:
    """Coarse category from tags or category field."""
    cat = (market.get("category") or "").lower()
    if cat:
        return cat
    tags = market.get("tags", []) or []
    if isinstance(tags, str):
        tags = [tags]
    tag_text = " ".join(str(t).lower() for t in tags)
    if "sports" in tag_text or "nba" in tag_text or "mlb" in tag_text or "nhl" in tag_text or "ufc" in tag_text:
        return "sports"
    if "politics" in tag_text or "election" in tag_text:
        return "politics"
    if "crypto" in tag_text or "bitcoin" in tag_text:
        return "crypto"
    return "event"


def price_bucket(price: float) -> str:
    if price < 0.20: return "p_lt_20"
    if price < 0.30: return "p_20_30"
    if price < 0.40: return "p_30_40"
    if price < 0.50: return "p_40_50"
    if price < 0.60: return "p_50_60"
    if price < 0.70: return "p_60_70"
    if price < 0.80: return "p_70_80"
    if price < 0.90: return "p_80_90"
    return "p_gt_90"


def did_yes_resolve(market: dict) -> bool | None:
    """Determine if YES outcome resolved. Use closedOutcome / outcomePrices."""
    # outcomePrices в closed market = final settlement (1 = won, 0 = lost)
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            return None
    else:
        prices = raw or []
    if not prices or len(prices) < 1:
        return None
    try:
        yes_final = float(prices[0])
    except Exception:
        return None
    if yes_final >= 0.99:
        return True
    if yes_final <= 0.01:
        return False
    return None  # ambiguous (probably canceled or partially-resolved)


def get_pre_resolution_yes_price(market: dict) -> float | None:
    """Best proxy для entry price prior to resolution.
    Use lastTradePrice или mid (best_bid + best_ask)/2 from when market was hot.
    """
    last = market.get("lastTradePrice")
    if last is not None:
        try:
            return float(last)
        except Exception:
            pass
    bid = market.get("bestBid") or 0
    ask = market.get("bestAsk") or 0
    try:
        bid_f = float(bid)
        ask_f = float(ask)
        if bid_f > 0 and ask_f > 0:
            return (bid_f + ask_f) / 2
    except Exception:
        pass
    return None


async def main() -> None:
    print("=" * 76)
    print("External Calibration via Gamma resolved markets (per [GPT 26] alt)")
    print("=" * 76)

    print("\n[1] Fetching resolved markets...")
    markets = await fetch_resolved_markets(limit_total=2000)
    print(f"   fetched: {len(markets)}")

    # Filter usable (decisive resolution + price available)
    usable = []
    for m in markets:
        yes_won = did_yes_resolve(m)
        if yes_won is None:
            continue
        # Use last_price proxy
        pre = get_pre_resolution_yes_price(m)
        if pre is None or pre < 0.05 or pre > 0.95:
            continue
        usable.append({
            "id": m.get("conditionId"),
            "question": m.get("question", "")[:80],
            "category": categorize_by_tags(m),
            "pre_yes_price": pre,
            "yes_won": yes_won,
            "p_bucket": price_bucket(pre),
            "liquidity": float(m.get("liquidity") or 0),
            "volume": float(m.get("volume") or 0),
        })

    print(f"\n[2] Usable resolved markets: {len(usable)}")

    # Distribution
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_cat_bucket: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for u in usable:
        by_bucket[u["p_bucket"]].append(u)
        by_cat_bucket[(u["category"], u["p_bucket"])].append(u)

    print("\n[3] Per-bucket realized YES outcome rate:")
    print(f"  {'bucket':<10} {'n':<6} {'pre_avg':<8} {'YES_won_rate':<12} {'expected':<10} {'bias':<8}")
    bucket_bias: dict[str, float] = {}
    for bucket, items in sorted(by_bucket.items()):
        if not items:
            continue
        wins = sum(1 for x in items if x["yes_won"])
        wr = wins / len(items)
        avg_pre = statistics.mean(x["pre_yes_price"] for x in items)
        # bias = realized_wr - market_pre_price (if market well calibrated, bias=0)
        bias = wr - avg_pre
        bucket_bias[bucket] = bias
        flag = ""
        if bias > 0.05 and len(items) >= 30:
            flag = " ⭐ underpriced YES (mispriced)"
        elif bias < -0.05 and len(items) >= 30:
            flag = " ⚠ overpriced YES (NO underpriced)"
        print(f"  {bucket:<10} {len(items):<6} {avg_pre:<8.3f} {wr:<12.3f} {avg_pre:<10.3f} {bias:+8.3f}{flag}")

    print("\n[4] Per (category × bucket) sample (n>=30):")
    for (cat, bucket), items in sorted(by_cat_bucket.items()):
        if len(items) < 30:
            continue
        wins = sum(1 for x in items if x["yes_won"])
        wr = wins / len(items)
        avg_pre = statistics.mean(x["pre_yes_price"] for x in items)
        bias = wr - avg_pre
        print(f"  {cat:<12} {bucket:<10} n={len(items):<5} pre_avg={avg_pre:.3f} wr={wr:.3f} bias={bias:+.3f}")

    output = {
        "n_total": len(usable),
        "bucket_bias": {k: round(v, 4) for k, v in bucket_bias.items()},
        "n_per_bucket": {k: len(v) for k, v in by_bucket.items()},
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"\n[5] Saved: {OUTPUT_PATH}")

    print("\n[6] Interpretation:")
    print("  bias > 0  → market UNDERPRICED YES → BUY YES extra edge")
    print("  bias < 0  → market OVERPRICED YES → AVOID/short YES")
    print("  |bias| ≈ 0 → market well calibrated → no overlay edge")


if __name__ == "__main__":
    asyncio.run(main())
