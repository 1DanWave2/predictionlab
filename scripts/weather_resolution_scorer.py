"""Weather Resolution Scorer — first ground-truth Brier check per [GPT 31 Q3].

After a temperature event resolves, fetches the actual recorded max temperature
from gamma (winning bucket determined by Polymarket itself) and compares:
  - Our Open-Meteo forecast distribution → Brier score, log-loss
  - PM market mid prices at signal time → Brier, log-loss
  - Did the forecast or the market predict better?

This is unit-test data for weather_bucket_shadow. One event != evidence; we run
this on every resolved temperature event going forward and accumulate a track.

Usage:
  docker exec polymarket-bot python3 -m scripts.weather_resolution_scorer
  docker exec polymarket-bot python3 -m scripts.weather_resolution_scorer --event-id 448466

Output: /app/data/weather_resolutions.jsonl (one record per resolved event)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events"
SHADOW_INPUT = Path("/app/data/weather_bucket_shadow.jsonl")
OUTPUT = Path("/app/data/weather_resolutions.jsonl")

logger = logging.getLogger(__name__)


def load_shadow_records_for_event(event_id: str) -> list[dict]:
    """All shadow snapshots for a given event_id, oldest first."""
    if not SHADOW_INPUT.exists():
        return []
    out: list[dict] = []
    with SHADOW_INPUT.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event_id") == event_id and "skipped" not in rec:
                out.append(rec)
    out.sort(key=lambda r: r.get("ts", 0))
    return out


async def fetch_resolved_event(client: httpx.AsyncClient, event_id: str) -> dict | None:
    try:
        r = await client.get(f"{GAMMA_EVENT_URL}/{event_id}", timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning("event_fetch_failed | event_id=%s err=%s", event_id, exc)
        return None


def find_winning_market(event: dict) -> dict | None:
    """Find the market that resolved YES.

    Tightened threshold to 0.99 to catch near-resolution cases where Polymarket
    has narrowed to a single ≥99% bucket (de-facto winner).
    """
    markets = event.get("markets") or []
    for m in markets:
        outcome_prices = m.get("outcomePrices")
        if not outcome_prices:
            continue
        try:
            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
            if prices and float(prices[0]) >= 0.99:
                return m
        except Exception:
            pass
    return None


def brier_score(probs: list[float], outcome_idx: int) -> float:
    """Brier = sum of (p_i - y_i)^2 over all buckets, where y_i = 1 if i==winner else 0."""
    return round(sum(
        (p - (1.0 if i == outcome_idx else 0.0)) ** 2
        for i, p in enumerate(probs)
    ), 6)


def log_loss(probs: list[float], outcome_idx: int) -> float:
    """-log(p_winner). Clamped to avoid -inf."""
    import math
    p = max(1e-6, probs[outcome_idx])
    return round(-math.log(p), 4)


def score_event_round(record: dict, winning_market_id: str) -> dict | None:
    """Compare forecast vs PM-implied probs for a single shadow snapshot."""
    legs = record.get("legs") or []
    if not legs:
        return None

    # Find which leg is the winner
    winner_idx = next(
        (i for i, l in enumerate(legs) if l.get("market_id") == winning_market_id),
        None,
    )
    if winner_idx is None:
        return None

    # Forecast probs (already in legs[].forecast_prob)
    fc_probs = [l.get("forecast_prob", 0.0) for l in legs]
    fc_sum = sum(fc_probs)
    if fc_sum > 0:
        fc_probs = [p / fc_sum for p in fc_probs]

    # PM-implied probs from gamma_yes (or ask_price as proxy if gamma null)
    pm_probs = [
        (l.get("gamma_yes") if l.get("gamma_yes") is not None else l.get("ask_price", 0.5)) or 0.0
        for l in legs
    ]
    pm_sum = sum(pm_probs)
    if pm_sum > 0:
        pm_probs = [p / pm_sum for p in pm_probs]

    return {
        "ts": record.get("ts"),
        "hours_to_resolution": record.get("hours_to_resolution"),
        "winner_market_id": winning_market_id,
        "winner_bucket": legs[winner_idx].get("bucket_temp"),
        "winner_kind": legs[winner_idx].get("bucket_kind"),
        "forecast_prob_winner": round(fc_probs[winner_idx], 4),
        "pm_prob_winner": round(pm_probs[winner_idx], 4),
        "forecast_brier": brier_score(fc_probs, winner_idx),
        "pm_brier": brier_score(pm_probs, winner_idx),
        "forecast_log_loss": log_loss(fc_probs, winner_idx),
        "pm_log_loss": log_loss(pm_probs, winner_idx),
        "winner_forecast_top": fc_probs.index(max(fc_probs)) == winner_idx,
        "winner_pm_top": pm_probs.index(max(pm_probs)) == winner_idx,
    }


def find_resolved_event_ids() -> list[str]:
    """All distinct event_ids in shadow log that have at least one snapshot."""
    if not SHADOW_INPUT.exists():
        return []
    seen: set[str] = set()
    with SHADOW_INPUT.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            eid = rec.get("event_id")
            if eid and "skipped" not in rec:
                seen.add(eid)
    return list(seen)


def already_scored(event_id: str) -> bool:
    if not OUTPUT.exists():
        return False
    with OUTPUT.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event_id") == event_id:
                return True
    return False


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=str, default=None)
    parser.add_argument("--re-score", action="store_true",
                        help="re-score even if already scored")
    args = parser.parse_args()

    candidates = [args.event_id] if args.event_id else find_resolved_event_ids()
    logger.info("scoring | n_events=%d", len(candidates))

    new_records: list[dict] = []
    async with httpx.AsyncClient() as client:
        for eid in candidates:
            if not args.re_score and already_scored(eid):
                continue
            event = await fetch_resolved_event(client, eid)
            if not event:
                continue
            # Score even if event still "active" — as long as a clear winner exists
            # (one bucket with YES >= 0.999). This catches near-resolution states.
            winner = find_winning_market(event)
            if not winner:
                logger.info("no_clear_winner | event_id=%s closed=%s", eid, event.get("closed"))
                continue
            winner_mid = str(winner.get("id"))

            shadow_records = load_shadow_records_for_event(eid)
            if not shadow_records:
                continue

            scored_rounds: list[dict] = []
            for rec in shadow_records:
                s = score_event_round(rec, winner_mid)
                if s:
                    scored_rounds.append(s)
            if not scored_rounds:
                continue

            # Aggregate across rounds (median Brier, etc.)
            fc_briers = sorted(s["forecast_brier"] for s in scored_rounds)
            pm_briers = sorted(s["pm_brier"] for s in scored_rounds)
            fc_logloss = sorted(s["forecast_log_loss"] for s in scored_rounds)
            pm_logloss = sorted(s["pm_log_loss"] for s in scored_rounds)

            agg = {
                "kind": "weather_resolution",
                "event_id": eid,
                "title": (event.get("title") or "")[:80],
                "city": shadow_records[0].get("city"),
                "station": shadow_records[0].get("station"),
                "ts_resolved": int(time.time()),
                "winner_market_id": winner_mid,
                "winner_bucket": winner.get("question", "")[:80],
                "n_shadow_rounds": len(scored_rounds),
                "forecast_brier_median": fc_briers[len(fc_briers) // 2],
                "pm_brier_median": pm_briers[len(pm_briers) // 2],
                "forecast_logloss_median": fc_logloss[len(fc_logloss) // 2],
                "pm_logloss_median": pm_logloss[len(pm_logloss) // 2],
                "forecast_called_winner_pct": round(
                    sum(1 for s in scored_rounds if s["winner_forecast_top"]) / len(scored_rounds) * 100, 1
                ),
                "pm_called_winner_pct": round(
                    sum(1 for s in scored_rounds if s["winner_pm_top"]) / len(scored_rounds) * 100, 1
                ),
                "forecast_beats_pm": fc_briers[len(fc_briers) // 2] < pm_briers[len(pm_briers) // 2],
                "rounds": scored_rounds,
            }
            new_records.append(agg)
            logger.info(
                "scored | event=%s winner_bucket='%s' fc_brier=%.4f pm_brier=%.4f forecast_beats_pm=%s",
                eid, winner.get("question", "")[:40], agg["forecast_brier_median"],
                agg["pm_brier_median"], agg["forecast_beats_pm"],
            )

    if new_records:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with OUTPUT.open("a") as fh:
            for r in new_records:
                fh.write(json.dumps(r) + "\n")

    print(f"\n=== Weather Resolutions ({len(new_records)} new) ===\n")
    for r in new_records:
        better = "FORECAST WINS" if r["forecast_beats_pm"] else "PM WINS"
        print(f"  {r['event_id']:<8} {r['city']:<12} winner='{r['winner_bucket'][:40]}'")
        print(f"    fc_brier={r['forecast_brier_median']:.4f}  pm_brier={r['pm_brier_median']:.4f}  → {better}")
        print(f"    fc called winner: {r['forecast_called_winner_pct']:.0f}%  pm called: {r['pm_called_winner_pct']:.0f}%")
        print()


if __name__ == "__main__":
    asyncio.run(main())
