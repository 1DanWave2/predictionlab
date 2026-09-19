"""Shadow maker: the Lab 2 hypothesis, run against live books without placing orders.

Hypothesis: a two-sided quote at the touch, skewed to the bid (bigger bid, smaller ask), on
reward markets with above-median pools, earns the liquidity reward with less exposure to the
informed buy-side flow.

Every cycle (default 60 s) for each selected market:
  - pull the full CLOB book for the YES token; compute the exact reward score of every
    resting order inside the corridor (v = rewardsMaxSpread) and our own score for a virtual
    bid of BID_SIZE at the best bid and a virtual ask of ASK_SIZE at the best ask
    (Polymarket 2026 rules: score = ((v - s)/v)^2 * size, Q = max(min(Q1,Q2), max(Q1,Q2)/3)
    inside 10-90c, else min). Accrue expected reward = pool/1440 * share for this minute.
  - pull taker trades since the last cycle; a trade at our bid price (taker sells YES) or at
    our ask price (taker buys YES) is a virtual fill: pro-rata share S/(depth+S) of its size,
    capped at S, and the last-in-queue variant max(size - depth, 0) capped at S.
  - log quotes and fills into SQLite. report.py computes markouts from the logged mids.

Market selection (refreshed every SELECT_EVERY cycles): active Gamma markets with a reward
pool, rewardsMinSize <= BID_SIZE, mid in [0.20, 0.80], endDate at least 2 days out; keep the
ones whose pool is at or above the median pool of the candidates, at most MAX_MARKETS.

    python3 shadow_maker.py --db data/labs/maker/shadow.db [--interval 60] [--cycles 0]
Nothing here places orders. It never touches keys.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BID_SIZE, ASK_SIZE = 1000.0, 500.0
MAX_MARKETS = 40
SELECT_EVERY = 10
MID_LO, MID_HI = 0.20, 0.80

SCHEMA = """
create table if not exists markets (condition_id text primary key, market_id text, question text, slug text,
    yes_token text, no_token text, v_cents real, min_size real, pool real, end_ts integer, first_seen integer, last_seen integer);
create table if not exists quotes (ts integer, condition_id text, mid real, bid real, ask real, depth_bid real, depth_ask real,
    q_book real, q_us real, share real, pool real, accrual real, corridor_shares real, primary key (ts, condition_id));
create table if not exists fills (trade_ts integer, condition_id text, side text, price real, size real, our_pro real, our_last real,
    mid_at real, depth real, tx text, primary key (trade_ts, condition_id, side, price, size));
create table if not exists state (key text primary key, value text);
"""


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def get(c: httpx.Client, url: str, params: dict | None = None):
    for attempt in range(4):
        try:
            r = c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        except httpx.HTTPError:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def parse_ts(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00").replace(" ", "T", 1)
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def select_markets(c: httpx.Client) -> list[dict]:
    rows = []
    for offset in range(0, 2000, 100):   # Gamma caps limit at 100 and offset at ~2000
        page = get(c, f"{GAMMA}/markets", {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false",
                                            "limit": 100, "offset": offset}) or []
        rows.extend(page)
        if len(page) < 100:
            break
        time.sleep(0.05)
    now = int(time.time())
    cands = []
    for m in rows:
        v = float(m.get("rewardsMaxSpread") or 0)
        pool = sum(float(r.get("rewardsDailyRate") or 0) for r in (m.get("clobRewards") or []))
        min_size = float(m.get("rewardsMinSize") or 0)
        if v <= 0 or pool <= 0 or min_size > BID_SIZE:
            continue
        bb, ba = m.get("bestBid"), m.get("bestAsk")
        if bb is None or ba is None:
            continue
        mid = (float(bb) + float(ba)) / 2
        if not (MID_LO <= mid <= MID_HI):
            continue
        end = parse_ts(m.get("endDate"))
        if end and end < now + 2 * 86400:
            continue
        toks = m.get("clobTokenIds")
        toks = json.loads(toks) if isinstance(toks, str) else toks
        if not toks or len(toks) < 2:
            continue
        cands.append({"condition_id": m["conditionId"], "market_id": str(m["id"]), "question": m.get("question"), "slug": m.get("slug"),
                      "yes_token": toks[0], "no_token": toks[1], "v": v, "min_size": min_size, "pool": pool, "end_ts": end})
    if not cands:
        return []
    pools = sorted(x["pool"] for x in cands)
    median = pools[len(pools) // 2]
    chosen = [x for x in cands if x["pool"] >= median]
    chosen.sort(key=lambda x: -x["pool"])
    return chosen[:MAX_MARKETS]


def corridor_score(levels: list[tuple[float, float]], mid: float, v: float) -> float:
    q = 0.0
    for p, sz in levels:
        s = abs(p - mid) * 100
        if s <= v:
            q += ((v - s) / v) ** 2 * sz
    return q


def two_sided(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / 3.0)
    return min(q1, q2)


def cycle(c: httpx.Client, db: sqlite3.Connection, markets: list[dict], last_trade_ts: dict[str, int]) -> tuple[int, int, float]:
    now = int(time.time())
    n_q = n_f = 0
    accrual_total = 0.0
    for m in markets:
        book = get(c, f"{CLOB}/book", {"token_id": m["yes_token"]})
        if not book:
            continue
        bids = [(float(x["price"]), float(x["size"])) for x in book.get("bids", [])]
        asks = [(float(x["price"]), float(x["size"])) for x in book.get("asks", [])]
        if not bids or not asks:
            continue
        bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
        if ba <= bb:
            continue
        mid, v = (bb + ba) / 2, m["v"]
        depth_bid = sum(sz for p, sz in bids if abs(p - bb) < 1e-9)
        depth_ask = sum(sz for p, sz in asks if abs(p - ba) < 1e-9)
        q_book = two_sided(corridor_score(bids, mid, v), corridor_score(asks, mid, v), mid)
        s = (ba - bb) / 2 * 100
        w = ((v - s) / v) ** 2 if s <= v else 0.0
        q_us = two_sided(w * BID_SIZE, w * ASK_SIZE, mid) if w > 0 else 0.0
        share = q_us / (q_us + q_book) if q_us + q_book > 0 else 0.0
        accrual = m["pool"] / 1440.0 * share
        corridor_shares = sum(sz for p, sz in bids + asks if abs(p - mid) * 100 <= v)
        db.execute("insert or replace into quotes values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now, m["condition_id"], mid, bb, ba, depth_bid, depth_ask, q_book, q_us, share, m["pool"], accrual, corridor_shares))
        db.execute("insert into markets (condition_id, market_id, question, slug, yes_token, no_token, v_cents, min_size, pool, end_ts, first_seen, last_seen) "
                   "values (?,?,?,?,?,?,?,?,?,?,?,?) on conflict(condition_id) do update set pool=excluded.pool, last_seen=excluded.last_seen",
                   (m["condition_id"], m["market_id"], m["question"], m["slug"], m["yes_token"], m["no_token"], v, m["min_size"], m["pool"], m["end_ts"], now, now))
        n_q += 1
        accrual_total += accrual
        # taker trades since the last cycle
        trades = get(c, f"{DATA_API}/trades", {"market": m["condition_id"], "limit": 200, "takerOnly": "true"}) or []
        since = last_trade_ts.get(m["condition_id"], now - 120)
        newest = since
        for t in trades:
            ts_t = int(t.get("timestamp") or 0)
            if ts_t <= since:
                continue
            newest = max(newest, ts_t)
            is_yes = str(t.get("asset")) == str(m["yes_token"])
            price = float(t["price"]); size = float(t["size"])
            p_yes = price if is_yes else 1 - price
            taker_buys_yes = (t.get("side") == "BUY") if is_yes else (t.get("side") == "SELL")
            if (not taker_buys_yes) and p_yes <= bb + 1e-9:          # taker sold into our bid
                our_pro = min(size * BID_SIZE / (depth_bid + BID_SIZE), BID_SIZE)
                our_last = min(max(size - depth_bid, 0.0), BID_SIZE)
                side, depth = "buy_yes", depth_bid
            elif taker_buys_yes and p_yes >= ba - 1e-9:              # taker bought our ask
                our_pro = min(size * ASK_SIZE / (depth_ask + ASK_SIZE), ASK_SIZE)
                our_last = min(max(size - depth_ask, 0.0), ASK_SIZE)
                side, depth = "sell_yes", depth_ask
            else:
                continue
            db.execute("insert or ignore into fills values (?,?,?,?,?,?,?,?,?,?)",
                       (ts_t, m["condition_id"], side, p_yes, size, our_pro, our_last, mid, depth, t.get("transactionHash") or ""))
            n_f += 1
        last_trade_ts[m["condition_id"]] = newest
        time.sleep(0.08)
    db.commit()
    return n_q, n_f, accrual_total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/labs/maker/shadow.db")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    a = ap.parse_args()
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)
    last_trade_ts: dict[str, int] = {}
    markets: list[dict] = []
    i = 0
    with httpx.Client(timeout=30, headers={"User-Agent": "predictionlab-shadow/0.1"}) as c:
        while True:
            if i % SELECT_EVERY == 0 or not markets:
                markets = select_markets(c)
                log(f"selected {len(markets)} markets, pools {min(m['pool'] for m in markets) if markets else 0:.0f}..{max(m['pool'] for m in markets) if markets else 0:.0f} $/day")
            t0 = time.time()
            n_q, n_f, acc = cycle(c, db, markets, last_trade_ts)
            log(f"cycle {i}: quotes={n_q} fills={n_f} accrual=${acc:.3f}/min (~${acc*1440:.0f}/day) took {time.time()-t0:.0f}s")
            i += 1
            if a.cycles and i >= a.cycles:
                break
            time.sleep(max(0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
