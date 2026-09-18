"""Morning report — daily Telegram summary at 07:00 UTC.

Cron on server: 0 7 * * *  (replaces unreliable Mac `at` jobs).

Pulls:
  - dashboard runtime (balance, PnL, last_tick)
  - last 24h closed positions per bucket
  - weather_resolutions count (toward 50)
  - last debate post
  - sm_fade canary status

Sends formatted Telegram message.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx

LOG = logging.getLogger(__name__)
DB = '/app/data/paper_bot.db'
RESOLUTIONS = Path('/app/data/weather_resolutions.jsonl')
DEBATE = Path('/app/_claude_bundle/AI_DEBATE.md')


def fetch_dashboard() -> dict:
    """Try multiple URLs — bot binds 0.0.0.0:8000 inside container."""
    for url in (
        "http://127.0.0.1:8000/dashboard/data",
        "http://localhost:8000/dashboard/data",
    ):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read())
        except Exception as exc:
            LOG.warning("dashboard fetch %s err: %s", url, exc)
    return {}


def count_resolutions() -> int:
    if not RESOLUTIONS.exists():
        return 0
    return sum(1 for ln in RESOLUTIONS.open() if ln.strip())


def last_debate_post() -> str:
    if not DEBATE.exists():
        return "?"
    last = "?"
    with DEBATE.open() as fh:
        for ln in fh:
            if ln.startswith("## ["):
                last = ln.strip()[3:].split('—')[0].strip()
    return last


def positions_24h() -> tuple[list, list]:
    """Return (closed_24h, open_now)."""
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    closed = c.execute(
        "SELECT bucket, COUNT(*), SUM(realized_pnl), "
        "SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) "
        "FROM positions WHERE updated_at >= datetime('now','-24 hours') "
        "AND quantity=0 GROUP BY bucket ORDER BY 2 DESC"
    ).fetchall()
    open_now = c.execute(
        "SELECT bucket, COUNT(*), SUM(quantity*avg_price), SUM(unrealized_pnl) "
        "FROM positions WHERE quantity>0 GROUP BY bucket"
    ).fetchall()
    conn.close()
    return closed, open_now


def send_telegram(text: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        LOG.warning("no TG creds; would send first 200 chars: %s", text[:200])
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            # Plain text — markdown escapes get fragile with [Claude N] etc.
            client.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text},
            )
    except Exception as exc:
        LOG.warning("telegram_send_failed | err=%s", exc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    now = datetime.now(timezone.utc)
    dash = fetch_dashboard()
    rt = dash.get("runtime", {})
    closed, open_pos = positions_24h()
    res_n = count_resolutions()
    last_post = last_debate_post()

    lines = [f"Morning Report — {now.strftime('%Y-%m-%d %H:%M UTC')}"]
    lines.append("")
    lines.append("Bot status:")
    lines.append(f"  balance:    ${rt.get('current_balance', 0):.2f}")
    lines.append(f"  realized:   ${rt.get('realized_pnl', 0):+.2f}")
    lines.append(f"  unrealized: ${rt.get('unrealized_pnl', 0):+.2f}")
    lines.append(f"  in_positions: ${rt.get('in_positions', 0):.2f}")
    lines.append(f"  last_tick:  {(rt.get('last_tick_at') or '?')[:19]}")

    if closed:
        lines.append("")
        lines.append("Closed last 24h:")
        total = 0.0
        for b, n, pnl, wins in closed:
            total += float(pnl or 0)
            wr = (wins or 0) * 100 / max(n, 1)
            lines.append(f"  {b or 'NULL'}: {n} trades | ${pnl or 0:+.2f} | WR {wr:.0f}%")
        lines.append(f"  TOTAL 24h: ${total:+.2f}")
    else:
        lines.append("")
        lines.append("No closed trades in last 24h")

    if open_pos:
        lines.append("")
        lines.append("Open now:")
        for b, n, exp, unr in open_pos:
            lines.append(f"  {b or 'NULL'}: {n} pos | exposure ${exp or 0:.2f} | unr ${unr or 0:+.2f}")

    lines.append("")
    lines.append(f"Weather resolutions: {res_n} / 50 (fade canary gate)")
    lines.append(f"Last debate post: {last_post}")

    msg = "\n".join(lines)
    send_telegram(msg)
    LOG.info("morning report sent | balance=$%.2f", rt.get('current_balance', 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
