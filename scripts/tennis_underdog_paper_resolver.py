"""Tennis underdog paper canary resolver — closes positions when market resolves.

For each open position with bucket='tennis_underdog_canary':
  1. Fetch gamma /markets/{market_id}
  2. If closed: determine winner outcome
  3. Match against position's outcome:
       - won  → realized_pnl = (1.0 - entry) * shares = (size_shares - notional)
       - lost → realized_pnl = -entry * shares = -notional
  4. Set quantity=0, write SELL paper_order, Telegram alert

Cron: */15min (slightly slower than runner since resolutions take time).

Кроме того — **safety closer**: if position older than 48h and still open
(market hasn't actually closed in gamma yet), force-close at avg_price (no PnL).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
DB = Path('/app/data/paper_bot.db')
BUCKET = 'tennis_underdog_canary'
STALE_HOURS = 48


def fetch_market(market_id: str) -> dict | None:
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def parse_winner(market: dict) -> str | None:
    if not market.get("closed"):
        return None
    try:
        prices_raw = market.get("outcomePrices", "")
        outcomes_raw = market.get("outcomes", "")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        if not prices or not outcomes:
            return None
        for i, p in enumerate(prices):
            if abs(float(p) - 1.0) < 0.01 and i < len(outcomes):
                return str(outcomes[i])
    except Exception:
        return None
    return None


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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not DB.exists():
        LOG.error("DB missing")
        return 1

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    open_pos = c.execute(
        """SELECT id, market_id, outcome, quantity, avg_price, created_at
           FROM positions
           WHERE bucket=? AND quantity>0""",
        (BUCKET,)
    ).fetchall()
    LOG.info("open positions: %s", len(open_pos))

    closed_now = 0
    for pid, mid, outcome, qty, entry, ct in open_pos:
        market = fetch_market(mid)
        if market is None:
            LOG.warning("market %s fetch failed", mid)
            continue

        winner = parse_winner(market)
        now = datetime.now(timezone.utc)
        ct_dt = datetime.fromisoformat(ct.replace(" ", "T") + "+00:00") if "+" not in ct else datetime.fromisoformat(ct)
        age_hours = (now - ct_dt.replace(tzinfo=timezone.utc) if ct_dt.tzinfo is None else now - ct_dt).total_seconds() / 3600

        if winner is None:
            # Not yet resolved
            if age_hours > STALE_HOURS:
                # Force close at entry (paper void)
                c.execute(
                    "UPDATE positions SET quantity=0, realized_pnl=0, unrealized_pnl=0, updated_at=? WHERE id=?",
                    (now.strftime('%Y-%m-%d %H:%M:%S'), pid)
                )
                c.execute(
                    """INSERT INTO paper_orders (market_id, outcome, side, price, size, status, mode, strategy, note, created_at, updated_at)
                       VALUES (?, ?, 'SELL', ?, ?, 'filled', 'paper_auto', ?, ?, ?, ?)""",
                    (mid, outcome, entry, qty, BUCKET,
                     f"STALE_EXIT after {age_hours:.1f}h, no winner from gamma",
                     now.strftime('%Y-%m-%d %H:%M:%S'),
                     now.strftime('%Y-%m-%d %H:%M:%S'))
                )
                conn.commit()
                LOG.warning("STALE_CLOSE id=%s after %.1fh", pid, age_hours)
            continue

        # Resolved
        won = (outcome == winner)
        # PnL: BUY at entry, hold to resolution
        # If won: receive $1 per share → PnL = (1 - entry) * qty
        # If lost: receive $0 per share → PnL = -entry * qty
        if won:
            realized = round((1.0 - entry) * qty, 4)
        else:
            realized = round(-entry * qty, 4)

        sell_price = 1.0 if won else 0.0
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            """UPDATE positions SET quantity=0, realized_pnl=?, unrealized_pnl=0, updated_at=? WHERE id=?""",
            (realized, now_str, pid)
        )
        c.execute(
            """INSERT INTO paper_orders (market_id, outcome, side, price, size, status, mode, strategy, note, created_at, updated_at)
               VALUES (?, ?, 'SELL', ?, ?, 'filled', 'paper_auto', ?, ?, ?, ?)""",
            (mid, outcome, sell_price, qty, BUCKET,
             f"RESOLVED winner={winner} our={outcome} won={won} entry=${entry:.4f}",
             now_str, now_str)
        )
        conn.commit()
        closed_now += 1

        emoji = "✅" if won else "❌"
        telegram(
            f"{emoji} Tennis Canary RESOLVED\n"
            f"Player: {outcome}\n"
            f"Winner: {winner}\n"
            f"Won: {won}\n"
            f"Entry: ${entry:.4f} × {qty:.2f}\n"
            f"PnL: ${realized:+.4f}"
        )
        LOG.info(
            "RESOLVED id=%s outcome=%s winner=%s won=%s pnl=$%+.4f",
            pid, outcome, winner, won, realized
        )

    # Summary across all closed
    n_total = c.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND quantity=0",
        (BUCKET,)
    ).fetchone()[0]
    cum = c.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE bucket=? AND quantity=0",
        (BUCKET,)
    ).fetchone()[0]
    wins = c.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND quantity=0 AND realized_pnl > 0",
        (BUCKET,)
    ).fetchone()[0]
    LOG.info(
        "summary | closed_total=%s wins=%s cum=$%+.2f closed_this_run=%s",
        n_total, wins, cum, closed_now
    )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
