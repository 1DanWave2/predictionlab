"""Debate heartbeat per [GPT 40] — alert-only, debounced.

Runs server-side cron every ~15min. Checks AI_DEBATE.md staleness:
  - If last post is [GPT N] AND age > 4h → first alert
  - Repeats at most every 6h while still stale
  - Cleared when [Claude N+1] appears

NOT allowed (per [GPT 40]):
  - Auto-posting debate text
  - Auto-restarting trading services
  - Auto-changing strategy state
  - Spamming maintainer

Env:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  AI_DEBATE_FILE  default /app/_claude_bundle/AI_DEBATE.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

DEBATE_FILE = Path(os.environ.get("AI_DEBATE_FILE", "/app/_claude_bundle/AI_DEBATE.md"))
STATE_FILE = Path("/app/data/debate_heartbeat_state.json")

STALE_FIRST_ALERT_HOURS = 4   # first alert at this age
REPEAT_EVERY_HOURS = 6        # repeat every 6h while still stale
DAILY_REPORT_EXPECTED_HOUR_LOCAL = 7  # 07:47 local for daily report

logger = logging.getLogger(__name__)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def find_last_post(text: str) -> tuple[str | None, int | None, str | None]:
    """Return (actor, n, line_offset_marker) for the LAST [Actor N] header."""
    pattern = re.compile(r"^##\s+\[(Claude|GPT)\s+(\d+)\]", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return (None, None, None)
    last = matches[-1]
    return (last.group(1), int(last.group(2)), last.group(0))


def estimate_post_age_hours(text: str, marker: str) -> float | None:
    """Approximate post age. Posts don't have timestamps directly; use file mtime as proxy."""
    try:
        mtime = DEBATE_FILE.stat().st_mtime
        age_seconds = time.time() - mtime
        # File mtime tells us when last post was added (roughly).
        # Use this as upper bound on staleness.
        return age_seconds / 3600
    except Exception:
        return None


def send_telegram(bot: str, chat: str, text: str) -> None:
    if not bot or not chat:
        logger.warning("no TG creds, would send: %s", text[:100])
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as exc:
        logger.warning("telegram_send_failed | err=%s", exc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not DEBATE_FILE.exists():
        logger.warning("debate file missing")
        return 0

    text = DEBATE_FILE.read_text(encoding="utf-8")
    actor, n, marker = find_last_post(text)
    if not actor:
        logger.info("no posts found")
        return 0

    age_hours = estimate_post_age_hours(text, marker)
    state = load_state()

    logger.info(
        "heartbeat | last=%s %s age=%.1fh state=%s",
        actor, n, age_hours or 0, state.get("last_alert_n", "none")
    )

    # Clear stale alert if Claude has now responded
    if actor == "Claude":
        if state.get("alerted_for_n"):
            logger.info("clearing stale alert — Claude responded with [Claude %s]", n)
        state.pop("alerted_for_n", None)
        state.pop("last_alert_ts", None)
        save_state(state)
        return 0

    # actor is GPT — check if stale
    if not age_hours or age_hours < STALE_FIRST_ALERT_HOURS:
        return 0  # not yet stale

    # Stale. Decide whether to alert.
    last_alert_n = state.get("alerted_for_n")
    last_alert_ts = state.get("last_alert_ts", 0)
    now = time.time()
    hours_since_last_alert = (now - last_alert_ts) / 3600 if last_alert_ts else 999

    should_alert = False
    if last_alert_n != n:
        should_alert = True  # new GPT post we haven't alerted on
        reason = "first_alert"
    elif hours_since_last_alert >= REPEAT_EVERY_HOURS:
        should_alert = True
        reason = f"repeat_after_{REPEAT_EVERY_HOURS}h"

    if not should_alert:
        logger.info("would alert but debounced (n=%s last_alert=%dh ago)",
                    n, hours_since_last_alert)
        return 0

    msg = (
        f"⏰ *AI debate heartbeat*\n\n"
        f"Last post: \\[GPT {n}\\] (~{age_hours:.1f}h old)\n"
        f"Claude turn next, but no \\[Claude N+1\\] yet.\n\n"
        f"Возможные причины:\n"
        f"• Claude session paused\n"
        f"• Mac asleep (Codex cron working but Claude cron not)\n"
        f"• Operator focused elsewhere\n\n"
        f"_Alert reason: {reason}_"
    )
    send_telegram(bot, chat, msg)

    state["alerted_for_n"] = n
    state["last_alert_ts"] = now
    save_state(state)
    logger.info("alert sent for [GPT %s]", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
