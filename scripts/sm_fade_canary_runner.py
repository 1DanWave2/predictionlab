"""SM fade canary — opens REAL paper trades against tracked-loser wallets.

Per [Claude 52] План B / maintainer GO.

Logic per cycle (cron */2 min):
  1. Read sm_weather_signals.jsonl tail (idempotent via byte offset state)
  2. For each new signal where:
       wallet in FADE_LIST
       side == BUY
       outcome == Yes
       title contains weather keywords + future date
       size > 0
     → place inverse trade: BUY NO at (1 - their_price)
  3. Daily cap: max 5 trades/day (safety)
  4. Skip if we already have open position on same market_id
  5. Direct DB insert (paper_order + position with bucket='sm_fade_0xc80fa1fc_canary')

Exit: handled by existing exit_manager (TP/SL) or stale_exit_hours=48.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
DB = '/app/data/paper_bot.db'
SIGNALS = Path('/app/data/sm_weather_signals.jsonl')
STATE = Path('/app/data/sm_fade_runner_state.json')
BUCKET = 'sm_fade_0xc80fa1fc_canary'
DAILY_CAP = 5
SIZE_USD = 1.0
# Real [GPT 44] (Codex) tightened spec — narrower than earlier draft:
MAX_ENTRY_PRICE = 0.95   # skip if NO already too expensive
MIN_ENTRY_PRICE = 0.92   # NEW: trigger only when their_price ≤ $0.08 (= our_entry ≥ $0.92)
DAILY_PNL_STOP = -2.0    # tightened from -$3 to -$2
CLUSTER_MAX_PER_EVENT = 1  # tightened from 2 → 1: max $1 per city/date
MAX_OPEN_EVENTS = 3      # NEW: max 3 distinct events open at once
MAX_SIGNAL_AGE_HOURS = 6  # NEW per [Claude] 2026-05-08: skip signals from already-resolved markets

FADE_LIST = {
    '0xc80fa1fc5740dec6',
}


def build_condition_id_map() -> dict[str, str]:
    """Fetch active gamma markets, build lowercased condition_id → numeric market_id.

    Returns map for the latest 500 active markets. Sufficient for weather markets
    which resolve daily.
    """
    url = (
        "https://gamma-api.polymarket.com/markets?"
        "limit=500&active=true&closed=false&archived=false&order=endDate&ascending=true"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as exc:
        LOG.warning("gamma_fetch_failed | err=%s", exc)
        return {}

    cmap: dict[str, str] = {}
    for m in data:
        cid = (m.get("conditionId") or "").lower()
        mid = str(m.get("id") or "")
        if cid and mid:
            cmap[cid] = mid
    LOG.info("condition_id_map | %s entries", len(cmap))
    return cmap


def parse_temp(text: str) -> int | None:
    m = re.search(r'(\d{1,2})\s*°?\s*[cf]', text.lower())
    return int(m.group(1)) if m else None


def parse_city_date(text: str) -> tuple[str | None, str | None]:
    t = text.lower()
    cities = ['tokyo', 'taipei', 'jakarta', 'manila', 'bangkok', 'singapore',
              'hong kong', 'seoul', 'shanghai', 'beijing', 'mumbai', 'delhi',
              'miami', 'austin', 'dallas', 'phoenix', 'new york', 'los angeles']
    city = next((c for c in cities if c in t), None)
    m = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})', t)
    date = f'{m.group(1)} {m.group(2)}' if m else None
    return city, date


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
    """Count currently-OPEN sm_fade positions (not historic — closed slots free up)."""
    c = conn.cursor()
    n = c.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND quantity>0",
        (BUCKET,)
    ).fetchone()[0]
    return n


def has_open_position(conn: sqlite3.Connection, market_id: str) -> bool:
    c = conn.cursor()
    n = c.execute(
        "SELECT COUNT(*) FROM positions WHERE bucket=? AND market_id=? AND quantity>0",
        (BUCKET, market_id)
    ).fetchone()[0]
    return n > 0


def trades_per_event(conn: sqlite3.Connection, city: str, date: str) -> int:
    """Count placed trades on given (city,date) — per [GPT 44] cluster gate."""
    c = conn.cursor()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    n = c.execute(
        "SELECT COUNT(*) FROM paper_orders WHERE strategy=? AND date(created_at)=? "
        "AND note LIKE ?",
        (BUCKET, today, f"%city={city} date={date}%")
    ).fetchone()[0]
    return n


def open_events_count(conn: sqlite3.Connection) -> int:
    """Number of distinct (city,date) events with currently-open sm_fade positions.
    Per [GPT 44] — max 3 open events."""
    c = conn.cursor()
    rows = c.execute(
        "SELECT note FROM paper_orders WHERE strategy=? "
        "AND market_id IN (SELECT market_id FROM positions WHERE bucket=? AND quantity>0)",
        (BUCKET, BUCKET)
    ).fetchall()
    events = set()
    for (note,) in rows:
        if note:
            # note format: ... city=X date=Y bucket=...
            import re as _re
            m = _re.search(r"city=(\S+)\s+date=(\S+\s+\d+)", note)
            if m:
                events.add((m.group(1), m.group(2)))
    return len(events)


def daily_pnl(conn: sqlite3.Connection) -> float:
    """Sum realized + unrealized for sm_fade today — per [GPT 44] daily-stop gate."""
    c = conn.cursor()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    realized = c.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
        "WHERE bucket=? AND date(updated_at)=? AND quantity=0",
        (BUCKET, today)
    ).fetchone()[0]
    unrealized = c.execute(
        "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM positions "
        "WHERE bucket=? AND date(created_at)=? AND quantity>0",
        (BUCKET, today)
    ).fetchone()[0]
    return float(realized) + float(unrealized)


def insert_fade_trade(conn: sqlite3.Connection, market_id: str,
                      our_entry: float, our_size: float, note: str) -> int:
    c = conn.cursor()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    c.execute(
        """INSERT INTO paper_orders
           (market_id, outcome, side, price, size, status, mode, strategy, note, created_at, updated_at)
           VALUES (?, 'NO', 'BUY', ?, ?, 'filled', 'paper_auto', ?, ?, ?, ?)""",
        (market_id, our_entry, our_size, BUCKET, note, now, now)
    )
    order_id = c.lastrowid
    c.execute(
        """INSERT INTO positions
           (market_id, outcome, quantity, avg_price, realized_pnl, unrealized_pnl, created_at, updated_at, bucket, cluster_key)
           VALUES (?, 'NO', ?, ?, 0.0, 0.0, ?, ?, ?, NULL)""",
        (market_id, our_size, our_entry, now, now, BUCKET)
    )
    conn.commit()
    return order_id


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    if not SIGNALS.exists():
        LOG.info('no signals file yet')
        return 0
    if not Path(DB).exists():
        LOG.error('DB missing')
        return 1

    state = load_state()
    last_offset = state.get('last_byte_offset', 0)
    conn = sqlite3.connect(DB)

    # Build condition_id → numeric market_id map once per run.
    # Per [Claude] 2026-05-08 incident: scanner orphan-killed positions whose
    # market_id was condition_id (0x...). Need numeric ID for scanner visibility.
    cid_map = build_condition_id_map()
    placed_today = trades_today(conn)
    pnl_today = daily_pnl(conn)
    LOG.info('start | placed_today=%s/%s pnl_today=$%.3f last_offset=%s',
             placed_today, DAILY_CAP, pnl_today, last_offset)

    if placed_today >= DAILY_CAP:
        LOG.info('daily cap reached, skipping new entries')
        with SIGNALS.open() as f:
            f.seek(0, 2)
            state['last_byte_offset'] = f.tell()
        save_state(state)
        return 0

    if pnl_today <= DAILY_PNL_STOP:
        LOG.warning('daily PnL stop hit | pnl=$%.3f <= $%.2f, halting entries',
                    pnl_today, DAILY_PNL_STOP)
        with SIGNALS.open() as f:
            f.seek(0, 2)
            state['last_byte_offset'] = f.tell()
        save_state(state)
        return 0

    # Read all lines from offset; track byte position manually (fh.tell() is
    # disabled inside `for ln in fh` iteration in CPython).
    new_count = 0
    with SIGNALS.open('rb') as fh_raw:
        fh_raw.seek(last_offset)
        raw = fh_raw.read()
    if not raw:
        save_state(state)
        conn.close()
        LOG.info('done | no new bytes')
        return 0
    new_offset = last_offset + len(raw)
    for line_bytes in raw.split(b'\n'):
        ln = line_bytes.decode('utf-8', errors='replace').strip()
        if True:
            if not ln:
                continue
            try:
                s = json.loads(ln)
            except Exception:
                continue

            wallet = s.get('wallet', '')
            short = wallet[:18]
            if short not in FADE_LIST:
                continue
            if s.get('side') != 'BUY' or s.get('outcome') != 'Yes':
                continue
            their_size = s.get('size', 0) or 0
            their_price = s.get('price', 0) or 0
            if their_size <= 0 or their_price <= 0 or their_price >= 1:
                continue

            # Reject signals older than MAX_SIGNAL_AGE_HOURS — markets likely resolved
            fill_ts = s.get('fill_ts', 0) or 0
            if fill_ts:
                age_hours = (datetime.now(timezone.utc).timestamp() - fill_ts) / 3600
                if age_hours > MAX_SIGNAL_AGE_HOURS:
                    continue

            title = (s.get('title') or '').lower()
            city, date = parse_city_date(title)
            bucket_temp = parse_temp(title)
            if not (city and date and bucket_temp):
                continue

            condition_id = (s.get('condition_id') or '').lower()
            if not condition_id:
                continue
            # Lookup numeric market_id (scanner needs this for orphan-check + TP/SL)
            market_id = cid_map.get(condition_id)
            if not market_id:
                LOG.info('skip | condition_id=%s not in active markets', condition_id[:20])
                continue

            our_entry = round(1.0 - their_price, 4)
            if our_entry > MAX_ENTRY_PRICE or our_entry < MIN_ENTRY_PRICE:
                LOG.info('skip | market=%s our_entry=%s out of [%s,%s]',
                         market_id[:18], our_entry, MIN_ENTRY_PRICE, MAX_ENTRY_PRICE)
                continue
            our_size = round(SIZE_USD / our_entry, 4) if our_entry > 0 else 0
            if our_size <= 0:
                continue

            if has_open_position(conn, market_id):
                LOG.info('skip | market=%s already_open', market_id[:18])
                continue
            if trades_per_event(conn, city, date) >= CLUSTER_MAX_PER_EVENT:
                LOG.info('skip | %s/%s cluster_cap reached', city, date)
                continue
            if open_events_count(conn) >= MAX_OPEN_EVENTS:
                LOG.info('skip | max_open_events=%s reached', MAX_OPEN_EVENTS)
                break
            if placed_today >= DAILY_CAP:
                LOG.info('cap_hit | mid_loop after %s placed', placed_today)
                break

            note = (
                f"sm_fade wallet={short} their=BUY-Yes@${their_price:.4f}x{their_size:.0f} "
                f"city={city} date={date} bucket={bucket_temp} cid={condition_id[:20]}"
            )
            order_id = insert_fade_trade(conn, market_id, our_entry, our_size, note)
            placed_today += 1
            new_count += 1
            LOG.info(
                'PLACED | order_id=%s market=%s our=BUY-NO@$%.4fx%.2f → $%.2f | %s',
                order_id, market_id[:18], our_entry, our_size, our_entry * our_size, note
            )

    state['last_byte_offset'] = new_offset
    save_state(state)
    conn.close()

    LOG.info('done | new_trades=%s placed_today=%s', new_count, placed_today)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
