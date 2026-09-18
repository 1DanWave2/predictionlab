"""Cross-Platform Radar per [GPT 24] insight:
  Cross-platform radar can become a Fade-Any filter.
  PM moved 5pp, Manifold did not → STRONG fade candidate
  PM moved 5pp, Manifold also moved → do NOT fade (real news)

Fetches Polymarket markets + Manifold probabilities, matches by keyword,
logs gaps and convergence to /app/data/cross_market_gaps.jsonl.

Run via cron */5 alongside fade_shadow_scanner.

Output записывается parallel с fade signals — at analysis time, fade
с no-confirmation становятся high-confidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


OUTPUT_PATH = Path("/app/data/cross_market_gaps.jsonl")
GAP_THRESHOLD = 0.06  # 6pp — per [GPT 24]
MIN_PERSISTENCE_S = 60

logger = logging.getLogger(__name__)


_STOPWORDS = {"will", "the", "a", "an", "be", "in", "on", "at", "to", "of", "by", "for",
              "is", "are", "have", "has", "do", "does", "did", "this", "that"}


def keywords(title: str) -> set[str]:
    """Extract significant keywords from title для matching."""
    text = title.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [t for t in text.split() if len(t) >= 4 and t not in _STOPWORDS]
    return set(tokens[:8])  # top tokens


def title_similarity(a: str, b: str) -> float:
    """Jaccard similarity на keywords."""
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


async def fetch_polymarket_markets(limit: int = 100) -> list[dict]:
    """Pull active PM markets с decent liquidity."""
    url = "https://gamma-api.polymarket.com/markets"
    params = {"active": "true", "closed": "false", "limit": 200, "order": "volume24hr", "ascending": "false"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()
    cutoff = datetime.now(timezone.utc) + timedelta(hours=2)
    out = []
    for m in markets:
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
            prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
            yes_price = float(prices[0]) if prices else 0.5
        except Exception:
            yes_price = 0.5
        if yes_price < 0.05 or yes_price > 0.95:
            continue
        try:
            liq = float(m.get("liquidity") or 0)
        except Exception:
            liq = 0
        if liq < 1000:
            continue
        out.append({
            "platform": "polymarket",
            "id": m["conditionId"],
            "title": m.get("question", "")[:200],
            "slug": m.get("slug", "")[:200],
            "yes_prob": yes_price,
            "liquidity": liq,
            "end_iso": end_raw,
        })
        if len(out) >= limit:
            break
    return out


async def fetch_manifold_markets(limit: int = 200) -> list[dict]:
    """Pull active binary Manifold markets с probability."""
    url = "https://api.manifold.markets/v0/markets"
    params = {"limit": 200}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        markets = r.json()
    out = []
    for m in markets:
        if m.get("isResolved"):
            continue
        if m.get("outcomeType") != "BINARY":
            continue
        prob = m.get("probability")
        if prob is None or prob < 0.05 or prob > 0.95:
            continue
        liq = float(m.get("totalLiquidity") or 0)
        if liq < 50:  # Manifold liquidity in mana, much lower scale
            continue
        out.append({
            "platform": "manifold",
            "id": m["id"],
            "title": m.get("question", "")[:200],
            "slug": m.get("slug", "")[:200],
            "yes_prob": prob,
            "liquidity": liq,
        })
        if len(out) >= limit:
            break
    return out


def match_markets(pm_markets: list[dict], mf_markets: list[dict], min_sim: float = 0.4) -> list[dict]:
    """Pair PM markets с best-matching Manifold markets by title similarity."""
    matches = []
    for pm in pm_markets:
        best_sim = 0.0
        best_mf = None
        for mf in mf_markets:
            sim = title_similarity(pm["title"], mf["title"])
            if sim > best_sim:
                best_sim = sim
                best_mf = mf
        if best_mf and best_sim >= min_sim:
            gap = pm["yes_prob"] - best_mf["yes_prob"]
            matches.append({
                "ts": int(time.time()),
                "pm_id": pm["id"],
                "pm_title": pm["title"],
                "pm_yes_prob": pm["yes_prob"],
                "mf_id": best_mf["id"],
                "mf_title": best_mf["title"],
                "mf_yes_prob": best_mf["yes_prob"],
                "similarity": round(best_sim, 3),
                "gap_pp": round(gap, 4),
                "abs_gap_pp": round(abs(gap), 4),
            })
    return matches


def append_signals(records: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    started = time.time()
    pm_markets, mf_markets = await asyncio.gather(
        fetch_polymarket_markets(limit=100),
        fetch_manifold_markets(limit=200),
    )
    logger.info(f"radar.fetch | pm={len(pm_markets)} mf={len(mf_markets)}")

    matches = match_markets(pm_markets, mf_markets, min_sim=0.4)
    big_gaps = [m for m in matches if m["abs_gap_pp"] >= GAP_THRESHOLD]

    append_signals(matches)
    elapsed = round(time.time() - started, 1)
    logger.info(
        f"radar.scan_complete | matched={len(matches)} big_gaps={len(big_gaps)} elapsed={elapsed}s"
    )

    # Print top gaps to log for visibility
    if big_gaps:
        print("=== TOP GAPS (≥6pp diff PM↔Manifold) ===")
        big_gaps.sort(key=lambda m: m["abs_gap_pp"], reverse=True)
        for m in big_gaps[:10]:
            print(
                f"  gap={m['gap_pp']:+.3f}  PM={m['pm_yes_prob']:.3f}  MF={m['mf_yes_prob']:.3f}  "
                f"sim={m['similarity']:.2f}  {m['pm_title'][:50]}"
            )


if __name__ == "__main__":
    asyncio.run(main())
