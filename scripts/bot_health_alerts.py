"""Bot health alerts — operational floor per [GPT 44] brother-corner.

Cron: */10min on server with TELEGRAM_BOT_TOKEN/CHAT_ID env.

Checks (each with sentinel state to avoid duplicate alerts):
  - disk_usage > 85%               → alert + recheck after 6h
  - paper_bot.db > 1.5GB           → alert + recheck after 12h
  - gamma_fallback in last 30min   → alert + recheck after 30min
  - no_real_fills_for_2h during    → alert (active hours 06:00-22:00 UTC)
       active hours (means bot is silent OR all rejected)

Sentinel: /app/data/health_alerts_state.json — tracks last_alert_ts per check.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

LOG = logging.getLogger(__name__)
DB = Path('/app/data/paper_bot.db')
STATE = Path('/app/data/health_alerts_state.json')

# Thresholds
DISK_PCT_THRESHOLD = 85
DB_SIZE_GB_THRESHOLD = 1.5
NO_FILLS_HOURS = 2.0
ACTIVE_HOUR_FROM = 6
ACTIVE_HOUR_TO = 22

# Recheck intervals (seconds)
RECHECK = {
    "disk": 6 * 3600,
    "db_size": 12 * 3600,
    "gamma_fallback": 30 * 60,
    "no_fills": 60 * 60,
}


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


def send_telegram(text: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        LOG.warning("no TG creds; would send: %s", text[:80])
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as exc:
        LOG.warning("telegram_send_failed | err=%s", exc)


def maybe_alert(state: dict, key: str, msg: str) -> None:
    now = time.time()
    last = state.get(f"last_alert_{key}", 0)
    if now - last < RECHECK.get(key, 3600):
        LOG.info("alert %s suppressed (cooldown)", key)
        return
    send_telegram(msg)
    state[f"last_alert_{key}"] = now
    LOG.info("alert sent | %s", key)


def check_disk(state: dict) -> None:
    """Check root disk usage."""
    try:
        total, used, free = shutil.disk_usage("/")
        pct = used * 100 / total
        LOG.info("disk_usage | %.1f%% used (%.1f GB free)", pct, free / 1e9)
        if pct >= DISK_PCT_THRESHOLD:
            msg = (
                f"🔴 *DISK ALERT*\n"
                f"Root disk: *{pct:.1f}%* used\n"
                f"Free: {free/1e9:.1f} GB\n"
                f"\n"
                f"Action: clean DB, rotate logs, OR ssh into the host"
            )
            maybe_alert(state, "disk", msg)
    except Exception as exc:
        LOG.warning("disk check err: %s", exc)


def check_db_size(state: dict) -> None:
    """Check paper_bot.db size."""
    if not DB.exists():
        return
    size_gb = DB.stat().st_size / 1e9
    LOG.info("db_size | %.2f GB", size_gb)
    if size_gb >= DB_SIZE_GB_THRESHOLD:
        msg = (
            f"🟡 *DB SIZE ALERT*\n"
            f"paper_bot.db: *{size_gb:.2f} GB*\n"
            f"\n"
            f"Action: docker exec polymarket-bot python3 /tmp/db_cleanup.py "
            f"(or run from Claude session)"
        )
        maybe_alert(state, "db_size", msg)


def check_gamma_fallback(state: dict) -> None:
    """Scan recent docker logs for gamma fallback warnings."""
    cutoff = datetime.now(timezone.utc).timestamp() - 30 * 60  # last 30 min
    log_file = Path("/var/log/bot_runtime.log")  # if exists
    # Fallback: query recent ticks via runtime data — markets count proxy
    # If the most recent tick has markets <= 3, gamma is probably dead
    try:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        # Check if recent market_snapshots is decreasing
        n_recent = c.execute(
            "SELECT COUNT(DISTINCT market_id) FROM market_snapshots "
            "WHERE created_at >= datetime('now','-15 minutes')"
        ).fetchone()[0]
        conn.close()
        LOG.info("gamma_proxy | distinct markets last 15min: %s", n_recent)
        if n_recent <= 3:
            msg = (
                f"🟠 *GAMMA FALLBACK SUSPECTED*\n"
                f"Distinct markets in last 15min: *{n_recent}*\n"
                f"(normal: 30-50, mock fallback gives 2-3)\n"
                f"\n"
                f"Action: check `docker logs polymarket-bot | grep gamma`"
            )
            maybe_alert(state, "gamma_fallback", msg)
    except Exception as exc:
        LOG.warning("gamma check err: %s", exc)


def check_no_fills(state: dict) -> None:
    """During active hours (06-22 UTC), if no real paper_orders for 2h → alert.

    Excludes sm_fade_canary (it has its own daily cap, may legitimately be silent).
    """
    now_hour = datetime.now(timezone.utc).hour
    if not (ACTIVE_HOUR_FROM <= now_hour < ACTIVE_HOUR_TO):
        LOG.info("no_fills check skipped (off hours)")
        return
    try:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        cutoff_str = (datetime.now(timezone.utc).timestamp() - NO_FILLS_HOURS * 3600)
        # Count paper_orders since cutoff
        n = c.execute(
            "SELECT COUNT(*) FROM paper_orders "
            "WHERE strategy != 'sm_fade_0xc80fa1fc_canary' "
            f"AND created_at >= datetime('now','-{int(NO_FILLS_HOURS*60)} minutes')"
        ).fetchone()[0]
        conn.close()
        LOG.info("no_fills | non-sm_fade orders last %.1fh: %s", NO_FILLS_HOURS, n)
        if n == 0:
            msg = (
                f"🟡 *BOT QUIET*\n"
                f"Zero non-sm_fade paper_orders in last *{NO_FILLS_HOURS:.1f}h* "
                f"during active hours ({ACTIVE_HOUR_FROM:02d}-{ACTIVE_HOUR_TO:02d} UTC).\n"
                f"\n"
                f"Possible causes:\n"
                f"• fade_any disabled (intended)\n"
                f"• Risk manager rejecting all signals\n"
                f"• No setups today (could be normal)\n"
                f"• Pipeline broken (worth checking logs)"
            )
            maybe_alert(state, "no_fills", msg)
    except Exception as exc:
        LOG.warning("no_fills check err: %s", exc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    state = load_state()
    check_disk(state)
    check_db_size(state)
    check_gamma_fallback(state)
    check_no_fills(state)
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
