"""Paper Tail-Maker Simulator per [GPT 32 H1] / [Claude 35] day-2.

Concept: in [Claude 33] we observed that Tokyo defenders walked tail asks up by
1 tick (e.g., $0.0005 → $0.001) on 7 long-tail buckets. That penny-tax IS the
maker PnL. This script simulates being on the OTHER side of that pattern:

  For each weather event with negRisk markets:
    For each tail bucket (forecast_prob between 0.001-0.05, ask < 0.05):
      "Post" a sim ask 1 tick above current best_ask (e.g. +$0.001)
      Track: would a taker have crossed our price during the next 5/30 min?
      Track: did the bucket resolve YES (we owe $1) or NO (we keep the ask premium)?

We do NOT post real orders. We log "would have filled" outcomes.

Output: /app/data/paper_maker_sim.jsonl
Cron: */15

Pass criteria (per [GPT 32]):
  >= 30 simulated fills
  positive penny_tax_pnl - adverse_selection
  not concentrated in one event (≤30% from a single event)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
TRADES_API = "https://data-api.polymarket.com/trades"

OUTPUT = Path("/app/data/paper_maker_sim.jsonl")
INVENTORY_OUTPUT = Path("/app/data/paper_maker_inventory.jsonl")  # rolling state

# Tail bucket criteria — only quote where forecast_prob is small AND ask is small
MAX_TAIL_ASK = 0.05         # only quote tail buckets cheaper than 5%
MIN_TAIL_DEPTH = 5.0        # at least 5 shares depth at top
TICK_SIZE = 0.001           # PM CLOB minimum increment
QUOTE_OFFSET_TICKS = 1      # we sit 1 tick above current best ask
QUOTE_SIZE_USD = 1.0        # $1 worth of YES per bucket per quote
MIN_OFFER_PRICE = 0.005     # never quote below half a cent (would saturate)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Polymarket helpers (same shape as weather_bucket_shadow)
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_active_temperature_events(client: httpx.AsyncClient, limit: int = 100) -> list[dict]:
    params = {
        "active": "true", "closed": "false",
        "limit": limit, "order": "volume24hr", "ascending": "false",
    }
    try:
        r = await client.get(GAMMA_EVENTS_URL, params=params, timeout=15.0)
        r.raise_for_status()
        events = r.json()
    except Exception as exc:
        logger.warning("events_fetch_failed | err=%s", exc)
        return []
    return [
        e for e in events
        if "temperature" in (e.get("title") or "").lower() and e.get("negRisk")
    ]


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def extract_yes_token(market: dict) -> str | None:
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[0]) if ids else None
    except Exception:
        return None


def gamma_yes(market: dict) -> float | None:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0]) if prices else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-event simulator
# ──────────────────────────────────────────────────────────────────────────────

def parse_bucket_temp(question: str) -> int | None:
    """Best-effort parse of integer °C from market question."""
    if not question:
        return None
    m = re.search(r"\b(\d+)\s*°?c\b", question.lower())
    return int(m.group(1)) if m else None


async def simulate_event(client: httpx.AsyncClient, event: dict, ts: int) -> dict:
    eid = event.get("id")
    title = (event.get("title") or "")[:80]
    markets = event.get("markets") or []
    if len(markets) < 3:
        return {"event_id": eid, "ts": ts, "skipped": "too_few_markets"}

    # endDate hours_to_resolution
    try:
        end_dt = datetime.fromisoformat((event.get("endDate") or "").replace("Z", "+00:00"))
        hrs_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        hrs_left = 999.0

    # Fetch all books in parallel (small N — typically 11)
    market_tokens: list[tuple] = []
    for m in markets:
        tok = extract_yes_token(m)
        if tok:
            market_tokens.append((m, tok))
    books = await asyncio.gather(*[fetch_book(client, t) for _, t in market_tokens])

    quotes: list[dict] = []  # one per bucket where we'd post a quote
    for (m, tok), book in zip(market_tokens, books):
        if not book:
            continue
        asks = sorted(book.get("asks") or [], key=lambda a: float(a.get("price", 0)))
        bids = sorted(book.get("bids") or [], key=lambda b: -float(b.get("price", 0)))
        if not asks:
            continue
        best_ask_p = float(asks[0].get("price", 0))
        best_ask_size = float(asks[0].get("size", 0))
        best_bid_p = float(bids[0].get("price", 0)) if bids else 0
        best_bid_size = float(bids[0].get("size", 0)) if bids else 0

        # Filter: only tail buckets
        if best_ask_p > MAX_TAIL_ASK or best_ask_p <= 0:
            continue
        if best_ask_size < MIN_TAIL_DEPTH:
            continue

        # Our sim quote: 1 tick above current best ask
        sim_ask_price = round(best_ask_p + QUOTE_OFFSET_TICKS * TICK_SIZE, 4)
        if sim_ask_price < MIN_OFFER_PRICE:
            sim_ask_price = MIN_OFFER_PRICE
        # Cap quote size into shares
        sim_quote_shares = round(QUOTE_SIZE_USD / sim_ask_price, 2)

        # Spread between our sim ask and best bid = our exposure if a taker hits us
        spread_pp = round((sim_ask_price - best_bid_p) * 100, 3)

        # Snapshot for markout tracking — record will be enriched on next run
        gy = gamma_yes(m)
        bucket_temp = parse_bucket_temp(m.get("question") or "")

        # Per [GPT 31 Q1]: record full ask depth below sim_ask (queue ahead of us)
        size_below_sim_ask = sum(
            float(a.get("size", 0)) for a in asks
            if float(a.get("price", 0)) < sim_ask_price
        )
        # ConditionId for trade-flow lookup later
        cond_id = m.get("conditionId") or ""

        quotes.append({
            "market_id": str(m.get("id")),
            "condition_id": cond_id,
            "token_id": tok,
            "question": (m.get("question") or "")[:80],
            "bucket_temp": bucket_temp,
            "best_bid": best_bid_p,
            "best_bid_depth": best_bid_size,
            "best_ask": best_ask_p,
            "best_ask_depth": best_ask_size,
            "sim_ask_price": sim_ask_price,
            "sim_quote_shares": sim_quote_shares,
            "size_below_sim_ask": round(size_below_sim_ask, 2),
            "spread_to_bid_pp": spread_pp,
            "gamma_yes": gy,
            "would_lose_if_yes_pp": round((sim_ask_price - 1.0) * 100, 3),
        })

    return {
        "event_id": eid,
        "title": title,
        "ts": ts,
        "hours_to_resolution": round(hrs_left, 1),
        "n_markets": len(markets),
        "n_tail_quotes": len(quotes),
        "quotes": quotes,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Markout / fill detection (compare our sim ask to next-snapshot best ask)
# ──────────────────────────────────────────────────────────────────────────────

def load_prev_snapshot() -> dict[tuple, dict]:
    """Load most recent prior quote per (event_id, market_id)."""
    if not OUTPUT.exists():
        return {}
    by_key: dict[tuple, dict] = {}
    with OUTPUT.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") != "quote_snapshot":
                continue
            ts = rec.get("ts", 0)
            for q in rec.get("quotes") or []:
                key = (rec.get("event_id"), q.get("market_id"))
                if key not in by_key or ts > by_key[key]["ts"]:
                    by_key[key] = {**q, "ts": ts, "event_id": rec.get("event_id")}
    return by_key


async def fetch_trades_window(
    client: httpx.AsyncClient, condition_id: str, since_ts: int
) -> list[dict]:
    """Pull all trades on a market since `since_ts`. PM trades are paginated DESC."""
    if not condition_id:
        return []
    out: list[dict] = []
    offset = 0
    while True:
        try:
            r = await client.get(
                TRADES_API,
                params={"market": condition_id, "limit": 500, "offset": offset},
                timeout=15.0,
            )
            r.raise_for_status()
            batch = r.json()
        except Exception:
            break
        if not batch:
            break
        # batch is DESC by timestamp
        for t in batch:
            if (t.get("timestamp") or 0) < since_ts:
                return out
            out.append(t)
        if len(batch) < 500:
            break
        offset += 500
        if offset > 5000:
            break  # safety cap
    return out


def classify_fill(prev: dict, trades: list[dict], cur_quote: dict) -> dict:
    """Per [GPT 31 Q1] tightened model.

    Returns dict with:
      crossed_quote_proxy : original heuristic (cur_best_bid >= prev_sim)
      fill_status         : NO_TRADE_AT_LEVEL | TRADE_BELOW_SIM_ASK |
                            TRADE_THROUGH_AVAILABLE | TRADE_THROUGH_INSUFFICIENT |
                            AMBIGUOUS
      taker_volume_at_or_above : sum of trade sizes where price >= sim_ask AND side=BUY
      queue_adjusted_fill_qty  : max(0, taker_volume - size_below_sim_ask)
      strict_filled            : True only when fill_status == TRADE_THROUGH_AVAILABLE
                                 AND queue_adjusted_fill_qty > 0
    """
    sim_ask = prev.get("sim_ask_price", 0)
    sim_size = prev.get("sim_quote_shares", 0)
    size_below = prev.get("size_below_sim_ask", 0)
    token_id = prev.get("token_id")
    cur_best_bid = cur_quote.get("best_bid", 0)

    # Original (loose) heuristic, kept for comparison
    crossed_quote_proxy = cur_best_bid >= sim_ask if sim_ask > 0 else False

    # Filter trades to our YES token only (asset_id matches)
    # And only BUY side (taker hitting asks)
    relevant_buys = [
        t for t in trades
        if str(t.get("asset")) == str(token_id) and t.get("side") == "BUY"
    ]
    max_buy_price = max(
        (float(t.get("price") or 0) for t in relevant_buys), default=0
    )
    taker_volume_at_level = sum(
        float(t.get("size") or 0) for t in relevant_buys
        if float(t.get("price") or 0) >= sim_ask
    )

    if not relevant_buys:
        fill_status = "NO_TRADE_AT_LEVEL"
        qa_fill = 0.0
        strict_filled = False
    elif max_buy_price < sim_ask:
        fill_status = "TRADE_BELOW_SIM_ASK"
        qa_fill = 0.0
        strict_filled = False
    else:
        # Trades crossed our level
        qa_fill = max(0.0, taker_volume_at_level - size_below)
        if qa_fill > 0:
            fill_status = "TRADE_THROUGH_AVAILABLE"
            strict_filled = True
        else:
            fill_status = "TRADE_THROUGH_INSUFFICIENT"
            strict_filled = False

    qa_fill_capped = min(qa_fill, sim_size)

    # Loose markout (existing) — over-counts
    markout_proxy = (
        round(sim_ask - cur_best_bid, 4)
        if crossed_quote_proxy and cur_best_bid > 0 else None
    )
    # Strict markout — only count actual trade-through fills
    markout_strict = (
        round(sim_ask - cur_best_bid, 4)
        if strict_filled and cur_best_bid > 0 else None
    )

    return {
        "sim_ask_price": sim_ask,
        "sim_quote_shares": sim_size,
        "size_below_sim_ask": size_below,
        "max_buy_price_in_window": round(max_buy_price, 4),
        "taker_volume_at_or_above_sim": round(taker_volume_at_level, 2),
        "n_trades_in_window": len(trades),
        "crossed_quote_proxy": crossed_quote_proxy,
        "fill_status": fill_status,
        "queue_adjusted_fill_qty": round(qa_fill_capped, 2),
        "strict_filled": strict_filled,
        "markout_pnl_per_share_proxy": markout_proxy,
        "markout_pnl_per_share_strict": markout_strict,
    }


async def evaluate_fills_strict(
    client: httpx.AsyncClient, prev_quotes_by_key: dict, cur_record: dict
) -> list[dict]:
    """Tightened evaluation using trade flow (Polymarket Data API).
    Old-style loose heuristic kept inside `crossed_quote_proxy` for comparison.
    """
    fills: list[dict] = []
    for q in cur_record.get("quotes") or []:
        mid = q["market_id"]
        prev = prev_quotes_by_key.get((cur_record.get("event_id"), mid))
        if not prev:
            continue
        cur_best_bid = q.get("best_bid", 0)
        cur_best_ask = q.get("best_ask", 0)
        elapsed_min = round((cur_record.get("ts", 0) - prev.get("ts", 0)) / 60, 1)

        # Pull trades on this market since previous snapshot
        cond_id = prev.get("condition_id") or ""
        trades = await fetch_trades_window(client, cond_id, prev.get("ts", 0))
        cls = classify_fill(prev, trades, q)

        fills.append({
            "kind": "markout",
            "ts": cur_record.get("ts"),
            "event_id": cur_record.get("event_id"),
            "market_id": mid,
            "prev_sim_ask": prev.get("sim_ask_price"),
            "cur_best_ask": cur_best_ask,
            "cur_best_bid": cur_best_bid,
            "elapsed_min": elapsed_min,
            "shares": prev.get("sim_quote_shares", 0),
            **cls,
        })
    return fills


def append_records(records: list[dict]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-events", type=int, default=200)
    args = parser.parse_args()

    started = time.time()
    ts_now = int(started)

    prev_quotes_by_key = load_prev_snapshot()
    logger.info("paper_maker.prior_snapshots | n=%d", len(prev_quotes_by_key))

    async with httpx.AsyncClient() as client:
        events = await fetch_active_temperature_events(client, limit=args.limit_events)
        logger.info("paper_maker.events | n=%d", len(events))
        if not events:
            logger.info("no events")
            return

        records: list[dict] = []
        for ev in events:
            try:
                rec = await simulate_event(client, ev, ts_now)
                rec["kind"] = "quote_snapshot"
                records.append(rec)
            except Exception as exc:
                logger.warning("simulate_failed | event=%s err=%s", ev.get("id"), exc)

    # Evaluate fills against prior snapshot — tightened model per [GPT 31 Q1]
    all_fills: list[dict] = []
    async with httpx.AsyncClient() as client2:
        for rec in records:
            if "skipped" in rec:
                continue
            fills = await evaluate_fills_strict(client2, prev_quotes_by_key, rec)
            all_fills.extend(fills)

    append_records(records + all_fills)

    elapsed = round(time.time() - started, 1)
    n_quotes = sum(r.get("n_tail_quotes", 0) or 0 for r in records)
    n_proxy = sum(1 for f in all_fills if f.get("crossed_quote_proxy"))
    n_strict = sum(1 for f in all_fills if f.get("strict_filled"))
    proxy_markouts = [
        f["markout_pnl_per_share_proxy"] for f in all_fills
        if f.get("markout_pnl_per_share_proxy") is not None
    ]
    strict_markouts = [
        f["markout_pnl_per_share_strict"] for f in all_fills
        if f.get("markout_pnl_per_share_strict") is not None
    ]
    median_proxy = sorted(proxy_markouts)[len(proxy_markouts) // 2] if proxy_markouts else 0
    median_strict = sorted(strict_markouts)[len(strict_markouts) // 2] if strict_markouts else 0
    total_proxy_pnl = sum(
        (f["markout_pnl_per_share_proxy"] or 0) * (f.get("shares") or 0)
        for f in all_fills if f.get("crossed_quote_proxy")
    )
    total_strict_pnl = sum(
        (f["markout_pnl_per_share_strict"] or 0) * (f.get("shares") or 0)
        for f in all_fills if f.get("strict_filled")
    )

    by_status: dict[str, int] = {}
    for f in all_fills:
        s = f.get("fill_status", "?")
        by_status[s] = by_status.get(s, 0) + 1

    logger.info(
        "paper_maker.done | events=%d quotes=%d fills_eval=%d "
        "proxy_filled=%d strict_filled=%d "
        "median_proxy=%.4f median_strict=%.4f "
        "total_proxy_pnl=$%.2f total_strict_pnl=$%.2f elapsed=%ss",
        len(records), n_quotes, len(all_fills),
        n_proxy, n_strict, median_proxy, median_strict,
        total_proxy_pnl, total_strict_pnl, elapsed,
    )
    logger.info("paper_maker.fill_status | %s", " ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    print(f"\n=== Paper Tail-Maker Sim ({len(records)} events, {n_quotes} tail quotes) ===\n")
    print(f"{'event':<8} {'hrs':<7} {'tail_quotes':<13} {'title':<55}")
    for r in records[:20]:
        if "skipped" in r:
            continue
        print(f"{(r.get('event_id') or '')[:7]:<8} {r.get('hours_to_resolution',0):<7.1f} "
              f"{r.get('n_tail_quotes', 0):<13} {(r.get('title') or '')[:55]}")

    if all_fills:
        print(f"\n=== Fill classification breakdown ===")
        for k, v in sorted(by_status.items()):
            print(f"  {k:<30} {v}")
        print(f"\n=== Strict-fill events (TRADE_THROUGH_AVAILABLE only) ===")
        print(f"{'event':<8} {'mid':<8} {'sim_ask':<10} {'cur_bid':<10} {'taker_vol':<11} {'qa_fill':<8} {'markout/sh':<12}")
        for f in all_fills:
            if not f.get("strict_filled"):
                continue
            mps = f.get("markout_pnl_per_share_strict")
            print(f"{(f.get('event_id') or '')[:7]:<8} "
                  f"{(f.get('market_id') or '')[:7]:<8} "
                  f"${f.get('prev_sim_ask', 0):<8.4f} "
                  f"${f.get('cur_best_bid', 0):<8.4f} "
                  f"{f.get('taker_volume_at_or_above_sim', 0):<11.0f} "
                  f"{f.get('queue_adjusted_fill_qty', 0):<8.1f} "
                  f"${mps if mps is not None else 0:<10.4f}")
    print(f"\n  proxy fills (UPPER BOUND):  {n_proxy}  median ${median_proxy:.4f}/sh  total ${total_proxy_pnl:.2f}")
    print(f"  strict fills (TRADE_THROUGH): {n_strict}  median ${median_strict:.4f}/sh  total ${total_strict_pnl:.2f}")
    print(f"\n  Status labels: PROXY=upper-bound until sufficient sample. STRICT=conservative trade-through queue-adjusted.")


if __name__ == "__main__":
    asyncio.run(main())
