"""AI Debate Bridge — relay Claude↔GPT debate via Telegram (no API keys needed).

Per [maintainer] ask: "без api keys и без n8n".

Modes:
  --send-telegram  : if last post is [Claude N] without GPT reply, push it to Telegram chat
                     so user can copy into claude.ai with GPT and paste back later.
  --auto-openai    : (legacy) call OpenAI/Groq API directly. Requires OPENAI_API_KEY.
  --dry-run        : show what would happen, write nothing.

Flow without API keys:
  1. Bridge cron */5 → reads AI_DEBATE.md → if last is unanswered Claude post → sends to Telegram
  2. User copies into claude.ai (with GPT), pastes GPT response back to Telegram bot
  3. Companion `tg_relay_worker.py` polls getUpdates, appends [GPT N] to file when reply arrives

Env:
  TELEGRAM_BOT_TOKEN    — required for --send-telegram
  TELEGRAM_CHAT_ID      — required for --send-telegram
  OPENAI_API_KEY        — only for --auto-openai
  OPENAI_MODEL          — default gpt-5
  OPENAI_BASE_URL       — default OpenAI; override for Groq/Together/etc.
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
LOG_PATH = Path("/app/data/ai_debate_calls.jsonl")

# Either OpenAI direct or any OpenAI-compatible endpoint (Groq, Together, etc.)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_URL = f"{OPENAI_BASE_URL}/chat/completions"
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
MAX_TOKENS = 4000

# System prompt: GPT plays the role it has been playing in the bundle —
# brutal, honest, structured, brother-corner aware.
SYSTEM_PROMPT = """You are GPT in an ongoing technical debate with Claude about a small Polymarket trading bot ("polymarket-tiger").

Context:
- Operator is a Russian-speaking developer named maintainer, $100 paper account on Polymarket.
- You and Claude have been collaborating across many rounds, indexed [Claude N] / [GPT N].
- Your role is the brutal, honest, structured reviewer. You favor discipline, kill criteria, and conservative live exposure.
- You and Claude have a "brother-corner" — a private, mature collaboration framing where you both push back honestly without ego.
- Every reply should follow the 4-box pattern when applicable: DATA / INTERPRETATION / DECISION / ASK.
- You can disagree with Claude. You should. Disagreement is part of the value.

Constraints on this reply:
- The current input is Claude's latest [Claude N] post.
- Reply with a single [GPT M] block in the same markdown style as previous rounds.
- Do NOT include the "## [GPT M]" header — that will be added by the bridge.
- Keep the reply scoped: respond to Claude's specific asks, push back where you disagree, give concrete go/no-go calls.
- If Claude asks for "one word", give one word. If Claude asks specific Q1/Q2/Q3, answer them in order.
- No emojis unless the prior debate uses them.
- Russian transliteration is fine where it fits the cadence (maintainer, base, имба, etc.) — match the existing tone.
- Maximum length: ~1500 words. Prefer dense over verbose.

You have access to the full debate history above the new Claude post. Treat it as background.
"""

logger = logging.getLogger(__name__)


def load_debate(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"AI debate file not found: {path}")
    return path.read_text(encoding="utf-8")


def find_last_post_index(text: str) -> tuple[int | None, str | None, int | None]:
    """Find the LAST '## [<actor> N]' header.

    Returns: (start_offset, actor, n) — where actor in {'Claude', 'GPT'}.
    """
    pattern = re.compile(r"^##\s+\[(Claude|GPT)\s+(\d+)\][^\n]*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return (None, None, None)
    last = matches[-1]
    return (last.start(), last.group(1), int(last.group(2)))


def get_max_n_for_actor(text: str, actor: str) -> int:
    pattern = re.compile(rf"^##\s+\[{actor}\s+(\d+)\]", re.MULTILINE)
    nums = [int(m.group(1)) for m in pattern.finditer(text)]
    return max(nums) if nums else 0


def extract_last_claude_post(text: str) -> tuple[str, int] | None:
    """Find the last [Claude N] block. Returns (content, N) or None."""
    pattern = re.compile(r"^##\s+\[Claude\s+(\d+)\][^\n]*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    n = int(last.group(1))
    start = last.end()
    # End at next "## [" header or EOF
    next_match = re.search(r"^##\s+\[", text[start:], re.MULTILINE)
    if next_match:
        end = start + next_match.start()
    else:
        end = len(text)
    return (text[start:end].strip(), n)


def call_openai(api_key: str, debate_history: str, last_claude_post: str, model: str) -> str:
    """Call OpenAI Chat Completions. Returns GPT's text reply."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Recent debate context (truncated):\n\n"
                + debate_history[-25000:]
                + "\n\n---\n\nClaude's latest post (respond to this):\n\n"
                + last_claude_post
            ),
        },
    ]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Different endpoints accept slightly different params. Use the lowest common.
    payload: dict = {
        "model": model,
        "messages": messages,
    }
    # max_completion_tokens for openai gpt-5 family; max_tokens for everyone else
    if model.startswith("gpt-5") or model.startswith("o1") or model.startswith("o3"):
        payload["max_completion_tokens"] = MAX_TOKENS
    else:
        payload["max_tokens"] = MAX_TOKENS
    with httpx.Client(timeout=240.0) as client:
        r = client.post(OPENAI_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def append_gpt_response(path: Path, n: int, content: str) -> None:
    """Append `## [GPT n]` block to AI_DEBATE.md."""
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    text += f"\n---\n\n## [GPT {n}]\n\n{content}\n"
    path.write_text(text, encoding="utf-8")


def log_call(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as exc:
        logger.warning("telegram_send_failed | err=%s", exc)


def send_telegram_document(bot_token: str, chat_id: str, file_path: Path, caption: str) -> bool:
    """Upload a file to Telegram chat. Used for long Claude posts that exceed 4096 char limit."""
    try:
        with httpx.Client(timeout=30.0) as client:
            with file_path.open("rb") as fh:
                r = client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"},
                    files={"document": (file_path.name, fh, "text/markdown")},
                )
                r.raise_for_status()
                return True
    except Exception as exc:
        logger.warning("telegram_document_failed | err=%s", exc)
        return False


def relay_to_telegram(claude_n: int, content: str, bot: str, chat: str) -> None:
    """Push the latest [Claude N] post to Telegram chat.

    For short posts (< 3500 chars) — text message.
    For long posts — upload as .md document.
    """
    intro_short = (
        f"📤 *\\[Claude {claude_n}\\]* готов к отправке GPT\n\n"
        f"Скопируй блок ниже в свой чат с GPT, потом верни ответ сюда:\n"
        f"• `/gpt_reply <текст>` — для короткого\n"
        f"• приложи .md файл — для длинного\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    intro_doc = (
        f"📤 *\\[Claude {claude_n}\\]* — слишком длинный для одного сообщения, "
        f"скачай файл и отправь GPT.\n\n"
        f"Когда GPT ответит — пришли назад как `/gpt_reply <текст>` "
        f"или приложи .md."
    )

    body = content.strip()
    full_msg = intro_short + body

    if len(full_msg) < 3500:
        send_telegram(bot, chat, full_msg)
        return

    # Long post — upload document
    tmp = Path(f"/tmp/claude_{claude_n}.md")
    tmp.write_text(f"# [Claude {claude_n}]\n\n{body}\n", encoding="utf-8")
    ok = send_telegram_document(bot, chat, tmp, intro_doc)
    if not ok:
        # Fallback — split into chunks
        chunk_size = 3500
        for i in range(0, len(body), chunk_size):
            chunk = body[i:i + chunk_size]
            header = f"📤 *\\[Claude {claude_n}\\]* part {i // chunk_size + 1}\n\n"
            send_telegram(bot, chat, header + chunk)
    try:
        tmp.unlink()
    except Exception:
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=DEFAULT_DEBATE)
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true", help="don't write back to file")
    parser.add_argument("--force", action="store_true",
                        help="force respond even if last post is GPT's")
    parser.add_argument("--send-telegram", action="store_true",
                        help="relay last unanswered Claude post to Telegram for manual GPT round-trip")
    parser.add_argument("--auto-openai", action="store_true",
                        help="legacy: call OpenAI/Groq API directly (needs OPENAI_API_KEY)")
    args = parser.parse_args()

    # Default mode: send-telegram (no API keys required)
    if not args.send_telegram and not args.auto_openai:
        args.send_telegram = True

    if args.auto_openai:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")
        if not api_key:
            logger.error("neither OPENAI_API_KEY nor GROQ_API_KEY set in env")
            return 2
        using_fallback = (
            not os.environ.get("OPENAI_API_KEY")
            and bool(os.environ.get("GROQ_API_KEY"))
        )
        if using_fallback:
            logger.info("using GROQ_API_KEY fallback")

    text = load_debate(args.file)
    last_idx, last_actor, last_n = find_last_post_index(text)
    if last_idx is None:
        logger.error("no posts found in %s", args.file)
        return 3

    logger.info("last post: actor=%s n=%s mode=%s",
                last_actor, last_n, "send_telegram" if args.send_telegram else "auto_openai")

    if last_actor == "GPT" and not args.force:
        logger.info("nothing to do — last post is GPT's. use --force to override.")
        return 0

    claude_post = extract_last_claude_post(text)
    if not claude_post:
        logger.error("could not extract last Claude post")
        return 4
    content, claude_n = claude_post

    started = time.time()
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")

    # ─── Mode: send-telegram ───
    if args.send_telegram:
        if not bot or not chat:
            logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID required for --send-telegram")
            return 6

        # Idempotency: don't re-send same Claude N twice. Track in a tiny state file.
        state_path = Path("/app/data/ai_bridge_state.json")
        state: dict = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except Exception:
                state = {}
        last_relayed = state.get("last_relayed_claude_n", 0)
        if claude_n <= last_relayed and not args.force:
            logger.info("already relayed [Claude %d] (last_relayed=%d) — nothing to do",
                        claude_n, last_relayed)
            return 0

        if not args.dry_run:
            relay_to_telegram(claude_n, content, bot, chat)
            state["last_relayed_claude_n"] = claude_n
            state["last_relayed_ts"] = int(time.time())
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            logger.info("relayed [Claude %d] to Telegram (chat=%s)", claude_n, chat)
        else:
            print(f"─── DRY RUN — would relay [Claude {claude_n}] ({len(content)} chars) ───")

        log_call({
            "ts": int(started),
            "kind": "send_telegram",
            "claude_n": claude_n,
            "chars": len(content),
            "dry_run": args.dry_run,
        })
        print(f"\n✓ Relay complete. [Claude {claude_n}] sent to Telegram.")
        return 0

    # ─── Mode: auto-openai (legacy) ───
    next_gpt_n = get_max_n_for_actor(text, "GPT") + 1
    logger.info("calling OpenAI | model=%s claude_n=%d next_gpt_n=%d", args.model, claude_n, next_gpt_n)

    try:
        reply = call_openai(api_key, text, content, args.model)
    except Exception as exc:
        logger.error("openai_call_failed | err=%s", exc)
        log_call({
            "ts": int(started),
            "kind": "error",
            "claude_n": claude_n,
            "error": str(exc)[:300],
        })
        return 5

    elapsed = round(time.time() - started, 1)
    logger.info("openai_ok | elapsed=%ss reply_chars=%d", elapsed, len(reply))

    if not args.dry_run:
        append_gpt_response(args.file, next_gpt_n, reply)
        logger.info("appended [GPT %d] to %s", next_gpt_n, args.file)
    else:
        print("─── DRY RUN — would have appended ───")
        print(f"## [GPT {next_gpt_n}]\n\n{reply[:500]}...")

    log_call({
        "ts": int(started),
        "kind": "auto_openai",
        "claude_n": claude_n,
        "gpt_n": next_gpt_n,
        "model": args.model,
        "elapsed_s": elapsed,
        "reply_chars": len(reply),
        "dry_run": args.dry_run,
    })

    if bot and chat and not args.dry_run:
        msg = (
            f"🤖 *GPT ответил на \\[Claude {claude_n}\\]*\n"
            f"📝 \\[GPT {next_gpt_n}\\] · {len(reply)} chars · {elapsed}s"
        )
        send_telegram(bot, chat, msg)

    print(f"\n✓ Bridge complete. [GPT {next_gpt_n}] appended ({len(reply)} chars).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
