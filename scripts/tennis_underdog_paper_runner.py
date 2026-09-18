"""Tennis underdog PAPER canary runner — overrides [GPT 49] spec per maintainer's call.

Per [GPT 49] strict gates (we apply them all even though we skipped Phase 1):
  - $1 stake per trade
  - max 5 trades / UTC day
  - max 2 per tournament / day
  - max 1 per match (idempotent on market_id)
  - daily loss stop -$5 → halt new entries
  - kill switch if 30 trades and cum < -$1 → halt strategy

Trigger: same as shadow runner
  - active gamma WTA/ATP H2H market
  - outcome ask ∈ [$0.075, $0.10]

Action:
  - Insert paper_order (BUY, NO/YES same as outcome bought)
  - Insert position with bucket='tennis_underdog_canary'
  - Use NUMERIC market_id (gamma id) so scanner can manage exits
  - Telegram alert on every entry

Cron: */10min (same as shadow)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
DB = Path('/app/data/paper_bot.db')
STATE = Path('/app/data/tennis_underdog_paper_state.json')
BUCKET = 'tennis_underdog_canary'

# [GPT 49] strict gates
MIN_PRICE = 0.075
MAX_PRICE = 0.10
SIZE_USD = 1.0
DAILY_CAP = 5
MAX_PER_TOURNAMENT = 2
MAX_OPEN = 5
DAILY_PNL_STOP = -5.0
KILL_AT_TRADES = 30
KILL_AT_PNL = -1.0


def fetch_gamma_markets() -> list[dict]:
    url = ("https://gamma-api.polymarket.com/markets?"
           "active=true&closed=false&archived=false&limit=500"
           "&order=volume24hr&ascending=false")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as exc:
        LOG.warning("gamma fetch err: %s", exc)
        return []


def fetch_clob_book(token_id: str) -> dict | None:
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def best_ask(book: dict) -> tuple[float | None, float]:
    asks = book.get("asks") or []
    if not asks:
        return None, 0.0
    best = min(asks, key=lambda a: float(a.get("price", 999)))
    return float(best.get("price", 0)), float(best.get("size", 0))


def best_bid(book: dict) -> float | None:
    bids = book.get("bids") or []
    if not bids:
        return None
    best = max(bids, key=lambda b: float(b.get("price", 0)))
    return float(best.get("price", 0))


def is_tennis_h2h(slug: str) -> tuple[bool, str | None]:
    s = (slug or "").lower()
    if "wta-" in s:
        return True, "WTA"
    if "atp-" in s:
        return True, "ATP"
    return False, None


def parse_tournament(slug: str) -> str:
    parts = (slug or "").split("-")
    if len(parts) >= 2 and parts[0] in ("wta", "atp"):
        for i, p in enumerate(parts[1:], 1):
            if p.isdigit() and len(p) == 4:
                return "-".join(parts[1:i])
        return parts[1]
    return "unknown"


def trades_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND date(created_at)=?",
        (BUCKET, today)
    ).fetchone()[0]


def open_positions(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND quantity>0",
        (BUCKET,)
    ).fetchone()[0]


def daily_pnl(conn: sqlite3.Connection) -> float:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
        "WHERE bucket=? AND date(updated_at)=? AND quantity=0",
        (BUCKET, today)
    ).fetchone()[0]
    unrealized = conn.execute(
        "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions "
        "WHERE bucket=? AND date(created_at)=? AND quantity>0",
        (BUCKET, today)
    ).fetchone()[0]
    return float(realized) + float(unrealized)


def total_stats(conn: sqlite3.Connection) -> tuple[int, float]:
    """Returns (n_closed_trades, cum_realized_$) for kill-switch check."""
    n = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND quantity=0",
        (BUCKET,)
    ).fetchone()[0]
    cum = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
        "WHERE bucket=? AND quantity=0",
        (BUCKET,)
    ).fetchone()[0]
    return n, float(cum)


def trades_per_tournament_today(conn: sqlite3.Connection, tournament: str) -> int:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return conn.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE strategy=? AND date(created_at)=? "
        "AND note LIKE ?",
        (BUCKET, today, f"%tournament={tournament}%")
    ).fetchone()[0]


def has_open_market(conn: sqlite3.Connection, market_id: str) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND market_id=? AND quantity>0",
        (BUCKET, market_id)
    ).fetchone()[0] > 0


def telegram(text: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        return
    try:
        import httpx
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text},
            )
    except Exception:
        pass


def insert_trade(
    conn: sqlite3.Connection, market_id: str, outcome: str,
    entry: float, size_shares: float, note: str,
) -> int:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO paper_orders
           (market_id, outcome, side, price, size, status, mode, strategy, note, created_at, updated_at)
           VALUES (?, ?, 'BUY', ?, ?, 'filled', 'paper_auto', ?, ?, ?, ?)""",
        (market_id, outcome, entry, size_shares, BUCKET, note, now, now)
    )
    order_id = cur.lastrowid
    cur.execute(
        """INSERT INTO positions
           (market_id, outcome, quantity, avg_price, realized_pnl, unrealized_pnl,
            created_at, updated_at, bucket, cluster_key)
           VALUES (?, ?, ?, ?, 0.0, 0.0, ?, ?, ?, NULL)""",
        (market_id, outcome, size_shares, entry, now, now, BUCKET)
    )
    conn.commit()
    return order_id


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not DB.exists():
        LOG.error("DB missing")
        return 1

    conn = sqlite3.connect(DB)

    # Kill-switch check
    n_closed, cum_pnl = total_stats(conn)
    if n_closed >= KILL_AT_TRADES and cum_pnl < KILL_AT_PNL:
        LOG.warning("KILL SWITCH: n=%s cum=$%.2f → strategy halted permanently",
                    n_closed, cum_pnl)
        conn.close()
        return 0

    placed = trades_today(conn)
    open_n = open_positions(conn)
    pnl = daily_pnl(conn)
    LOG.info(
        "start | placed=%s/%s open=%s/%s pnl_today=$%.3f | total_n=%s cum=$%.2f",
        placed, DAILY_CAP, open_n, MAX_OPEN, pnl, n_closed, cum_pnl
    )

    if placed >= DAILY_CAP:
        LOG.info("daily cap reached")
        conn.close()
        return 0
    if open_n >= MAX_OPEN:
        LOG.info("max open reached")
        conn.close()
        return 0
    if pnl <= DAILY_PNL_STOP:
        LOG.warning("daily PnL stop hit | pnl=$%.3f", pnl)
        conn.close()
        return 0

    markets = fetch_gamma_markets()
    LOG.info("gamma markets: %s", len(markets))

    new_count = 0
    for m in markets:
        if placed >= DAILY_CAP or open_n + new_count >= MAX_OPEN:
            break

        slug = m.get("slug") or ""
        is_tennis, sport = is_tennis_h2h(slug)
        if not is_tennis:
            continue

        outcomes_raw = m.get("outcomes")
        token_ids_raw = m.get("clobTokenIds")
        if not outcomes_raw or not token_ids_raw:
            continue
        try:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        except Exception:
            continue
        if len(outcomes) != 2:
            continue

        market_id = str(m.get("id"))
        cid = m.get("conditionId") or ""
        end_date = m.get("endDate") or ""
        tournament = parse_tournament(slug)

        # Tournament cap — skip if already 2 from this tour today
        tour_count = trades_per_tournament_today(conn, tournament)
        if tour_count >= MAX_PER_TOURNAMENT:
            continue

        if has_open_market(conn, market_id):
            continue  # max 1 per match (idempotent)

        # Check both outcomes for sweet spot
        for i, (out_name, tok_id) in enumerate(zip(outcomes, token_ids)):
            book = fetch_clob_book(tok_id)
            if book is None:
                continue
            ask, ask_size = best_ask(book)
            bid = best_bid(book)
            if ask is None or ask < MIN_PRICE or ask > MAX_PRICE:
                continue

            size_shares = round(SIZE_USD / ask, 4)
            if size_shares <= 0:
                continue

            note = (
                f"tennis_underdog | {sport} | tournament={tournament} | "
                f"slug={slug} | ask=${ask:.4f} ask_size={ask_size:.0f} bid=${bid or 0:.4f} | "
                f"end={end_date[:10]}"
            )
            order_id = insert_trade(conn, market_id, out_name, ask, size_shares, note)
            placed += 1
            new_count += 1
            LOG.info(
                "PLACED | order_id=%s mid=%s %s/%s @$%.4fx%.2f | $%.2f notional",
                order_id, market_id, sport, out_name, ask, size_shares, ask * size_shares
            )

            telegram(
                f"🎾 Tennis Underdog Canary OPENED\n"
                f"{sport} match: {slug[:50]}\n"
                f"Player: {out_name}\n"
                f"Entry: ${ask:.4f} × {size_shares:.2f} = $1.00\n"
                f"Spread: ${ask - (bid or 0):.4f}\n"
                f"Tournament: {tournament}\n"
                f"Resolves: {end_date[:16]}\n"
                f"Today: {placed}/{DAILY_CAP} entries"
            )

            if placed >= DAILY_CAP:
                break

    LOG.info("done | new_trades=%s placed_today=%s", new_count, placed)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
