"""Negative-Risk Arbitrage Scanner per [GPT 26].

Polymarket negRisk events: mutually exclusive outcomes (e.g., 13 senate candidates).
Sum of YES prices should = 1.0 если markets calibrated.

Arb opportunity:
  sum_yes < threshold (e.g. 0.97) → BUY all YES, lock in (1 - sum_yes) profit per $1
  sum_yes > 1.03 → BUY all NO instead

Constraint per [GPT 26]: SCANNER ONLY, NO live orders без hedge_manager.
Output: /app/data/arb_opportunities.jsonl per event, sum_yes, depth, edge_estimate.
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

OUTPUT_PATH = Path("/app/data/arb_opportunities.jsonl")
ARB_THRESHOLD = 0.03  # 3pp deviation от 1.0 = arb candidate

logger = logging.getLogger(__name__)


async def fetch_neg_risk_events(limit: int = 300) -> list[dict]:
    """Pull events с negRisk=true."""
    url = "https://gamma-api.polymarket.com/events"
    params = {
        "active": "true", "closed": "false",
        "limit": limit,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        events = r.json()
    return [e for e in events if e.get("negRisk") == True and len(e.get("markets", [])) >= 2]


def parse_market_yes_price(market: dict) -> float | None:
    """Extract YES price from market."""
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            return None
    else:
        prices = raw or []
    if not prices:
        return None
    try:
        return float(prices[0])
    except Exception:
        return None


def parse_market_liquidity(market: dict) -> float:
    try:
        return float(market.get("liquidity") or 0)
    except Exception:
        return 0


def analyze_event(event: dict) -> dict:
    """Compute sum_yes, sum_no, identify arb."""
    markets = event.get("markets", []) or []
    sum_yes = 0.0
    valid = 0
    min_liq = float("inf")
    max_liq = 0
    for m in markets:
        yes = parse_market_yes_price(m)
        if yes is None or yes <= 0 or yes >= 1:
            continue
        sum_yes += yes
        liq = parse_market_liquidity(m)
        min_liq = min(min_liq, liq)
        max_liq = max(max_liq, liq)
        valid += 1

    if valid < 2:
        return {"event_id": event.get("id"), "skip_reason": "too_few_markets"}

    sum_no = valid - sum_yes  # NO price = (1 - YES) per market
    deviation = sum_yes - 1.0  # how far from calibrated 1.0
    # FIX-1 [Claude 47]: всегда писать raw_edge_pp = |deviation|*100 (раньше 0 если ниже threshold).
    # tradable отдельный флаг — иначе все 12K events/24h имели edge=0 и scanner был mute.
    raw_edge_pp = abs(deviation) * 100
    tradable = abs(deviation) >= ARB_THRESHOLD
    arb_signal = None
    if tradable:
        arb_signal = "BUY_YES_basket" if deviation < 0 else "BUY_NO_basket"
    return {
        "event_id": event.get("id"),
        "title": (event.get("title") or event.get("slug") or "")[:80],
        "n_markets": valid,
        "sum_yes": round(sum_yes, 4),
        "sum_no": round(sum_no, 4),
        "deviation": round(deviation, 4),
        "arb_signal": arb_signal,
        "tradable": tradable,
        "edge_pp": round(raw_edge_pp if tradable else 0.0, 2),  # backward-compat
        "raw_edge_pp": round(raw_edge_pp, 4),  # always populated for backtest
        "min_liq": min_liq if min_liq != float("inf") else 0,
        "max_liq": max_liq,
    }


def append_records(records: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    with OUTPUT_PATH.open("a") as fh:
        for r in records:
            fh.write(json.dumps({**r, "ts": now_ts}) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=ARB_THRESHOLD)
    args = parser.parse_args()

    started = time.time()
    events = await fetch_neg_risk_events()
    logger.info(f"arb_scan.fetched | neg_risk_events={len(events)}")

    analyzed = []
    for e in events:
        result = analyze_event(e)
        if "skip_reason" not in result:
            analyzed.append(result)

    arb_candidates = [a for a in analyzed if a.get("arb_signal")]
    append_records(analyzed)

    elapsed = round(time.time() - started, 1)
    logger.info(
        f"arb_scan_complete | analyzed={len(analyzed)} arbs={len(arb_candidates)} elapsed={elapsed}s"
    )

    print(f"\n=== Top arb candidates (≥{args.threshold*100:.1f}pp deviation) ===")
    arb_candidates.sort(key=lambda r: -abs(r["deviation"]))
    print(f"{'edge_pp':<8} {'sig':<18} {'sum_yes':<10} {'#mkts':<6} {'min_liq':<10}  title")
    for a in arb_candidates[:15]:
        print(
            f"{a['edge_pp']:<8.2f} {a['arb_signal']:<18} "
            f"{a['sum_yes']:<10.4f} {a['n_markets']:<6} ${a['min_liq']:<9.0f}  {a['title']}"
        )

    print(f"\nTotal: {len(arb_candidates)}/{len(analyzed)} events с arb edge ≥{args.threshold*100:.1f}pp")


if __name__ == "__main__":
    asyncio.run(main())
