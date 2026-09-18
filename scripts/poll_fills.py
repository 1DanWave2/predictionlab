"""Production fills logger per [GPT 18] / [GPT 20].

Single-run script. Designed to be invoked by cron каждые 15-30 минут.

Pulls last 30 минут trades for top-N short-term markets, dedupes against
existing pm_fills via (tx_hash, asset, wallet), appends new ones.

Usage:
    python -m scripts.poll_fills
    python -m scripts.poll_fills --markets 30 --window-min 30

Logged metrics:
    n_markets_polled, n_fills_fetched, n_fills_inserted, n_fills_dup
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db import db_session, initialize_database
from app.integrations.polymarket_data_api import PMFill as APIFill, PolymarketDataApiClient
from app.models import PMFill


logger = logging.getLogger(__name__)


async def get_short_term_markets(limit: int = 30, max_horizon_days: int = 7) -> list[dict]:
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

    cutoff_max = datetime.now(timezone.utc) + timedelta(days=max_horizon_days)
    cutoff_min = datetime.now(timezone.utc) + timedelta(hours=2)
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
        if yes_price < 0.05 or yes_price > 0.95:
            continue
        out.append({
            "condition_id": m["conditionId"],
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "title": m.get("question", "")[:200],
            "slug": m.get("slug", "")[:200],
            "end_ts": int(end_dt.timestamp()),
        })
        if len(out) >= limit:
            break
    return out


def existing_keys() -> set[tuple[str, str, str]]:
    """Pull (tx_hash, asset, wallet) tuples already в БД для dedup."""
    with db_session() as s:
        rows = s.execute(
            select(PMFill.tx_hash, PMFill.asset, PMFill.wallet)
        ).all()
    return {(t, a, w) for (t, a, w) in rows}


def insert_fills(api_fills: list[APIFill]) -> tuple[int, int]:
    """Insert new fills, dedup by (tx_hash, asset, wallet). Returns (inserted, dup)."""
    if not api_fills:
        return 0, 0
    existing = existing_keys()
    to_insert = []
    dup = 0
    for f in api_fills:
        key = (f.transaction_hash, f.asset, f.wallet)
        if key in existing:
            dup += 1
            continue
        existing.add(key)
        to_insert.append(PMFill(
            fill_ts=f.timestamp,
            wallet=f.wallet,
            side=f.side,
            condition_id=f.condition_id,
            asset=f.asset,
            price=f.price,
            size=f.size,
            notional=f.notional,
            title=f.title[:255],
            slug=f.slug[:255],
            outcome=f.outcome[:32],
            tx_hash=f.transaction_hash,
        ))
    if to_insert:
        with db_session() as s:
            s.add_all(to_insert)
    return len(to_insert), dup


async def poll_once(markets_limit: int = 30, window_minutes: int = 30) -> dict:
    initialize_database()
    started = time.time()
    markets = await get_short_term_markets(limit=markets_limit)
    since_ts = int(time.time() - window_minutes * 60)
    client = PolymarketDataApiClient()

    total_fetched = 0
    total_inserted = 0
    total_dup = 0
    errors = 0
    for m in markets:
        try:
            fills = await client.iterate_market_fills(
                m["condition_id"], since_ts=since_ts, page_size=500, max_pages=10
            )
            inserted, dup = insert_fills(fills)
            total_fetched += len(fills)
            total_inserted += inserted
            total_dup += dup
        except Exception as e:
            errors += 1
            logger.warning(f"poll_once: market {m['title'][:40]}: {e}")
        await asyncio.sleep(0.2)

    elapsed = time.time() - started
    return {
        "n_markets": len(markets),
        "n_fetched": total_fetched,
        "n_inserted": total_inserted,
        "n_dup": total_dup,
        "errors": errors,
        "elapsed_s": round(elapsed, 1),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=30)
    parser.add_argument("--window-min", type=int, default=30)
    args = parser.parse_args()

    result = asyncio.run(poll_once(args.markets, args.window_min))
    logger.info(f"pm_fills.poll_complete | {result}")


if __name__ == "__main__":
    main()
