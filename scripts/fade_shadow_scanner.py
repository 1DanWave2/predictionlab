"""Fade-Any Shadow Scanner per [GPT 23] validated edge (+3.61% median 30m, 57% hit).

Standalone cron-friendly script. Каждый run:
  1. Fetch markets via Gamma API (active, mid 0.10-0.90)
  2. Per market: pull 6m of prices-history
  3. Detect 5pp+ pump down в last 5 min
  4. If pump detected AND filters pass: log shadow signal
  5. Stored в /app/data/fade_signals.jsonl для later analysis

Run: docker exec polymarket-bot python3 -m scripts.fade_shadow_scanner

After 24h accumulation: replay forward returns offline → decide live canary.
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

from app.integrations.polymarket_data_api import PolymarketDataApiClient


PUMP_THRESHOLD_MIN = 0.06   # 6pp — empirical sweet spot per fade_replay analysis
PUMP_THRESHOLD_MAX = 0.10   # 10pp+ pumps continue (real news, fade fails)
PUMP_THRESHOLD = PUMP_THRESHOLD_MIN  # legacy var
MIN_HOURS = 6.0
MIN_LIQ = 3000.0
MAX_SPREAD = 0.06

OUTPUT_PATH = Path("/app/data/fade_signals.jsonl")

logger = logging.getLogger(__name__)


async def fetch_manifold_markets(limit: int = 200) -> list[dict]:
    """Per [GPT 25]: log Manifold confirmation as feature, not filter."""
    url = "https://api.manifold.markets/v0/markets"
    params = {"limit": limit}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception:
        return []


def find_manifold_match(pm_title: str, manifold_markets: list[dict]) -> dict | None:
    """Jaccard keyword match. Returns best match if similarity >= 0.4."""
    import re
    stop = {"will", "the", "a", "an", "be", "in", "on", "at", "to", "of", "by", "for", "is", "are"}
    def kw(t: str) -> set[str]:
        text = re.sub(r"[^\w\s]", " ", t.lower())
        return {w for w in text.split() if len(w) >= 4 and w not in stop}
    pm_kw = kw(pm_title)
    if not pm_kw:
        return None
    best, best_sim = None, 0.0
    for m in manifold_markets:
        if m.get("isResolved") or m.get("outcomeType") != "BINARY":
            continue
        prob = m.get("probability")
        if prob is None:
            continue
        mf_kw = kw(m.get("question", ""))
        if not mf_kw:
            continue
        sim = len(pm_kw & mf_kw) / len(pm_kw | mf_kw)
        if sim > best_sim:
            best_sim = sim
            best = m
    if best_sim >= 0.4:
        return {**best, "_similarity": best_sim}
    return None


async def get_eligible_markets(limit: int = 100) -> list[dict]:
    """Active markets с liquidity ≥ $3K, hours ≥ 6, mid ∈ [0.10, 0.90]."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true", "closed": "false",
        "limit": 300,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()

    cutoff = datetime.now(timezone.utc) + timedelta(hours=MIN_HOURS)
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
        if end_dt < cutoff:
            continue
        try:
            liq = float(m.get("liquidity") or 0)
        except Exception:
            liq = 0
        if liq < MIN_LIQ:
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
            "title": m.get("question", "")[:200],
            "yes_price": yes_price,
            "liquidity": liq,
            "end_ts": int(end_dt.timestamp()),
        })
        if len(out) >= limit:
            break
    return out


def append_signal(record: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


async def scan_once(markets_limit: int = 100) -> dict:
    started = time.time()
    markets = await get_eligible_markets(limit=markets_limit)
    manifold_markets = await fetch_manifold_markets(limit=200)
    client = PolymarketDataApiClient()
    now = int(time.time())
    detected = 0
    errors = 0

    for m in markets:
        try:
            # Pump UP detection — fade by hypothetical SELL on YES (would BUY NO)
            yes_hist = await client.fetch_prices_history(m["yes_token"])
            no_hist = await client.fetch_prices_history(m["no_token"])
        except Exception as e:
            errors += 1
            continue

        for token_label, hist, fade_token, fade_label in [
            ("YES", yes_hist, m["no_token"], "NO"),
            ("NO", no_hist, m["yes_token"], "YES"),
        ]:
            if not hist or len(hist) < 5:
                continue
            recent = [p for p in hist if p[0] >= now - 360]
            if len(recent) < 2:
                continue
            t0 = recent[0][0]
            p0 = recent[0][1]
            tN = recent[-1][0]
            pN = recent[-1][1]
            if (tN - t0) < 240:
                continue
            delta = pN - p0
            # Sweet spot 6-10pp empirically (per fade_replay 19 episodes +6.06% median)
            # Below 6pp: marginal edge
            # Above 10pp: real news, pump continues, fade fails (-7.42% median!)
            if delta < PUMP_THRESHOLD_MIN or delta > PUMP_THRESHOLD_MAX:
                continue
            # Pump UP detected on this token → fade by buying opposite token
            target = p0 + 0.5 * delta  # half-reversion (still above p0)
            # Fade entry на opposite token: if YES pumped to 0.6, NO is at 0.4
            # We'd BUY NO at ~0.4 expecting NO to rise to (1 - target) = ~0.5
            opposite_current = 1.0 - pN
            opposite_target = 1.0 - target  # half-reversion of YES = rise of NO
            entry_edge = opposite_target - opposite_current
            if entry_edge < 0.02:
                continue
            # Per [GPT 25]: log Manifold confirmation as feature, not filter
            mf_match = find_manifold_match(m["title"], manifold_markets)
            if mf_match:
                mf_prob = mf_match.get("probability", 0)
                mf_pm_diff = pN - mf_prob if token_label == "YES" else (1 - pN) - mf_prob
                # Check if Manifold has recent activity (proxy: lastBetTime within 60min)
                last_bet = mf_match.get("lastBetTime", 0)
                mf_recent = (now * 1000 - last_bet) < 60 * 60 * 1000 if last_bet else False
                manifold_payload = {
                    "manifold_id": mf_match.get("id"),
                    "manifold_prob": round(mf_prob, 4),
                    "manifold_recent": mf_recent,
                    "match_similarity": round(mf_match.get("_similarity", 0), 3),
                    "pm_minus_mf_diff": round(mf_pm_diff, 4),
                    "external_confirmation": "diverges" if abs(mf_pm_diff) > 0.06 else "agrees",
                }
            else:
                manifold_payload = {"manifold_id": None, "external_confirmation": "no_match"}

            record = {
                "ts": now,
                "market_id": m["condition_id"],
                "title": m["title"],
                "pumped_token": token_label,
                "delta_5m": round(delta, 4),
                "p_start": round(p0, 4),
                "p_now": round(pN, 4),
                "fade_token_id": fade_token,
                "fade_token_label": fade_label,
                "fade_entry_price": round(opposite_current, 4),
                "fade_target_price": round(opposite_target, 4),
                "expected_edge": round(entry_edge, 4),
                "liquidity": m["liquidity"],
                **manifold_payload,
            }
            append_signal(record)
            detected += 1
            logger.info(
                f"fade_signal | market={m['condition_id'][:10]} {token_label} delta={delta:+.3f} "
                f"fade_buy_{fade_label} edge={entry_edge:.3f} | {m['title'][:50]}"
            )
        await asyncio.sleep(0.1)

    return {
        "n_markets": len(markets),
        "detected": detected,
        "errors": errors,
        "elapsed_s": round(time.time() - started, 1),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=100)
    args = parser.parse_args()
    result = asyncio.run(scan_once(args.markets))
    logger.info(f"fade_shadow.scan_complete | {result}")


if __name__ == "__main__":
    main()
