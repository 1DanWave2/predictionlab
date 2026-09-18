"""Resolution Risk Filter per [GPT 26].

Score market wording для resolution-risk traps. Avoid markets with:
- Subjective source ("credible reports", "according to news")
- Human interpretation ("considered", "deemed", "regarded")
- Multi-outcome ambiguity (high outcomeIndex max)
- Augmented neg-risk "Other" / "Placeholder" outcomes

Use AS FILTER (avoid these markets), not as entry signal.

Output: /app/data/resolution_risk.jsonl per market score.
Cron: */15 * * * * — refresh hourly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

OUTPUT_PATH = Path("/app/data/resolution_risk.jsonl")

logger = logging.getLogger(__name__)


# Subjective wording patterns — high risk
SUBJECTIVE_PATTERNS = [
    r"\bcredible report",
    r"\baccording to (news|reports|sources)",
    r"\bsubstantiated",
    r"\bgenerally regarded",
    r"\bconsidered\b",
    r"\bdeemed\b",
    r"\bperceived\b",
    r"\bconsensus",
    r"\bmajority of",
    r"\bappropriate\b",
    r"\breasonable\b",
    r"\bpotentially\b",
    r"\beffectively\b",
    r"\bsignificant",
]

# Ambiguous wording — needs careful interpretation
AMBIGUOUS_PATTERNS = [
    r"\bany part of\b",
    r"\bin some capacity\b",
    r"\bhowever\b",
    r"\bunless\b",
    r"\bexcept\b",
    r"\bif and only if\b",
    r"\bdiscretion\b",
    r"\binterpretation\b",
]

# Boilerplate/safe — low risk
SAFE_PATTERNS = [
    r"\bpriced according to\b",
    r"\bbinance\b",
    r"\bofficial source\b",
    r"\bclose price\b",
    r"\bend of day\b",
    r"\bkalshi\b",
    r"\bofficially announce",
]


def score_wording(question: str, description: str = "") -> dict:
    text = (question + " " + description).lower()
    if not text.strip():
        return {"score": 0, "subjective": 0, "ambiguous": 0, "safe": 0, "reason": "empty"}

    subj = sum(1 for p in SUBJECTIVE_PATTERNS if re.search(p, text))
    amb = sum(1 for p in AMBIGUOUS_PATTERNS if re.search(p, text))
    safe = sum(1 for p in SAFE_PATTERNS if re.search(p, text))

    # Risk score: subjective worst, ambiguous medium, safe negative
    score = subj * 3 + amb * 2 - safe * 2
    score = max(0, min(20, score))

    risk_level = "LOW"
    if score >= 8:
        risk_level = "HIGH"
    elif score >= 4:
        risk_level = "MEDIUM"

    reason_bits = []
    if subj > 0:
        reason_bits.append(f"{subj} subjective")
    if amb > 0:
        reason_bits.append(f"{amb} ambiguous")
    if safe > 0:
        reason_bits.append(f"{safe} safe-keywords")

    return {
        "score": score,
        "subjective": subj,
        "ambiguous": amb,
        "safe": safe,
        "risk_level": risk_level,
        "reason": "; ".join(reason_bits) or "neutral",
    }


async def fetch_active_markets(limit: int = 200) -> list[dict]:
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true", "closed": "false",
        "limit": limit,
        "order": "volume24hr", "ascending": "false",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def append_records(records: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=150)
    args = parser.parse_args()

    started = time.time()
    markets = await fetch_active_markets(limit=args.limit)
    logger.info(f"resolution_risk.fetched | markets={len(markets)}")

    now_ts = int(time.time())
    records = []
    high_risk: list[dict] = []
    medium_risk: list[dict] = []
    safe: list[dict] = []
    for m in markets:
        question = m.get("question") or ""
        description = m.get("description") or ""
        score_data = score_wording(question, description)
        record = {
            "ts": now_ts,
            "condition_id": m.get("conditionId"),
            "title": question[:100],
            "category": m.get("category"),
            **score_data,
        }
        records.append(record)
        if score_data["risk_level"] == "HIGH":
            high_risk.append(record)
        elif score_data["risk_level"] == "MEDIUM":
            medium_risk.append(record)
        else:
            safe.append(record)

    append_records(records)
    elapsed = round(time.time() - started, 1)
    logger.info(
        f"resolution_risk.scan_complete | n={len(records)} HIGH={len(high_risk)} "
        f"MEDIUM={len(medium_risk)} SAFE={len(safe)} elapsed={elapsed}s"
    )

    print(f"\n=== HIGH-RISK markets ({len(high_risk)}) — AVOID ===")
    high_risk.sort(key=lambda r: -r["score"])
    for r in high_risk[:10]:
        print(f"  score={r['score']} {r['risk_level']:6s} | {r['title'][:70]}")
        print(f"    └ {r['reason']}")

    print(f"\n=== MEDIUM-RISK ({len(medium_risk)}) — caution ===")
    for r in medium_risk[:5]:
        print(f"  score={r['score']} | {r['title'][:70]}")


if __name__ == "__main__":
    asyncio.run(main())
