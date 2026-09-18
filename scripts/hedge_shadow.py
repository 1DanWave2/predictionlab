"""Hedge Manager Shadow Simulator per [GPT 28] / refined per [GPT 30].

Refined gates (per [GPT 30]):
  - CLEAN_BASKET: skipped_legs == 0, all legs covered, persists across two fetches
  - INCOMPLETE_BASKET: skipped > 0 → not eligible for live, brutal haircut for research
  - Two-fetch persistence: fetch_1 → wait 25s → fetch_2; require edge ≥ threshold on BOTH
  - Threshold ladder: shadow_display 0.5% / clean_executable 2.0% / live_canary 3.0%
  - Skipped haircut: max(gamma_skipped*2, skipped/total, 0.25)
  - Absurd edge (>20%) flagged as ERROR_DETECTOR (likely phantom)

Pipeline:
  1. Pull latest neg-risk arb candidates (last 30 min)
  2. Fetch gamma events + extract token_ids
  3. Round 1: fetch CLOB books for all legs in parallel
  4. Sleep 25s (PERSISTENCE_GAP_SECONDS)
  5. Round 2: re-fetch CLOB books for same legs
  6. Simulate baskets at K = [1,5,10,25,50,100] for each round
  7. Classify: CLEAN / INCOMPLETE / STALE / ERROR_DETECTOR / DEPTH_TOO_THIN
  8. Track edge_decay, depth_decay, legs_changed across rounds
  9. Output to /app/data/hedge_shadow.jsonl

NO LIVE ORDERS. Pure read-only simulator.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

ARB_INPUT = Path("/app/data/arb_opportunities.jsonl")
SHADOW_OUTPUT = Path("/app/data/hedge_shadow.jsonl")

SHARE_TIERS = [1, 5, 10, 25, 50, 100]  # smaller cap per [GPT 30] sanity
FEE_RATE = 0.000
SLIPPAGE_BUFFER_PCT = 0.005

# Per [GPT 30] threshold ladder
SHADOW_DISPLAY_MIN_EDGE = 0.005   # 0.5% — show on dashboard
CLEAN_EXECUTABLE_MIN_EDGE = 0.02  # 2.0% — eligible for ≥20-count gate
LIVE_CANARY_MIN_EDGE = 0.03       # 3.0% — required for live $1 (not used here, just labeled)
ABSURD_EDGE_FLAG = 0.20           # >20% edge → ERROR_DETECTOR
MAX_TOTAL_BUDGET_USD = 50.0
MAX_PARTIAL_LOSS_USD = 0.25       # worst-case partial loss must be ≤ this for live
MIN_DOLLAR_EDGE_FOR_LIVE = 0.05   # ≥ $0.05 net profit at $1 size

PERSISTENCE_GAP_SECONDS = 25
EDGE_DECAY_TOLERANCE = 0.005  # 0.5pp — if edge drops more than this between rounds → STALE
LEG_PRICE_DRIFT_LIMIT = 0.01  # 1pp — any leg avg drifting > this → STALE

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_recent_arb_candidates(max_age_seconds: int = 1800) -> list[dict]:
    if not ARB_INPUT.exists():
        return []
    cutoff = int(time.time()) - max_age_seconds
    by_event: dict[str, dict] = {}
    with ARB_INPUT.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            if not rec.get("arb_signal"):
                continue
            eid = rec.get("event_id")
            if not eid:
                continue
            if eid not in by_event or rec["ts"] > by_event[eid]["ts"]:
                by_event[eid] = rec
    return list(by_event.values())


async def fetch_event(client: httpx.AsyncClient, event_id: str) -> dict | None:
    try:
        r = await client.get(f"{GAMMA_EVENT_URL}/{event_id}", timeout=15.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning("event_fetch_failed | event_id=%s err=%s", event_id, exc)
        return None


_BOOK_SEMAPHORE = asyncio.Semaphore(15)  # cap parallelism to avoid CLOB rate limits


# Rate-limit telemetry per [GPT 32] — accumulates across one run, reset before main()
_RATE_STATS: dict = {
    "attempts": 0,
    "success": 0,
    "http_429": 0,
    "http_other_error": 0,
    "timeout": 0,
    "latencies_ms": [],
}


def _reset_rate_stats() -> None:
    _RATE_STATS["attempts"] = 0
    _RATE_STATS["success"] = 0
    _RATE_STATS["http_429"] = 0
    _RATE_STATS["http_other_error"] = 0
    _RATE_STATS["timeout"] = 0
    _RATE_STATS["latencies_ms"] = []


def _rate_summary() -> dict:
    lats = sorted(_RATE_STATS["latencies_ms"])
    n = len(lats)

    def pct(p):
        if not lats:
            return 0
        idx = max(0, min(n - 1, int(p * n / 100)))
        return lats[idx]

    return {
        "attempts": _RATE_STATS["attempts"],
        "success": _RATE_STATS["success"],
        "http_429": _RATE_STATS["http_429"],
        "http_other_error": _RATE_STATS["http_other_error"],
        "timeout": _RATE_STATS["timeout"],
        "median_latency_ms": pct(50),
        "p95_latency_ms": pct(95),
        "p99_latency_ms": pct(99),
        "max_latency_ms": lats[-1] if lats else 0,
    }


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    async with _BOOK_SEMAPHORE:
        _RATE_STATS["attempts"] += 1
        t_start = time.time()
        try:
            r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=15.0)
            elapsed_ms = int((time.time() - t_start) * 1000)
            _RATE_STATS["latencies_ms"].append(elapsed_ms)
            if r.status_code == 429:
                _RATE_STATS["http_429"] += 1
                return None
            r.raise_for_status()
            _RATE_STATS["success"] += 1
            return r.json()
        except httpx.TimeoutException:
            _RATE_STATS["timeout"] += 1
            return None
        except Exception:
            _RATE_STATS["http_other_error"] += 1
            return None


def _gamma_yes_price(market: dict) -> float | None:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return float(prices[0])
    except Exception:
        pass
    return None


def extract_token_ids(market: dict) -> tuple[str | None, str | None]:
    raw = market.get("clobTokenIds")
    if not raw:
        return (None, None)
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if len(ids) >= 2:
            return (str(ids[0]), str(ids[1]))
    except Exception:
        pass
    return (None, None)


# ──────────────────────────────────────────────────────────────────────────────
# Book walk + basket simulation (shares-based)
# ──────────────────────────────────────────────────────────────────────────────

def walk_book_buy_shares(asks: list[dict], target_shares: float) -> dict:
    sorted_asks = sorted(asks, key=lambda a: float(a.get("price", 0)))
    filled_s = 0.0
    cost_d = 0.0
    levels = 0
    for level in sorted_asks:
        price = float(level.get("price", 0) or 0)
        size = float(level.get("size", 0) or 0)
        if price <= 0 or size <= 0:
            continue
        remaining_shares = target_shares - filled_s
        if remaining_shares <= 0:
            break
        if size <= remaining_shares:
            filled_s += size
            cost_d += price * size
            levels += 1
        else:
            cost_d += price * remaining_shares
            filled_s += remaining_shares
            levels += 1
            break
    avg_price = (cost_d / filled_s) if filled_s > 0 else 0.0
    return {
        "filled_shares": round(filled_s, 4),
        "cost_dollars": round(cost_d, 4),
        "avg_price": round(avg_price, 4),
        "levels_used": levels,
        "fully_filled": filled_s >= target_shares - 0.01,
    }


def simulate_basket_shares(legs: list[dict], shares_per_leg: float) -> dict:
    leg_results = []
    total_cost = 0.0
    fully_filled_legs = 0
    for leg in legs:
        walk = walk_book_buy_shares(leg["asks"], shares_per_leg)
        leg_results.append({
            "market_id": leg["market_id"],
            **walk,
        })
        total_cost += walk["cost_dollars"]
        if walk["fully_filled"]:
            fully_filled_legs += 1
    return {
        "legs": leg_results,
        "total_cost": round(total_cost, 4),
        "shares_per_leg_target": shares_per_leg,
        "fully_filled_legs": fully_filled_legs,
        "n_legs": len(legs),
    }


def basket_payout_per_share(side: str, n_legs: int) -> float:
    if side == "NO":
        return float(n_legs - 1)
    return 1.0


def compute_skipped_haircut(prob_gamma_skipped: float, n_skipped: int, n_total: int) -> float:
    """Per [GPT 30]: max(gamma*2, skipped/total, 0.25) when skipped > 0."""
    if n_skipped == 0:
        return 0.0
    haircut = max(
        prob_gamma_skipped * 2.0,
        n_skipped / max(1, n_total),
        0.25,
    )
    return min(1.0, haircut)


def compute_basket_edge(
    basket: dict,
    side: str,
    haircut: float,
) -> dict:
    """Compute risk-adjusted edge for one round at one tier."""
    n_legs = basket["n_legs"]
    K = basket["shares_per_leg_target"]
    cost = basket["total_cost"]
    filled_legs = basket["fully_filled_legs"]
    naive_payout = basket_payout_per_share(side, n_legs) * K

    if side == "YES":
        risk_adj_payout = (1.0 - haircut) * naive_payout
    else:
        # NO basket: if winner is among skipped, ALL n_legs NOs win = K*n_legs
        # if winner is in covered, payout = K*(n_legs-1)
        risk_adj_payout = (1.0 - haircut) * naive_payout + haircut * K * float(n_legs)

    gross_profit = risk_adj_payout - cost
    slippage = cost * SLIPPAGE_BUFFER_PCT
    net_profit = gross_profit - cost * FEE_RATE - slippage
    edge_per_dollar = net_profit / cost if cost > 0 else 0.0

    return {
        "shares_per_leg": K,
        "n_legs": n_legs,
        "fully_filled_legs": filled_legs,
        "actual_basket_cost": round(cost, 4),
        "naive_payout": round(naive_payout, 4),
        "risk_adj_payout": round(risk_adj_payout, 4),
        "gross_profit": round(gross_profit, 4),
        "net_profit": round(net_profit, 4),
        "edge_per_dollar": round(edge_per_dollar, 4),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Two-round fetch + classification pipeline
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_leg_books(client: httpx.AsyncClient, market_token_pairs: list[tuple]) -> dict[str, dict]:
    """Fetch book for each (market, token_id). Return {market_id: {asks, bids} or None}."""
    tasks = [fetch_book(client, t[1]) for t in market_token_pairs]
    books = await asyncio.gather(*tasks)
    out: dict[str, dict] = {}
    for (m, _), book in zip(market_token_pairs, books):
        out[str(m.get("id"))] = book or {}
    return out


def assemble_legs(market_token_pairs: list[tuple], books_by_mid: dict) -> tuple[list[dict], list[dict]]:
    """Return (legs_ready, skipped). Each ready leg has asks + bids."""
    legs: list[dict] = []
    skipped: list[dict] = []
    for m, token_id in market_token_pairs:
        mid = str(m.get("id"))
        book = books_by_mid.get(mid) or {}
        asks = book.get("asks") or []
        if not asks:
            skipped.append({
                "market_id": mid,
                "reason": "book_fetch_failed" if not book else "no_asks",
                "yes_price": _gamma_yes_price(m),
            })
            continue
        legs.append({
            "market_id": mid,
            "market_question": (m.get("question") or "")[:80],
            "token_id": token_id,
            "asks": asks,
            "bids": book.get("bids") or [],
        })
    return legs, skipped


def best_tier(legs: list[dict], side: str, haircut: float, threshold: float) -> dict | None:
    """Find the tier that maximizes net_profit subject to edge ≥ threshold + budget."""
    best = None
    for K in SHARE_TIERS:
        basket = simulate_basket_shares(legs, K)
        if basket["fully_filled_legs"] != basket["n_legs"]:
            continue
        if basket["total_cost"] > MAX_TOTAL_BUDGET_USD:
            continue
        edge = compute_basket_edge(basket, side, haircut)
        if edge["edge_per_dollar"] < threshold:
            continue
        if best is None or edge["net_profit"] > best["net_profit"]:
            best = edge
    return best


def per_leg_drift(legs_r1: list[dict], legs_r2: list[dict], K: int) -> dict:
    """Per [GPT 32]: log per-leg drift between rounds + classify edge_decay_reason.

    For each market_id present in both rounds, compute:
      - avg_price_r1, avg_price_r2 at K shares
      - depth at top-of-book r1, r2
      - price_drift_pp, depth_drift
    Aggregate to summary metrics for dashboard.
    """
    by_mid_r1 = {l["market_id"]: l for l in legs_r1}
    by_mid_r2 = {l["market_id"]: l for l in legs_r2}
    all_mids = set(by_mid_r1) | set(by_mid_r2)

    per_leg: list[dict] = []
    for mid in all_mids:
        l1 = by_mid_r1.get(mid)
        l2 = by_mid_r2.get(mid)
        if l1 is None and l2 is None:
            continue
        # Walk K shares on each round
        w1 = walk_book_buy_shares(l1["asks"], K) if l1 else None
        w2 = walk_book_buy_shares(l2["asks"], K) if l2 else None
        # Depth at best ask = first level size (post-sort ascending)
        depth_r1 = float(sorted(l1["asks"], key=lambda a: float(a.get("price", 0)))[0].get("size", 0)) if l1 and l1["asks"] else 0
        depth_r2 = float(sorted(l2["asks"], key=lambda a: float(a.get("price", 0)))[0].get("size", 0)) if l2 and l2["asks"] else 0
        avg_r1 = w1["avg_price"] if w1 else None
        avg_r2 = w2["avg_price"] if w2 else None
        drift_pp = (avg_r2 - avg_r1) * 100 if avg_r1 is not None and avg_r2 is not None else None
        per_leg.append({
            "market_id": mid,
            "leg_ready_r1": l1 is not None,
            "leg_ready_r2": l2 is not None,
            "avg_price_r1": round(avg_r1, 4) if avg_r1 else None,
            "avg_price_r2": round(avg_r2, 4) if avg_r2 else None,
            "depth_r1": depth_r1,
            "depth_r2": depth_r2,
            "price_drift_pp": round(drift_pp, 3) if drift_pp is not None else None,
            "depth_drift": depth_r2 - depth_r1,
        })

    # Summary metrics
    drifts = [l["price_drift_pp"] for l in per_leg if l["price_drift_pp"] is not None]
    legs_lost = sum(1 for l in per_leg if l["leg_ready_r1"] and not l["leg_ready_r2"])
    depth_decays = [
        (l["depth_r2"] - l["depth_r1"]) / max(l["depth_r1"], 1)
        for l in per_leg if l["depth_r1"] > 0
    ]

    if drifts:
        worst_drift = max(drifts, key=abs)
        median_drift = sorted(drifts)[len(drifts) // 2]
    else:
        worst_drift = 0
        median_drift = 0

    # Edge decay reason
    reason = "stable"
    if legs_lost > 0:
        reason = "leg_lost"
    elif abs(worst_drift) > 1.0:
        reason = "price_moved"
    elif depth_decays and min(depth_decays) < -0.5:
        reason = "depth_disappeared"
    elif drifts:
        reason = "mixed"

    return {
        "per_leg": per_leg,
        "worst_leg_drift_pp": round(worst_drift, 3),
        "median_leg_drift_pp": round(median_drift, 3),
        "legs_lost_between_rounds": legs_lost,
        "median_depth_decay_pct": round(sorted(depth_decays)[len(depth_decays) // 2] * 100, 2) if depth_decays else 0,
        "edge_decay_reason": reason,
    }


def classify_candidate(
    n_skipped: int,
    n_total_markets: int,
    haircut: float,
    round1_best: dict | None,
    round2_best: dict | None,
) -> tuple[str, dict]:
    """Apply [GPT 30] decision tree → (status, reason_dict)."""
    if round1_best is None and round2_best is None:
        return "NOT_EXECUTABLE", {"reason": "no_tier_meets_min_edge"}

    edge1 = round1_best["edge_per_dollar"] if round1_best else 0.0
    edge2 = round2_best["edge_per_dollar"] if round2_best else 0.0
    decay = round(edge1 - edge2, 4)

    if max(edge1, edge2) >= ABSURD_EDGE_FLAG:
        return "ERROR_DETECTOR", {
            "reason": f"absurd_edge>={ABSURD_EDGE_FLAG*100:.0f}%",
            "edge_round_1": round(edge1, 4),
            "edge_round_2": round(edge2, 4),
        }

    if round1_best is None or round2_best is None:
        return "STALE_EDGE", {
            "reason": "edge_only_one_round",
            "edge_round_1": round(edge1, 4),
            "edge_round_2": round(edge2, 4),
        }

    if decay > EDGE_DECAY_TOLERANCE:
        return "STALE_EDGE", {
            "reason": f"edge_decay={decay*100:.2f}pp > tolerance",
            "edge_round_1": round(edge1, 4),
            "edge_round_2": round(edge2, 4),
            "edge_decay": decay,
        }

    if n_skipped > 0:
        return "INCOMPLETE_BASKET", {
            "reason": "n_skipped>0 — not eligible for live, model-only",
            "skipped_haircut": round(haircut, 4),
            "edge_round_1": round(edge1, 4),
            "edge_round_2": round(edge2, 4),
        }

    if edge2 < CLEAN_EXECUTABLE_MIN_EDGE:
        return "EDGE_TOO_THIN", {
            "reason": f"edge<{CLEAN_EXECUTABLE_MIN_EDGE*100:.0f}% on round_2",
            "edge_round_1": round(edge1, 4),
            "edge_round_2": round(edge2, 4),
        }

    return "CLEAN_EXECUTABLE", {
        "edge_round_1": round(edge1, 4),
        "edge_round_2": round(edge2, 4),
        "edge_decay": decay,
        "live_eligible": (
            edge2 >= LIVE_CANARY_MIN_EDGE
            and round2_best["net_profit"] >= MIN_DOLLAR_EDGE_FOR_LIVE
        ),
    }


async def shadow_one_candidate(
    client: httpx.AsyncClient,
    candidate: dict,
    round1_books_by_mid: dict,
    round2_books_by_mid: dict,
    market_token_pairs: list[tuple],
    side: str,
    n_total_markets: int,
) -> dict:
    """Process one candidate using already-fetched books."""
    state_log: list[dict] = []

    def log_state(state: str, **kw) -> None:
        state_log.append({"state": state, **kw, "t": round(time.time(), 2)})

    log_state("VALIDATING", title=candidate.get("title", "")[:60])

    legs_r1, skipped_r1 = assemble_legs(market_token_pairs, round1_books_by_mid)
    legs_r2, skipped_r2 = assemble_legs(market_token_pairs, round2_books_by_mid)

    # Use intersection: legs ready in BOTH rounds (per GPT 30: legs_changed should be 0)
    r1_mids = {l["market_id"] for l in legs_r1}
    r2_mids = {l["market_id"] for l in legs_r2}
    common_mids = r1_mids & r2_mids
    legs_changed = len(r1_mids ^ r2_mids)

    legs_r1 = [l for l in legs_r1 if l["market_id"] in common_mids]
    legs_r2 = [l for l in legs_r2 if l["market_id"] in common_mids]
    legs_r2.sort(key=lambda l: [m["market_id"] for m in legs_r1].index(l["market_id"]))

    n_legs_ready = len(legs_r1)
    n_skipped = n_total_markets - n_legs_ready

    log_state(
        "BOOK_FETCH_DONE",
        legs_ready=n_legs_ready,
        n_skipped=n_skipped,
        legs_changed=legs_changed,
        n_total_markets=n_total_markets,
    )

    if n_legs_ready < 2:
        log_state("DEPTH_TOO_THIN", reason="too_few_legs")
        return {
            "event_id": candidate["event_id"],
            "title": candidate.get("title", "")[:80],
            "ts": int(time.time()),
            "side": side,
            "n_total_markets": n_total_markets,
            "n_legs_ready": n_legs_ready,
            "n_skipped": n_skipped,
            "legs_changed": legs_changed,
            "status": "DEPTH_TOO_THIN",
            "reason": "too_few_legs",
            "skipped_sample": (skipped_r1 or skipped_r2)[:5],
            "state_log": state_log,
        }

    # Skipped probability haircut
    prob_gamma_skipped = sum(
        s.get("yes_price") or 0 for s in skipped_r1
    ) if side == "YES" else sum(
        max(0, 1 - (s.get("yes_price") or 0)) for s in skipped_r1
    )
    prob_gamma_skipped = max(0.0, min(1.0, prob_gamma_skipped))
    haircut = compute_skipped_haircut(prob_gamma_skipped, n_skipped, n_total_markets)

    log_state("SIM_START", n_legs=n_legs_ready, haircut=round(haircut, 4))

    # Find best tier on each round at the SHADOW threshold (so we capture decay)
    threshold_for_search = SHADOW_DISPLAY_MIN_EDGE
    best_r1 = best_tier(legs_r1, side, haircut, threshold_for_search)
    best_r2 = best_tier(legs_r2, side, haircut, threshold_for_search)

    # If best tier differs, force re-evaluation at the same K (use round_1 K on round_2)
    if best_r1 and best_r2 and best_r1["shares_per_leg"] != best_r2["shares_per_leg"]:
        K_r1 = best_r1["shares_per_leg"]
        basket_r2_at_K = simulate_basket_shares(legs_r2, K_r1)
        if basket_r2_at_K["fully_filled_legs"] == basket_r2_at_K["n_legs"]:
            best_r2 = compute_basket_edge(basket_r2_at_K, side, haircut)

    status, status_data = classify_candidate(
        n_skipped, n_total_markets, haircut, best_r1, best_r2
    )

    # Per-leg drift telemetry (per [GPT 32] Q2). Use K from best_r2 if available.
    K_for_drift = (best_r2 or best_r1 or {}).get("shares_per_leg", 10)
    drift = per_leg_drift(legs_r1, legs_r2, K_for_drift)

    log_state(
        "SIM_DONE",
        status=status,
        worst_drift_pp=drift["worst_leg_drift_pp"],
        edge_decay_reason=drift["edge_decay_reason"],
        **{k: v for k, v in status_data.items() if k != "reason"},
    )
    log_state("DONE")

    # FIX-3 [Claude 47]: surface aggregated hedge_pnl at top level.
    # Was: pnl_24h=$0 for 6,538 events — net_profit was buried in round_1/round_2.
    r1_pnl = (best_r1 or {}).get("net_profit", 0.0) if isinstance(best_r1, dict) else 0.0
    r2_pnl = (best_r2 or {}).get("net_profit", 0.0) if isinstance(best_r2, dict) else 0.0
    hedge_pnl = (r1_pnl or 0.0) + (r2_pnl or 0.0)
    return {
        "event_id": candidate["event_id"],
        "title": candidate.get("title", "")[:80],
        "ts": int(time.time()),
        "side": side,
        "n_total_markets": n_total_markets,
        "n_legs_ready": n_legs_ready,
        "n_skipped": n_skipped,
        "legs_changed": legs_changed,
        "skipped_haircut": round(haircut, 4),
        "claimed_edge_pp": candidate.get("edge_pp"),
        "claimed_sum_yes": candidate.get("sum_yes"),
        "round_1": best_r1,
        "round_2": best_r2,
        "r1_net_profit": round(r1_pnl, 4),
        "r2_net_profit": round(r2_pnl, 4),
        "hedge_pnl": round(hedge_pnl, 4),
        "status": status,
        "status_data": status_data,
        "drift_summary": {
            "worst_leg_drift_pp": drift["worst_leg_drift_pp"],
            "median_leg_drift_pp": drift["median_leg_drift_pp"],
            "legs_lost_between_rounds": drift["legs_lost_between_rounds"],
            "median_depth_decay_pct": drift["median_depth_decay_pct"],
            "edge_decay_reason": drift["edge_decay_reason"],
        },
        "drift_per_leg": drift["per_leg"][:20],  # cap on disk per record
        "skipped_sample": skipped_r1[:5],
        "state_log": state_log,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main loop with two fetch rounds
# ──────────────────────────────────────────────────────────────────────────────

def append_records(records: list[dict]) -> None:
    SHADOW_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_OUTPUT.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-min", type=int, default=30)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--gap-seconds", type=int, default=PERSISTENCE_GAP_SECONDS)
    args = parser.parse_args()

    started = time.time()
    _reset_rate_stats()
    candidates = load_recent_arb_candidates(max_age_seconds=args.max_age_min * 60)
    candidates.sort(key=lambda c: -abs(c.get("deviation", 0)))
    candidates = candidates[: args.limit]
    logger.info("hedge_shadow.start | candidates=%d gap=%ds", len(candidates), args.gap_seconds)

    if not candidates:
        logger.info("hedge_shadow.no_candidates")
        return

    async with httpx.AsyncClient() as client:
        # ─── Phase 0: refetch events to get latest market lists + token_ids ───
        events = await asyncio.gather(*[fetch_event(client, c["event_id"]) for c in candidates])

        candidate_legs: list[dict] = []  # one entry per candidate
        for c, ev in zip(candidates, events):
            if not ev:
                candidate_legs.append({"candidate": c, "event": None})
                continue
            markets = ev.get("markets") or []
            side = "YES" if c.get("arb_signal") == "BUY_YES_basket" else "NO"
            pairs = []
            for m in markets:
                yes_id, no_id = extract_token_ids(m)
                token_id = yes_id if side == "YES" else no_id
                if token_id:
                    pairs.append((m, token_id))
            candidate_legs.append({
                "candidate": c,
                "event": ev,
                "side": side,
                "pairs": pairs,
                "n_total_markets": len(markets),
            })

        # ─── Phase 1: book fetch round 1 (parallel across all candidates' legs) ───
        round1_per_candidate: list[dict] = []
        all_round1_tasks = []
        boundaries: list[tuple[int, int]] = []
        cursor = 0
        for cl in candidate_legs:
            if cl.get("event") is None:
                boundaries.append((cursor, cursor))
                continue
            n = len(cl["pairs"])
            boundaries.append((cursor, cursor + n))
            cursor += n
            for _, t in cl["pairs"]:
                all_round1_tasks.append(fetch_book(client, t))

        logger.info("phase1.book_fetch_round_1 | tokens=%d", len(all_round1_tasks))
        all_round1_books = await asyncio.gather(*all_round1_tasks)

        for cl, (start, end) in zip(candidate_legs, boundaries):
            if cl.get("event") is None:
                round1_per_candidate.append({})
                continue
            books_for_this = all_round1_books[start:end]
            books_by_mid: dict[str, dict] = {}
            for (m, _), book in zip(cl["pairs"], books_for_this):
                books_by_mid[str(m.get("id"))] = book or {}
            round1_per_candidate.append(books_by_mid)

        # ─── Phase 2: persistence gap ───
        logger.info("phase2.persistence_gap | sleeping %ds", args.gap_seconds)
        await asyncio.sleep(args.gap_seconds)

        # ─── Phase 3: book fetch round 2 (same tokens) ───
        all_round2_tasks = []
        for cl in candidate_legs:
            if cl.get("event") is None:
                continue
            for _, t in cl["pairs"]:
                all_round2_tasks.append(fetch_book(client, t))

        logger.info("phase3.book_fetch_round_2 | tokens=%d", len(all_round2_tasks))
        all_round2_books = await asyncio.gather(*all_round2_tasks)

        round2_per_candidate: list[dict] = []
        for cl, (start, end) in zip(candidate_legs, boundaries):
            if cl.get("event") is None:
                round2_per_candidate.append({})
                continue
            books_for_this = all_round2_books[start:end]
            books_by_mid: dict[str, dict] = {}
            for (m, _), book in zip(cl["pairs"], books_for_this):
                books_by_mid[str(m.get("id"))] = book or {}
            round2_per_candidate.append(books_by_mid)

        # ─── Phase 4: classify each candidate ───
        results: list[dict] = []
        for cl, r1_books, r2_books in zip(candidate_legs, round1_per_candidate, round2_per_candidate):
            if cl.get("event") is None:
                results.append({
                    "event_id": cl["candidate"]["event_id"],
                    "ts": int(time.time()),
                    "status": "EVENT_FETCH_FAILED",
                    "title": cl["candidate"].get("title", "")[:80],
                })
                continue
            try:
                r = await shadow_one_candidate(
                    client=client,
                    candidate=cl["candidate"],
                    round1_books_by_mid=r1_books,
                    round2_books_by_mid=r2_books,
                    market_token_pairs=cl["pairs"],
                    side=cl["side"],
                    n_total_markets=cl["n_total_markets"],
                )
                results.append(r)
            except Exception as exc:
                logger.warning("classify_failed | event=%s err=%s", cl["candidate"].get("event_id"), exc)
                results.append({
                    "event_id": cl["candidate"]["event_id"],
                    "ts": int(time.time()),
                    "status": "ERROR",
                    "error": str(exc)[:120],
                })

    elapsed = round(time.time() - started, 1)
    rate = _rate_summary()

    # Append a single telemetry record at the end of each run (per [GPT 32])
    telemetry_record = {
        "ts": int(time.time()),
        "kind": "rate_telemetry",
        "elapsed_s": elapsed,
        "candidates": len(candidates),
        **rate,
    }
    results.append(telemetry_record)
    append_records(results)

    by_status: dict[str, int] = {}
    for r in results:
        if r.get("kind") == "rate_telemetry":
            continue
        by_status[r.get("status", "UNKNOWN")] = by_status.get(r.get("status", "UNKNOWN"), 0) + 1

    logger.info("hedge_shadow.done | total=%d elapsed=%ss | %s", len(results) - 1, elapsed,
                " ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    logger.info(
        "rate_limit | attempts=%d success=%d 429=%d timeout=%d other=%d median_ms=%d p95_ms=%d",
        rate["attempts"], rate["success"], rate["http_429"], rate["timeout"],
        rate["http_other_error"], rate["median_latency_ms"], rate["p95_latency_ms"],
    )

    print(f"\n=== Hedge Shadow Results ({len(results)} candidates) ===\n")
    print(f"{'event':<8} {'side':<5} {'status':<22} {'edge1':<8} {'edge2':<8} {'cost':<8} {'profit':<8} title")
    for r in results[:30]:
        eid = (r.get("event_id") or "")[:7]
        title = (r.get("title") or "")[:42]
        status = r.get("status", "?")
        side = r.get("side", "")
        r1 = r.get("round_1") or {}
        r2 = r.get("round_2") or {}
        e1 = r1.get("edge_per_dollar")
        e2 = r2.get("edge_per_dollar")
        cost = r2.get("actual_basket_cost") or r1.get("actual_basket_cost")
        profit = r2.get("net_profit") or r1.get("net_profit")
        e1_str = f"{e1*100:.2f}" if e1 is not None else "--"
        e2_str = f"{e2*100:.2f}" if e2 is not None else "--"
        cost_str = f"${cost:.2f}" if cost is not None else "--"
        profit_str = f"${profit:.3f}" if profit is not None else "--"
        print(f"{eid:<8} {side:<5} {status:<22} {e1_str:<8} {e2_str:<8} {cost_str:<8} {profit_str:<8} {title}")

    clean = [r for r in results if r.get("status") == "CLEAN_EXECUTABLE"]
    incomplete = [r for r in results if r.get("status") == "INCOMPLETE_BASKET"]
    print(f"\nCLEAN_EXECUTABLE: {len(clean)}/{len(results)} (target ≥20 per [GPT 30])")
    print(f"INCOMPLETE_BASKET: {len(incomplete)} (research only, not eligible for live)")


if __name__ == "__main__":
    asyncio.run(main())
