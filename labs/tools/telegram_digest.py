"""Post a lab digest to a Telegram channel: newest CHANGELOG entry as caption plus the figures.

    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHANNEL_ID=@channel python3 telegram_digest.py labs/<lab-folder>

Exits 0 without sending when the two environment variables are missing, so the weekly
workflow can stay in the repo before the channel exists. Uses the Bot API directly.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
FIGURES = ["longshots.png", "fade_by_horizon.png", "by_category.png", "calibration.png"]
CAPTION_LIMIT = 1024


def newest_entry(changelog: Path) -> str:
    text = changelog.read_text(encoding="utf-8")
    m = re.search(r"^## (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        return ""
    date, body = m.group(1), m.group(2)
    # keep the summary lines and alerts, drop the numbers table (figures carry the numbers)
    lines = [ln for ln in body.strip().splitlines() if not ln.startswith("|")]
    return f"{date}\n" + "\n".join(lines).strip()


def to_plain(md: str) -> str:
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    md = re.sub(r"`(.+?)`", r"\1", md)
    return md


def main() -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHANNEL_ID")
    if not token or not chat:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not set; nothing sent", file=sys.stderr)
        return
    lab = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    title = "PredictionLab · Lab 1 · weekly refresh"
    entry = newest_entry(lab / "CHANGELOG.md") if (lab / "CHANGELOG.md").exists() else ""
    link = f"https://github.com/1DanWave2/predictionlab/tree/main/{lab.as_posix()}"
    caption = to_plain(f"{title}\n\n{entry}\n\n{link}")[:CAPTION_LIMIT]

    media, files = [], {}
    for i, name in enumerate(FIGURES):
        p = lab / "figures" / name
        if not p.exists():
            continue
        key = f"fig{i}"
        files[key] = (name, p.read_bytes(), "image/png")
        item = {"type": "photo", "media": f"attach://{key}"}
        if not media:
            item["caption"] = caption
        media.append(item)
    if not media:
        print("no figures found; nothing sent", file=sys.stderr)
        return
    import json
    r = httpx.post(API.format(token=token, method="sendMediaGroup"), data={"chat_id": chat, "media": json.dumps(media)},
                   files=files, timeout=60)
    r.raise_for_status()
    print(f"sent {len(media)} photos to {chat}", file=sys.stderr)


if __name__ == "__main__":
    main()
