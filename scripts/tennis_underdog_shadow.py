"""Tennis underdog shadow runner — Phase 1 per [GPT 49].

NO EXPOSURE. Pre-registers candidates BEFORE match resolution so we can
later prove the bot picked them at signal time, not in hindsight.

Trigger:
  - active gamma WTA or ATP H2H market
  - market not yet closed
  - outcome (player) ask ∈ [$0.075, $0.10]
  - we haven't logged this (market_id, outcome) pair already

Action:
  Append to /app/data/tennis_underdog_shadow.jsonl:
    {
      ts: int,
      market_id: str,        # numeric gamma id
      condition_id: str,     # 0x...
      outcome: str,          # player name
      slug: str,
      tournament: str,       # parsed from slug if possible
      ask: float,
      bid: float,
      spread: float,
      ask_size: float,       # liquidity at signal
      end_date: str,
      decision: "SHADOW_BUY",
      sport: "WTA" | "ATP",
    }

NO TRADES. NO POSITIONS. Just timestamped predictions.

Cron: */10min
Idempotent state: /app/data/tennis_underdog_shadow_state.json
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

LOG = logging.getLogger(__name__)
SHADOW_LOG = Path('/app/data/tennis_underdog_shadow.jsonl')
STATE = Path('/app/data/tennis_underdog_shadow_state.json')

GAMMA_URL = (
    "https://gamma-api.polymarket.com/markets?"
    "active=true&closed=false&archived=false&limit=500&order=volume24hr&ascending=false"
)
CLOB_BOOK = "https://clob.polymarket.com/book?token_id={tok}"

MIN_PRICE = 0.075
MAX_PRICE = 0.10


def fetch_gamma_markets() -> list[dict]:
    try:
        req = urllib.request.Request(GAMMA_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as exc:
        LOG.warning("gamma fetch err: %s", exc)
        return []


def is_tennis_h2h(slug: str) -> tuple[bool, str | None]:
    s = (slug or "").lower()
    if "wta-" in s:
        return True, "WTA"
    if "atp-" in s:
        return True, "ATP"
    return False, None


def parse_tournament(slug: str) -> str:
    """Best-effort tournament name from slug."""
    parts = (slug or "").split("-")
    # slug pattern: wta-rome-rybakina-vondrousova-2026-05-10 or similar
    if len(parts) >= 2 and parts[0] in ("wta", "atp"):
        # Find date-like pattern position
        for i, p in enumerate(parts[1:], 1):
            if p.isdigit() and len(p) == 4:
                # tournament = parts before date
                return "-".join(parts[1:i])
        return parts[1]  # fallback first segment
    return "unknown"


def fetch_clob_book(token_id: str) -> dict | None:
    """Fetch CLOB order book for a token."""
    try:
        req = urllib.request.Request(
            CLOB_BOOK.format(tok=token_id),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def best_ask(book: dict) -> tuple[float | None, float]:
    """Returns (best_ask_price, ask_size)."""
    asks = book.get("asks") or []
    if not asks:
        return None, 0.0
    # asks sorted ascending; take min price
    best = min(asks, key=lambda a: float(a.get("price", 999)))
    return float(best.get("price", 0)), float(best.get("size", 0))


def best_bid(book: dict) -> float | None:
    bids = book.get("bids") or []
    if not bids:
        return None
    best = max(bids, key=lambda b: float(b.get("price", 0)))
    return float(best.get("price", 0))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def log_candidate(rec: dict) -> None:
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_LOG.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    state = load_state()
    seen: set[str] = set(state.get("seen_keys", []))

    markets = fetch_gamma_markets()
    LOG.info("gamma_markets=%s seen_keys=%s", len(markets), len(seen))

    new_count = 0
    skipped_already = 0
    skipped_filter = 0
    book_fail = 0
    tennis_seen = 0
    skipped_price = 0

    for m in markets:
        slug = m.get("slug") or ""
        is_tennis, sport = is_tennis_h2h(slug)
        if not is_tennis:
            skipped_filter += 1
            continue
        tennis_seen += 1

        outcomes_raw = m.get("outcomes")
        token_ids_raw = m.get("clobTokenIds")
        if not outcomes_raw or not token_ids_raw:
            skipped_filter += 1
            continue
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except Exception:
            skipped_filter += 1
            continue

        if len(outcomes) != 2 or len(token_ids) != 2:
            continue

        market_id = str(m.get("id"))
        cid = m.get("conditionId") or ""
        end_date = m.get("endDate") or ""
        tournament = parse_tournament(slug)

        # Check each outcome side independently
        for i, (out_name, tok_id) in enumerate(zip(outcomes, token_ids)):
            key = f"{cid}|{out_name}"
            if key in seen:
                skipped_already += 1
                continue

            book = fetch_clob_book(tok_id)
            if book is None:
                book_fail += 1
                continue

            ask, ask_size = best_ask(book)
            bid = best_bid(book)
            if ask is None:
                continue
            if ask < MIN_PRICE or ask > MAX_PRICE:
                skipped_price += 1
                continue
            spread = (ask - bid) if (bid is not None and ask is not None) else 0.0

            rec = {
                "ts": int(time.time()),
                "market_id": market_id,
                "condition_id": cid,
                "outcome": out_name,
                "slug": slug,
                "tournament": tournament,
                "ask": ask,
                "bid": bid,
                "spread": round(spread, 4),
                "ask_size": ask_size,
                "end_date": end_date,
                "decision": "SHADOW_BUY",
                "sport": sport,
            }
            log_candidate(rec)
            seen.add(key)
            new_count += 1
            LOG.info(
                "SHADOW_BUY | %s/%s @$%.4f×%.0f spread=$%.3f | %s",
                sport, out_name, ask, ask_size, spread, slug[:50],
            )

    state["seen_keys"] = sorted(seen)
    state["last_run"] = int(time.time())
    save_state(state)

    LOG.info(
        "done | new=%s already=%s tennis_markets=%s skip_price=%s filtered=%s book_fail=%s",
        new_count, skipped_already, tennis_seen, skipped_price, skipped_filter, book_fail,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
