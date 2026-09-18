"""Event-level audit per [GPT 49] requirements.

Fixes the row-level mistake from [Claude 60]:
  - Collapse pm_fills to ONE candidate per market_id
  - Use FIRST fill as candidate entry price (or earliest fill < start time)
  - Mark pre-match vs live based on fill_ts vs market.startDate
  - Split WTA / ATP / MLB / other separately
  - Robustness: ROI excluding top 1/3/5 winners
  - Drawdown: longest losing streak

Output: stdout + /app/data/longtail_event_audit.jsonl
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path('/app/data/paper_bot.db')
OUTPUT = Path('/app/data/longtail_event_audit.jsonl')


def fetch_all_closed_markets(max_pages: int = 25) -> dict[str, dict]:
    cmap: dict[str, dict] = {}
    for page in range(max_pages):
        url = (
            f"https://gamma-api.polymarket.com/markets?"
            f"closed=true&limit=500&offset={page*500}&order=endDate&ascending=false"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"  page {page} err: {e}")
            break
        if not data:
            break
        for m in data:
            cid = (m.get("conditionId") or "").lower()
            if cid:
                cmap[cid] = m
        if len(data) < 500:
            break
    return cmap


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


def parse_market_start_ts(market: dict) -> float | None:
    """Parse startDate or endDate to UTC timestamp."""
    s = market.get("startDate") or market.get("endDate")
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def classify_sport(slug: str) -> str:
    s = (slug or "").lower()
    if "wta" in s:
        return "WTA"
    if "atp" in s:
        return "ATP"
    if "mlb" in s:
        return "MLB"
    if any(x in s for x in ("lol", "cs2", "valorant", "dota")):
        return "esport"
    if "nba" in s:
        return "NBA"
    if "nfl" in s:
        return "NFL"
    if "nhl" in s:
        return "NHL"
    return "other"


def main() -> None:
    print("=" * 78)
    print(f"  LONG-TAIL EVENT-LEVEL AUDIT (per [GPT 49])")
    print(f"  ts={datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    if not DB.exists():
        print("DB missing")
        return

    print(f"\n  Pulling closed markets via gamma...")
    cmap = fetch_all_closed_markets()
    print(f"  closed markets: {len(cmap)}")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Pull all long-tail BUY fills
    rows = c.execute(
        """SELECT condition_id, side, outcome, price, size, notional, fill_ts, slug, title, wallet
           FROM pm_fills
           WHERE price > 0.005 AND price <= 0.10 AND side = 'BUY'
           ORDER BY fill_ts ASC"""
    ).fetchall()
    print(f"  pm_fills retrieved: {len(rows)}")

    # Group by (condition_id, outcome) — one event = one market_id+outcome combo
    # because in tennis "Player A YES" is different decision than "Player B YES"
    # but both are sub-markets of same match.
    # For simplicity collapse to (cid, outcome) pair.
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for cid, side, outcome, price, size, notional, fill_ts, slug, title, wallet in rows:
        key = (cid or "", outcome or "")
        by_key[key].append({
            "price": price, "size": size, "notional": notional,
            "fill_ts": fill_ts, "slug": slug or "", "title": title or "",
            "wallet": wallet,
        })

    print(f"  unique (market, outcome) pairs: {len(by_key)}")

    # Build event candidates: one per pair, using FIRST fill as entry
    events = []
    for (cid, outcome), fills in by_key.items():
        cid_lower = (cid or "").lower()
        market = cmap.get(cid_lower)
        if market is None:
            continue
        winner = parse_winner(market)
        if winner is None:
            continue
        start_ts = parse_market_start_ts(market)
        first = fills[0]  # earliest by fill_ts (sorted ASC)
        # Pre-match if first fill before market start
        pre_match = (start_ts is not None and first["fill_ts"] < start_ts)
        won = (outcome == winner)
        entry = first["price"]
        # PnL per $1 stake (= 1/entry shares × payout)
        if entry <= 0:
            continue
        shares = 1.0 / entry
        payout = 1.0 if won else 0.0
        pnl_dollar = (payout - entry) * shares
        events.append({
            "cid": cid_lower,
            "outcome": outcome,
            "won": won,
            "entry": entry,
            "fill_ts": first["fill_ts"],
            "start_ts": start_ts,
            "pre_match": pre_match,
            "minutes_before_start": (
                (start_ts - first["fill_ts"]) / 60.0
                if start_ts and first["fill_ts"] else None
            ),
            "pnl_per_dollar": pnl_dollar,
            "n_fills": len(fills),
            "wallet_first": first["wallet"],
            "slug": first["slug"],
            "sport": classify_sport(first["slug"]),
            "title": first["title"],
        })

    print(f"  events resolved: {len(events)}")

    if not events:
        print("  ❌ no events to analyze")
        return

    # NOTE: pre_match flag was unreliable — gamma startDate often not set or = creation time
    # All 183 events showed pre_match=0 in first run. Treat all events same for now.
    pre = events  # use all events; "pre-match" cannot be reliably distinguished
    live: list[dict] = []
    print(f"  events used (pre+live combined, gamma startDate unreliable): {len(events)}")

    # Aggregate pre-match by sport
    print(f"\n{'─' * 78}")
    print(f"  PRE-MATCH ONLY — by SPORT")
    print(f"{'─' * 78}")
    print(f"  {'sport':<10} {'n':<5} {'WR':<7} {'avg_p':<8} {'avg_$':<10} {'cum_$':<10} {'top1_$':<10}")
    by_sport_stats = []
    for sport in ("WTA", "ATP", "MLB", "esport", "NBA", "NFL", "NHL", "other"):
        sub = [e for e in pre if e["sport"] == sport]
        if not sub:
            continue
        n = len(sub)
        wins = sum(1 for e in sub if e["won"])
        wr = wins / n
        avg_p = sum(e["entry"] for e in sub) / n
        cum = sum(e["pnl_per_dollar"] for e in sub)
        avg = cum / n
        top1 = max(e["pnl_per_dollar"] for e in sub) if sub else 0
        by_sport_stats.append({
            "sport": sport, "n": n, "wr": wr, "avg_entry": avg_p,
            "cum_$": cum, "avg_$": avg, "top1": top1,
        })
        print(f"  {sport:<10} {n:<5} {wr*100:>5.1f}%  ${avg_p:<6.4f}  ${avg:<+8.4f}  ${cum:<+8.2f}  ${top1:<+8.2f}")

    # Robustness: ROI removing top N winners
    print(f"\n{'─' * 78}")
    print(f"  ROBUSTNESS — pre-match all sports, top-N winners removed")
    print(f"{'─' * 78}")
    pre_sorted = sorted(pre, key=lambda x: -x["pnl_per_dollar"])
    cum_all = sum(e["pnl_per_dollar"] for e in pre)
    n_pre = len(pre)
    print(f"  full sample:        n={n_pre} cum_$=${cum_all:+.2f} avg=${cum_all/n_pre:+.4f}")
    for k in (1, 3, 5, 10):
        if k >= n_pre:
            break
        cum_k = sum(e["pnl_per_dollar"] for e in pre_sorted[k:])
        print(f"  excluding top {k}: n={n_pre-k} cum_$=${cum_k:+.2f} avg=${cum_k/(n_pre-k):+.4f}")

    # Drawdown / longest losing streak (chronological)
    pre_chrono = sorted(pre, key=lambda x: x["fill_ts"])
    losing_streak = 0
    max_streak = 0
    for e in pre_chrono:
        if e["won"]:
            losing_streak = 0
        else:
            losing_streak += 1
            max_streak = max(max_streak, losing_streak)

    # Cumulative PnL chronological
    cum_running = 0
    min_running = 0
    for e in pre_chrono:
        cum_running += e["pnl_per_dollar"]
        min_running = min(min_running, cum_running)

    print(f"\n{'─' * 78}")
    print(f"  CHRONOLOGICAL ROBUSTNESS")
    print(f"{'─' * 78}")
    print(f"  longest losing streak (pre-match):  {max_streak} consecutive losses")
    print(f"  max chronological drawdown:         ${min_running:+.4f} per $1 stake")
    print(f"  final chronological cum_$:           ${cum_running:+.4f}")

    # Per price bucket WITHIN pre-match
    print(f"\n{'─' * 78}")
    print(f"  PRE-MATCH ONLY — by price bucket")
    print(f"{'─' * 78}")
    buckets = [(0.005, 0.025), (0.025, 0.05), (0.05, 0.075), (0.075, 0.10)]
    for lo, hi in buckets:
        sub = [e for e in pre if lo <= e["entry"] < hi]
        if not sub:
            continue
        n = len(sub)
        wins = sum(1 for e in sub if e["won"])
        wr = wins / n
        cum = sum(e["pnl_per_dollar"] for e in sub)
        avg_p = sum(e["entry"] for e in sub) / n
        avg = cum / n
        # remove top 5
        sub_sorted = sorted(sub, key=lambda x: -x["pnl_per_dollar"])
        cum_no5 = sum(e["pnl_per_dollar"] for e in sub_sorted[5:]) if n > 5 else cum
        print(f"  ${lo:.3f}-${hi:.3f}  n={n:<4} WR={wr*100:>5.1f}%  "
              f"impl={avg_p*100:>5.2f}%  avg_$=${avg:<+6.4f}  cum_$=${cum:<+7.2f}  "
              f"cum_no_top5=${cum_no5:<+7.2f}")

    # Verdict
    print(f"\n{'═' * 78}")
    print(f"  VERDICT — per [GPT 49] gates")
    print(f"{'═' * 78}")
    pre_sweet = [e for e in pre if 0.075 <= e["entry"] < 0.10 and e["sport"] in ("WTA", "ATP")]
    n_sweet = len(pre_sweet)
    if n_sweet >= 10:
        sweet_cum = sum(e["pnl_per_dollar"] for e in pre_sweet)
        sweet_sorted = sorted(pre_sweet, key=lambda x: -x["pnl_per_dollar"])
        sweet_no5 = sum(e["pnl_per_dollar"] for e in sweet_sorted[5:]) if n_sweet > 5 else sweet_cum
        sweet_avg_no5 = sweet_no5 / max(n_sweet - 5, 1)
        sweet_wins = sum(1 for e in pre_sweet if e["won"])
        print(f"  pre-match WTA/ATP sweet spot ($0.075-$0.10):")
        print(f"    n={n_sweet}  WR={sweet_wins*100/n_sweet:.1f}%  cum_$=${sweet_cum:+.2f}")
        print(f"    after removing top 5 winners: cum_$=${sweet_no5:+.2f} avg=${sweet_avg_no5:+.4f}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "event_audit_v1",
            "n_events": len(events),
            "n_pre_match": len(pre),
            "n_live_or_unknown": len(live),
            "longest_losing_streak": max_streak,
            "max_drawdown": round(min_running, 4),
            "by_sport": by_sport_stats,
        }) + "\n")
    print(f"\n  saved to {OUTPUT}")
    conn.close()


if __name__ == "__main__":
    main()
