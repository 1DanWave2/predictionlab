"""Telegram Relay Worker — receive GPT replies from Telegram chat (no n8n needed).

Polls Telegram getUpdates, extracts:
  - `/gpt_reply <text>`  → append text as [GPT N] in AI_DEBATE.md
  - document upload (.md / .txt) — download, parse as [GPT N]
  - reply to "Claude N" forwarded message — also treated as GPT reply

Long-polling via getUpdates, runs in cron-driven shot mode (one polling pass per call).
Default timeout 25s — fits inside cron */1 budget.

Env:
  TELEGRAM_BOT_TOKEN    — required
  TELEGRAM_CHAT_ID      — required (only this chat can post replies)
  AI_DEBATE_FILE        — default /app/_claude_bundle/AI_DEBATE.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx

DEFAULT_DEBATE = Path(os.environ.get(
    "AI_DEBATE_FILE",
    "/app/_claude_bundle/AI_DEBATE.md",
))
STATE_PATH = Path("/app/data/tg_relay_state.json")
LOG_PATH = Path("/app/data/tg_relay_calls.jsonl")

CMD_GPT_REPLY = "/gpt_reply"
CMD_STATUS = "/status_bridge"
ACCEPTED_DOC_EXTS = (".md", ".txt", ".markdown")

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"last_update_id": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def log_event(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Telegram API helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_updates(client: httpx.Client, bot: str, offset: int, timeout: int) -> list[dict]:
    try:
        r = client.get(
            f"https://api.telegram.org/bot{bot}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 10,
        )
        r.raise_for_status()
        return r.json().get("result") or []
    except Exception as exc:
        logger.warning("getUpdates_failed | err=%s", exc)
        return []


def send_message(client: httpx.Client, bot: str, chat: str, text: str) -> None:
    try:
        client.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("send_failed | err=%s", exc)


def get_file_path(client: httpx.Client, bot: str, file_id: str) -> str | None:
    try:
        r = client.get(
            f"https://api.telegram.org/bot{bot}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["result"]["file_path"]
    except Exception as exc:
        logger.warning("getFile_failed | err=%s", exc)
        return None


def download_file(client: httpx.Client, bot: str, file_path: str) -> str | None:
    try:
        r = client.get(
            f"https://api.telegram.org/file/bot{bot}/{file_path}",
            timeout=30,
        )
        r.raise_for_status()
        return r.content.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("download_failed | err=%s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# AI_DEBATE.md write
# ──────────────────────────────────────────────────────────────────────────────

def get_max_gpt_n(text: str) -> int:
    pattern = re.compile(r"^##\s+\[GPT\s+(\d+)\]", re.MULTILINE)
    nums = [int(m.group(1)) for m in pattern.finditer(text)]
    return max(nums) if nums else 0


def append_gpt_response(file_path: Path, content: str) -> int:
    """Append `## [GPT N+1]` block. Returns the assigned N."""
    text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    next_n = get_max_gpt_n(text) + 1
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n---\n\n## [GPT {next_n}]\n\n{content.strip()}\n"
    file_path.write_text(text, encoding="utf-8")
    return next_n


def get_last_claude_n(file_path: Path) -> int:
    if not file_path.exists():
        return 0
    text = file_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^##\s+\[Claude\s+(\d+)\]", re.MULTILINE)
    nums = [int(m.group(1)) for m in pattern.finditer(text)]
    return max(nums) if nums else 0


# ──────────────────────────────────────────────────────────────────────────────
# Message handlers
# ──────────────────────────────────────────────────────────────────────────────

def is_authorized(msg: dict, allowed_chat_id: str) -> bool:
    chat_id = msg.get("chat", {}).get("id")
    return str(chat_id) == str(allowed_chat_id)


def handle_text_command(
    text: str, msg: dict, debate_file: Path,
    client: httpx.Client, bot: str, chat: str,
) -> dict | None:
    text = text.strip()

    # /status_bridge — show last Claude / GPT N
    if text.startswith(CMD_STATUS):
        last_claude = get_last_claude_n(debate_file)
        last_gpt = get_max_gpt_n(debate_file.read_text() if debate_file.exists() else "")
        send_message(
            client, bot, chat,
            f"📊 *AI Debate Bridge Status*\n\n"
            f"Last \\[Claude N\\]: *{last_claude}*\n"
            f"Last \\[GPT N\\]: *{last_gpt}*\n"
            f"Pending: {'GPT reply needed' if last_claude > last_gpt else 'all caught up'}",
        )
        return {"kind": "status_query"}

    # /gpt_reply <text>
    if text.startswith(CMD_GPT_REPLY):
        body = text[len(CMD_GPT_REPLY):].strip()
        if not body:
            send_message(client, bot, chat, "❌ `/gpt_reply <text>` — текст не должен быть пустым")
            return {"kind": "empty_reply"}
        if len(body) < 50:
            send_message(client, bot, chat,
                         "⚠️ Ответ короче 50 символов — отправь больше контекста или приложи файл")
            return {"kind": "too_short"}

        gpt_n = append_gpt_response(debate_file, body)
        send_message(
            client, bot, chat,
            f"✅ \\[GPT {gpt_n}\\] записан в AI\\_DEBATE.md\n"
            f"📝 {len(body)} chars\n\n"
            f"_Скажи Claude \"gpt ответил\" для следующего раунда._",
        )
        return {"kind": "gpt_reply_text", "gpt_n": gpt_n, "chars": len(body)}

    return None


def handle_document(
    msg: dict, debate_file: Path,
    client: httpx.Client, bot: str, chat: str,
) -> dict | None:
    doc = msg.get("document") or {}
    file_name = (doc.get("file_name") or "").lower()
    if not file_name.endswith(ACCEPTED_DOC_EXTS):
        send_message(client, bot, chat,
                     f"❌ Файл `{file_name}` не поддерживается. Отправь .md или .txt.")
        return {"kind": "wrong_doc_type", "file_name": file_name}

    file_id = doc.get("file_id")
    file_path = get_file_path(client, bot, file_id)
    if not file_path:
        send_message(client, bot, chat, "❌ Не смог получить путь файла")
        return {"kind": "getfile_failed"}

    body = download_file(client, bot, file_path)
    if not body:
        send_message(client, bot, chat, "❌ Не смог скачать файл")
        return {"kind": "download_failed"}

    body = body.strip()
    if len(body) < 50:
        send_message(client, bot, chat,
                     f"⚠️ Содержимое файла слишком короткое ({len(body)} chars)")
        return {"kind": "too_short_doc"}

    # Strip Markdown header line if present (e.g., "# [GPT 32]")
    body = re.sub(r"^#+\s+\[?GPT[^\n]*\n+", "", body, count=1)

    gpt_n = append_gpt_response(debate_file, body)
    send_message(
        client, bot, chat,
        f"✅ \\[GPT {gpt_n}\\] записан из файла `{file_name}`\n"
        f"📝 {len(body)} chars\n\n"
        f"_Скажи Claude \"gpt ответил\" для следующего раунда._",
    )
    return {"kind": "gpt_reply_doc", "gpt_n": gpt_n, "chars": len(body), "file_name": file_name}


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_DEBATE)
    parser.add_argument("--timeout", type=int, default=25, help="long-poll timeout seconds")
    parser.add_argument("--max-rounds", type=int, default=10,
                        help="max getUpdates calls per invocation")
    args = parser.parse_args()

    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID required")
        return 2

    state = load_state()
    offset = state.get("last_update_id", 0) + 1
    logger.info("tg_relay.start | offset=%d timeout=%ds", offset, args.timeout)

    handled_count = 0
    with httpx.Client() as client:
        for round_n in range(args.max_rounds):
            updates = get_updates(client, bot, offset, args.timeout)
            if not updates:
                logger.info("no updates — exit")
                break

            for upd in updates:
                upd_id = upd.get("update_id")
                if upd_id is not None:
                    offset = upd_id + 1
                    state["last_update_id"] = upd_id

                msg = upd.get("message") or {}
                if not msg or not is_authorized(msg, chat):
                    continue

                text = msg.get("text") or ""
                doc = msg.get("document")

                result: dict | None = None
                if text:
                    result = handle_text_command(text, msg, args.file, client, bot, chat)
                elif doc:
                    result = handle_document(msg, args.file, client, bot, chat)

                if result:
                    handled_count += 1
                    log_event({
                        "ts": int(time.time()),
                        "update_id": upd_id,
                        **result,
                    })

            save_state(state)

            # On first non-empty round, do one more short poll to flush, then exit
            updates = get_updates(client, bot, offset, 1)
            if not updates:
                break
            for upd in updates:
                upd_id = upd.get("update_id")
                if upd_id is not None:
                    offset = upd_id + 1
                    state["last_update_id"] = upd_id
                msg = upd.get("message") or {}
                if not msg or not is_authorized(msg, chat):
                    continue
                text = msg.get("text") or ""
                doc = msg.get("document")
                result = None
                if text:
                    result = handle_text_command(text, msg, args.file, client, bot, chat)
                elif doc:
                    result = handle_document(msg, args.file, client, bot, chat)
                if result:
                    handled_count += 1
                    log_event({"ts": int(time.time()), "update_id": upd_id, **result})
            save_state(state)
            break

    logger.info("tg_relay.done | handled=%d offset=%d", handled_count, offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
