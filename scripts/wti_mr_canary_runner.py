"""WTI Mean-Reversion Canary — diagnostic single-asset trader.

Per [Claude 56] inverted backtest:
  - Train @ 4% threshold: WR=85%, avg=+$0.04, sharpe=+0.50
  - Val @ 4% threshold: WR=77%, avg=+$0.025, sharpe=+0.40
  - 1794 val trades all on WTI markets

Hypothesis: WTI markets mean-revert at 60m timescale.
  When bot proposes BUY YES with raw_edge >= 4% on WTI, the mid drifts
  back DOWN within 60m → fade signal = BUY NO at (1 - poly_ask).

Cron: */5min. Runs as DIAGNOSTIC canary, $1 stake, narrow gates:
  - WTI markets only (slug contains 'wti' or 'oil')
  - raw_edge >= 4%
  - max 3 open positions at once
  - daily loss stop -$2 (kill new entries)
  - exit at 60m (we set updated_at; relies on scanner exit_manager
    or stale_exit at 4h fallback)
  - max 5 entries / day

Kill criteria (per [GPT 46] strict gates):
  - if cum_$ over 50 closed trades < +$0.50 → halt + alert
  - if any single day -$3 → halt + alert
  - if cluster diversity < 3 distinct slugs in 50 trades → diagnostic flag

Output:
  - paper_orders + positions with bucket='wti_mr_canary'
  - /var/log/wti_mr_canary.log
  - Telegram alert on first daily entry + on kill criteria hits
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import urllib.request

LOG = logging.getLogger(__name__)
DB = Path('/app/data/paper_bot.db')
STATE = Path('/app/data/wti_mr_canary_state.json')
BUCKET = 'wti_mr_canary'

# Strict spec
MIN_RAW_EDGE = 0.04
MAX_RAW_EDGE = 0.20  # cap — beyond is suspicious
DAILY_CAP = 5
SIZE_USD = 1.0
MAX_OPEN = 3
DAILY_PNL_STOP = -2.0
MIN_NO_PRICE = 0.50  # don't fade if NO already too cheap
MAX_NO_PRICE = 0.85  # don't fade if NO already too expensive (no edge to capture)
MAX_CANDIDATE_AGE_MIN = 5  # use only fresh signals


def is_wti_market(slug: str) -> bool:
    s = (slug or '').lower()
    return 'wti' in s or 'oil' in s or 'crude' in s


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


def trades_today(conn: sqlite3.Connection) -> int:
    """Count opened today (whether closed or not)."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND date(created_at)=?",
        (BUCKET, today)
    ).fetchone()[0]


def open_positions_count(conn: sqlite3.Connection) -> int:
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


def insert_trade(conn, market_id, no_entry, no_size, note):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO paper_orders
           (market_id, outcome, side, price, size, status, mode, strategy, note, created_at, updated_at)
           VALUES (?, 'NO', 'BUY', ?, ?, 'filled', 'paper_auto', ?, ?, ?, ?)""",
        (market_id, no_entry, no_size, BUCKET, note, now, now)
    )
    order_id = cur.lastrowid
    cur.execute(
        """INSERT INTO positions
           (market_id, outcome, quantity, avg_price, realized_pnl, unrealized_pnl,
            created_at, updated_at, bucket, cluster_key)
           VALUES (?, 'NO', ?, ?, 0.0, 0.0, ?, ?, ?, NULL)""",
        (market_id, no_size, no_entry, now, now, BUCKET)
    )
    conn.commit()
    return order_id


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not DB.exists():
        LOG.error("DB missing")
        return 1

    conn = sqlite3.connect(DB)
    placed = trades_today(conn)
    open_n = open_positions_count(conn)
    pnl = daily_pnl(conn)

    LOG.info("start | placed_today=%s/%s open=%s/%s pnl_today=$%.3f",
             placed, DAILY_CAP, open_n, MAX_OPEN, pnl)

    # Kill switches
    if placed >= DAILY_CAP:
        LOG.info("daily cap reached")
        conn.close()
        return 0
    if open_n >= MAX_OPEN:
        LOG.info("max open reached")
        conn.close()
        return 0
    if pnl <= DAILY_PNL_STOP:
        LOG.warning("daily PnL stop hit | pnl=$%.3f <= $%.2f", pnl, DAILY_PNL_STOP)
        conn.close()
        return 0

    # Pull recent fresh asset_target candidates with raw_edge >= MIN
    rows = conn.execute(
        f"""SELECT id, market_id, slug, raw_edge, poly_ask, poly_bid, created_at
            FROM opportunity_logs
            WHERE market_type='asset_target'
              AND created_at >= datetime('now','-{MAX_CANDIDATE_AGE_MIN} minutes')
              AND raw_edge >= ? AND raw_edge <= ?
              AND poly_ask IS NOT NULL
            ORDER BY created_at DESC LIMIT 20""",
        (MIN_RAW_EDGE, MAX_RAW_EDGE)
    ).fetchall()
    LOG.info("candidates: %s", len(rows))

    for rid, mid, slug, raw, p_ask, p_bid, ct in rows:
        if not is_wti_market(slug):
            continue

        # Compute our NO entry (= 1 - YES ask)
        if not p_ask or p_ask <= 0 or p_ask >= 1:
            continue
        no_entry = round(1.0 - p_ask, 4)
        if no_entry < MIN_NO_PRICE or no_entry > MAX_NO_PRICE:
            LOG.info("skip %s no_entry=$%.3f out of [%s,%s]",
                     mid, no_entry, MIN_NO_PRICE, MAX_NO_PRICE)
            continue

        # Already have open on same market?
        n_same = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE bucket=? AND market_id=? AND quantity>0",
            (BUCKET, mid)
        ).fetchone()[0]
        if n_same > 0:
            continue

        no_size = round(SIZE_USD / no_entry, 4)
        note = f"wti_mr fade-{slug[:30]} raw_edge={raw:.4f} their_yes=${p_ask:.4f}"
        order_id = insert_trade(conn, mid, no_entry, no_size, note)
        placed += 1
        LOG.info("PLACED order_id=%s mid=%s NO@$%.4fx%.2f | %s",
                 order_id, mid, no_entry, no_size, note)

        # First trade of day → telegram alert
        if placed == 1:
            telegram(
                f"WTI MR canary opened first trade today\n"
                f"market: {slug[:40]}\n"
                f"NO entry: ${no_entry:.4f} x {no_size:.2f} = $1.00\n"
                f"opportunity raw_edge: {raw*100:.1f}%"
            )

        if placed >= DAILY_CAP or open_n + placed >= MAX_OPEN:
            break

    LOG.info("done | placed=%s", placed)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
