"""Watcher: Telegram alert when weather_resolutions count crosses thresholds.

Per [Claude 50/51] plan: fade canary deployment gated on 50 resolutions.

Cron: */30 * * * * with TELEGRAM_BOT_TOKEN/CHAT_ID env.
Maintains sentinel file at /app/data/resolutions_watcher_state.json to avoid duplicate alerts.

Thresholds checked: 50, 75, 100, 150, 200.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

LOG = logging.getLogger(__name__)
RESOLUTIONS = Path("/app/data/weather_resolutions.jsonl")
STATE = Path("/app/data/resolutions_watcher_state.json")
THRESHOLDS = [50, 75, 100, 150, 200]


def count_resolutions() -> int:
    if not RESOLUTIONS.exists():
        return 0
    n = 0
    with RESOLUTIONS.open() as fh:
        for ln in fh:
            if ln.strip():
                n += 1
    return n


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    n = count_resolutions()
    state = load_state()
    alerted = set(state.get("alerted_thresholds", []))

    LOG.info("resolutions_count=%s alerted_thresholds=%s", n, sorted(alerted))

    new_alerts = []
    for th in THRESHOLDS:
        if n >= th and th not in alerted:
            new_alerts.append(th)

    if not new_alerts:
        return 0

    for th in new_alerts:
        if th == 50:
            msg = (
                f"🚨 *Weather resolutions = {n}* (≥50 hit)\n\n"
                f"FADE canary readiness gate per \\[Claude 50\\] cleared.\n"
                f"Action items:\n"
                f"• Re-run sm\\_mirror\\_backtest with fresh resolution sample\n"
                f"• If 0xc80fa1fc still 95%+ wrong on n≥10 events → propose $1 fade canary\n"
                f"• Apply \\[GPT 40\\] ramp gates: 25 trades + 24h + cluster diversity\n"
            )
        else:
            msg = f"📊 *Weather resolutions = {n}* (≥{th} hit). New backtest sample worth running."
        send_telegram(msg)
        alerted.add(th)
        LOG.info("alert sent for threshold %s (count=%s)", th, n)

    state["alerted_thresholds"] = sorted(alerted)
    state["last_count"] = n
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
