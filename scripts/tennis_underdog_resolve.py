"""Tennis underdog shadow resolver — companion to tennis_underdog_shadow.py.

Reads shadow candidates, checks if market resolved (gamma closed=true),
matches our outcome to actual winner, computes hypothetical $1-stake PnL.

Output: appends `resolution` field to each shadow record (in-place rewrite)
        + summary line to /app/data/tennis_underdog_resolved.jsonl

Cron: */1h (resolutions take time; no need to poll faster)

Idempotency: skip records that already have `resolution_ts`.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
SHADOW_LOG = Path('/app/data/tennis_underdog_shadow.jsonl')
RESOLVED_LOG = Path('/app/data/tennis_underdog_resolved.jsonl')


def fetch_market(market_id: str) -> dict | None:
    """Pull single market by id from gamma."""
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def parse_winner(market: dict) -> str | None:
    if not market.get("closed"):
        return None
    try:
        prices_raw = market.get("outcomePrices", "")
        outcomes_raw = market.get("outcomes", "")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        if not prices or not outcomes:
            return None
        for i, p in enumerate(prices):
            if abs(float(p) - 1.0) < 0.01 and i < len(outcomes):
                return str(outcomes[i])
    except Exception:
        return None
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if not SHADOW_LOG.exists():
        LOG.info("no shadow log yet")
        return 0

    candidates = []
    with SHADOW_LOG.open() as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            candidates.append(rec)

    LOG.info("candidates loaded: %s", len(candidates))

    pending = [r for r in candidates if not r.get("resolution_ts")]
    LOG.info("pending resolution: %s", len(pending))

    resolved_now = 0
    for rec in pending:
        market = fetch_market(rec.get("market_id", ""))
        if market is None:
            continue
        winner = parse_winner(market)
        if winner is None:
            continue
        # Match
        won = (rec.get("outcome") == winner)
        ask = float(rec.get("ask", 0))
        if ask <= 0:
            continue
        # PnL per $1 stake
        shares = 1.0 / ask
        payout = 1.0 if won else 0.0
        pnl_dollar = (payout - ask) * shares
        rec["resolution_ts"] = int(time.time())
        rec["winner"] = winner
        rec["won"] = won
        rec["pnl_per_dollar"] = round(pnl_dollar, 4)
        # Append to resolved log (separate from main shadow log to keep simple)
        RESOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RESOLVED_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        resolved_now += 1
        LOG.info(
            "RESOLVED | %s/%s won=%s ask=$%.4f pnl_$1=$%+.4f",
            rec.get("sport"), rec.get("outcome"), won, ask, pnl_dollar,
        )

    # Quick summary
    if RESOLVED_LOG.exists():
        all_resolved = []
        with RESOLVED_LOG.open() as fh:
            for ln in fh:
                try:
                    all_resolved.append(json.loads(ln))
                except Exception:
                    pass
        n = len(all_resolved)
        if n:
            wins = sum(1 for r in all_resolved if r.get("won"))
            cum = sum(r.get("pnl_per_dollar", 0) for r in all_resolved)
            LOG.info(
                "summary | resolved_total=%s wins=%s WR=%.1f%% cum_$=%+.2f",
                n, wins, wins * 100 / n, cum,
            )

    LOG.info("done | resolved_this_run=%s", resolved_now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
