"""Smart Money v2 — Weather Subset per [Claude 35] day-2.

Smart Money v1 failed against broad sample (0 eligible wallets). This v2 retries
against a NARROW subset: wallets that have repeatedly traded weather/temperature
negRisk events specifically.

Strategy:
  1. Scan local pm_fills SQLite for weather-event fills (title contains 'temperature')
  2. Aggregate per wallet: n_temp_trades, n_unique_events, avg_size, recency
  3. Rank candidates that look like specialists (≥3 trades, $5-$200 avg, active <14d)
  4. Pull each candidate wallet's CURRENT open positions on weather events via Data API
  5. Log "follow_signal" — log what each candidate is currently long, with weight
  6. Resolution outcomes accumulate into wallet score over time

Output: /app/data/sm_weather_candidates.jsonl  (per-run snapshot)
        /app/data/sm_weather_signals.jsonl     (per-wallet current positions)

NO live trades. Pure shadow accumulation. After 14d, we have:
  - which wallets keep winning weather buckets
  - whether copying their positions would have been +EV
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DB_PATH = Path("/app/data/paper_bot.db")
CANDIDATES_OUT = Path("/app/data/sm_weather_candidates.jsonl")
SIGNALS_OUT = Path("/app/data/sm_weather_signals.jsonl")

DATA_API = "https://data-api.polymarket.com/trades"

# Selection criteria
MIN_TEMP_TRADES = 3
MIN_AVG_POSITION_USD = 5.0
MAX_AVG_POSITION_USD = 500.0
MAX_DAYS_INACTIVE = 14
MAX_CANDIDATES = 100

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1: scan local pm_fills for weather specialists
# ──────────────────────────────────────────────────────────────────────────────

def find_weather_wallets() -> list[dict]:
    """Find wallets with ≥3 temperature-event fills, ranked by activity."""
    if not DB_PATH.exists():
        logger.warning("db_not_found | path=%s", DB_PATH)
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=MAX_DAYS_INACTIVE)).timestamp())

    # Aggregate per wallet
    rows = c.execute("""
        SELECT
            wallet,
            COUNT(*) as n_trades,
            COUNT(DISTINCT condition_id) as n_events,
            AVG(notional) as avg_notional,
            SUM(notional) as total_volume,
            MIN(fill_ts) as earliest,
            MAX(fill_ts) as latest,
            SUM(CASE WHEN side = 'BUY' THEN 1 ELSE 0 END) as n_buys,
            SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) as n_sells
        FROM pm_fills
        WHERE LOWER(title) LIKE '%temperature%'
        GROUP BY wallet
        HAVING n_trades >= ?
        ORDER BY latest DESC, n_trades DESC
    """, (MIN_TEMP_TRADES,)).fetchall()

    candidates = []
    for r in rows:
        wallet, n_t, n_e, avg_n, total_v, earliest, latest, n_buys, n_sells = r
        # Filter by sizing & recency
        if not (MIN_AVG_POSITION_USD <= (avg_n or 0) <= MAX_AVG_POSITION_USD):
            continue
        if (latest or 0) < cutoff_ts:
            continue
        days_since = (int(time.time()) - latest) / 86400
        candidates.append({
            "wallet": wallet,
            "n_temp_trades": n_t,
            "n_unique_events": n_e,
            "avg_notional_usd": round(float(avg_n or 0), 2),
            "total_volume_usd": round(float(total_v or 0), 2),
            "n_buys": n_buys,
            "n_sells": n_sells,
            "first_seen_days_ago": round((int(time.time()) - earliest) / 86400, 1),
            "last_active_days_ago": round(days_since, 1),
        })
    conn.close()
    return candidates[:MAX_CANDIDATES]


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2: pull CURRENT trades for each candidate, log their open weather bets
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_recent_wallet_trades(client: httpx.AsyncClient, wallet: str) -> list[dict]:
    """Pull last 200 trades for one wallet via Data API."""
    try:
        r = await client.get(DATA_API, params={"user": wallet, "limit": 200}, timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.debug("wallet_fetch_failed | wallet=%s err=%s", wallet[:10], exc)
        return []


def extract_weather_signals(wallet: str, trades: list[dict]) -> list[dict]:
    """Filter to recent (last 7 days) weather-event trades."""
    cutoff_ts = int(time.time()) - 7 * 86400
    signals = []
    for t in trades:
        title = (t.get("title") or "").lower()
        if "temperature" not in title:
            continue
        ts = t.get("timestamp") or 0
        if ts < cutoff_ts:
            continue
        signals.append({
            "wallet": wallet,
            "fill_ts": ts,
            "side": t.get("side"),
            "outcome": t.get("outcome", ""),
            "outcome_index": t.get("outcomeIndex", 0),
            "condition_id": t.get("conditionId"),
            "title": title[:80],
            "size": float(t.get("size") or 0),
            "price": float(t.get("price") or 0),
            "notional": float(t.get("size") or 0) * float(t.get("price") or 0),
        })
    return signals


def append_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-wallets", type=int, default=50,
                        help="cap on wallet API calls per run")
    parser.add_argument("--skip-api", action="store_true",
                        help="only do candidate scoring, no Data API calls")
    args = parser.parse_args()

    started = time.time()
    ts_now = int(started)

    # Phase 1
    candidates = find_weather_wallets()
    logger.info("sm_weather.candidates | n=%d", len(candidates))

    if candidates:
        snap = {"ts": ts_now, "kind": "candidate_snapshot", "candidates": candidates}
        append_records(CANDIDATES_OUT, [snap])

    if args.skip_api or not candidates:
        print(f"\n=== Smart Money v2 Weather — Candidates ({len(candidates)}) ===\n")
        print(f"{'wallet':<14} {'trades':<8} {'events':<8} {'avg_$':<8} {'total_$':<10} {'last_d':<7}")
        for c in candidates[:30]:
            print(f"{c['wallet'][:12]:<14} {c['n_temp_trades']:<8} {c['n_unique_events']:<8} "
                  f"${c['avg_notional_usd']:<6.0f} ${c['total_volume_usd']:<8.0f} "
                  f"{c['last_active_days_ago']:<7.1f}")
        return

    # Phase 2
    wallets_to_query = [c["wallet"] for c in candidates[:args.max_wallets]]
    logger.info("sm_weather.api_pull | wallets=%d", len(wallets_to_query))

    sem = asyncio.Semaphore(8)

    async def _pull_one(wallet: str) -> list[dict]:
        async with sem:
            async with httpx.AsyncClient() as client:
                trades = await fetch_recent_wallet_trades(client, wallet)
                return extract_weather_signals(wallet, trades)

    results = await asyncio.gather(*[_pull_one(w) for w in wallets_to_query])
    all_signals: list[dict] = []
    for sigs in results:
        all_signals.extend(sigs)

    if all_signals:
        for s in all_signals:
            s["ts"] = ts_now
            s["kind"] = "follow_signal"
        append_records(SIGNALS_OUT, all_signals)

    elapsed = round(time.time() - started, 1)
    logger.info(
        "sm_weather.done | candidates=%d signals=%d elapsed=%ss",
        len(candidates), len(all_signals), elapsed,
    )

    print(f"\n=== Smart Money v2 Weather ({len(candidates)} candidates) ===\n")
    print(f"{'wallet':<14} {'trades':<8} {'events':<8} {'avg_$':<8} {'last_d':<7}")
    for c in candidates[:20]:
        print(f"{c['wallet'][:12]:<14} {c['n_temp_trades']:<8} {c['n_unique_events']:<8} "
              f"${c['avg_notional_usd']:<6.0f} {c['last_active_days_ago']:<7.1f}")

    print(f"\n=== Recent (7d) weather follow-signals ({len(all_signals)}) ===")
    print(f"{'wallet':<14} {'side':<5} {'price':<8} {'size':<8} {'title':<40}")
    for s in all_signals[:25]:
        print(f"{s['wallet'][:12]:<14} {s['side']:<5} ${s['price']:<6.4f} "
              f"{s['size']:<8.0f} {s['title'][:40]}")


if __name__ == "__main__":
    asyncio.run(main())
