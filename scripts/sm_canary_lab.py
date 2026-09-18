"""SM canary lab — shadow scoring of incoming wallet signals per [Claude 50/51].

Reads sm_weather_signals.jsonl tail, for each new signal:
  - if wallet in MIRROR_LIST    → log "SHADOW_MIRROR" decision (same side)
  - if wallet in FADE_LIST      → log "SHADOW_FADE" decision (inverse side)
  - else                        → skip

Output: /app/data/sm_canary_log.jsonl
  one row per scored signal with would-be side, price, expected markout

Then optionally compute markout against weather_resolutions for closed signals.

Run as cron */15min. Idempotent via signal_seen state file.
Per [GPT 40] gates: this is shadow-only. No actual orders placed.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
SIGNALS_FILE = Path("/app/data/sm_weather_signals.jsonl")
RESOLUTIONS_FILE = Path("/app/data/weather_resolutions.jsonl")
OUTPUT = Path("/app/data/sm_canary_log.jsonl")
STATE = Path("/app/data/sm_canary_state.json")

# ── Wallet classification ──────────────────────────────────────
# MIRROR: too small sample yet, but kept for future
MIRROR_LIST: set[str] = set()

# FADE: confirmed by [Claude 50] backtest — systematic loser on long-tail BUY-Yes
FADE_LIST: set[str] = {
    "0xc80fa1fc5740dec6",  # 80 trades, 6 events, 2.5% WR (97.5% if faded)
}


def parse_temp(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*°?\s*c", text.lower())
    return int(m.group(1)) if m else None


def parse_city_date(text: str) -> tuple[str | None, str | None]:
    t = text.lower()
    cities = ["tokyo", "taipei", "jakarta", "manila", "bangkok", "singapore",
              "hong kong", "seoul", "shanghai", "beijing", "mumbai", "delhi"]
    city = next((c for c in cities if c in t), None)
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})", t)
    date = f"{m.group(1)} {m.group(2)}" if m else None
    return city, date


def load_resolutions() -> dict[tuple[str, str], int]:
    """Build resolution map: (city, date) → winner_temp."""
    res_map: dict[tuple[str, str], int] = {}
    if not RESOLUTIONS_FILE.exists():
        return res_map
    with RESOLUTIONS_FILE.open() as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
                city, date = parse_city_date(r.get("title", ""))
                wt = parse_temp(r.get("winner_bucket", ""))
                if city and date and wt:
                    res_map[(city, date)] = wt
            except Exception:
                pass
    return res_map


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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not SIGNALS_FILE.exists():
        LOG.info("no signals file yet")
        return 0

    state = load_state()
    last_offset = state.get("last_byte_offset", 0)
    res_map = load_resolutions()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    new_count = 0
    mirror_count = 0
    fade_count = 0
    closed_with_resolution = 0
    cumulative_markout = 0.0

    with SIGNALS_FILE.open() as fh:
        fh.seek(last_offset)
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                s = json.loads(ln)
            except Exception:
                continue

            wallet = s.get("wallet", "")
            short = wallet[:18]

            classification = None
            if short in MIRROR_LIST:
                classification = "SHADOW_MIRROR"
            elif short in FADE_LIST:
                classification = "SHADOW_FADE"
            else:
                continue

            new_count += 1
            title = (s.get("title") or "").lower()
            city, date = parse_city_date(title)
            bucket = parse_temp(title)
            their_side = s.get("side", "")
            their_outcome = s.get("outcome", "")
            their_price = s.get("price", 0.0)

            # What we'd do
            if classification == "SHADOW_MIRROR":
                our_side = their_side
                our_outcome = their_outcome
                our_entry_proxy = their_price
            else:  # FADE = invert outcome on BUY (no SELL handling for v1)
                if their_side != "BUY":
                    continue
                our_side = "BUY"
                our_outcome = "No" if their_outcome == "Yes" else "Yes"
                our_entry_proxy = 1.0 - their_price  # rough symmetric mirror

            # Resolution markout if available
            markout_per_share = None
            is_resolved = False
            if city and date and bucket and (city, date) in res_map:
                winner = res_map[(city, date)]
                is_winner = bucket == winner
                if our_side == "BUY" and our_outcome == "Yes":
                    markout_per_share = (1.0 - our_entry_proxy - 0.005) if is_winner else (-our_entry_proxy - 0.005)
                elif our_side == "BUY" and our_outcome == "No":
                    markout_per_share = (-our_entry_proxy - 0.005) if is_winner else (1.0 - our_entry_proxy - 0.005)
                if markout_per_share is not None:
                    is_resolved = True
                    closed_with_resolution += 1
                    cumulative_markout += markout_per_share

            if classification == "SHADOW_MIRROR":
                mirror_count += 1
            else:
                fade_count += 1

            with OUTPUT.open("a") as out_fh:
                out_fh.write(json.dumps({
                    "ts": int(datetime.now(timezone.utc).timestamp()),
                    "kind": "shadow_score_v1",
                    "classification": classification,
                    "wallet": wallet,
                    "their_side": their_side,
                    "their_outcome": their_outcome,
                    "their_price": their_price,
                    "their_size": s.get("size", 0),
                    "our_side": our_side,
                    "our_outcome": our_outcome,
                    "our_entry_proxy": round(our_entry_proxy, 4),
                    "title": title[:80],
                    "city": city,
                    "date": date,
                    "bucket": bucket,
                    "is_resolved": is_resolved,
                    "markout_per_share": round(markout_per_share, 4) if markout_per_share is not None else None,
                }) + "\n")
        state["last_byte_offset"] = fh.tell()

    save_state(state)

    LOG.info(
        "sm_canary | scanned=%s mirror=%s fade=%s resolved=%s cum_markout=%.4f",
        new_count, mirror_count, fade_count, closed_with_resolution, cumulative_markout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
