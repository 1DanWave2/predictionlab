"""Lab 3, stage 3: poll the prices of matched Kalshi <-> Polymarket pairs into SQLite.

    python3 collect.py --db /data/xvenue.db [--interval 300] [--rematch-hours 6] [--cycles 0]

Every cycle: Kalshi /markets?tickers=... in chunks of 300 (yes_bid / yes_ask / last / 24h volume /
open interest) and Polymarket CLOB POST /prices in chunks of 150 tokens (BUY = best bid, SELL =
best ask for the outcome token tied to the Kalshi YES). One row per pair per cycle goes into
`ticks`. Every --rematch-hours the snapshot + matcher (fetch.py, match.py) are re-run in this
directory and the `pairs` table is refreshed: new pairs are added, pairs whose Kalshi market is
closed are deactivated. Nothing here places orders or needs keys.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("XVENUE_DATA_DIR") or HERE.parents[1] / "data" / "labs" / "xvenue")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CLOB = "https://clob.polymarket.com"

SCHEMA = """
create table if not exists pairs (pair_id text primary key, kalshi_ticker text, poly_id text, poly_token text, poly_outcome text, grp text,
    method text, score real, kalshi_title text, kalshi_sub text, poly_question text, kalshi_close text, poly_end text, poly_game_start text,
    first_seen integer, last_seen integer, active integer);
create table if not exists ticks (ts integer, pair_id text, k_bid real, k_ask real, k_last real, k_vol24 real, k_oi real, p_bid real, p_ask real,
    primary key (ts, pair_id));
create table if not exists runs (ts integer, kind text, info text);
create index if not exists ticks_pair on ticks (pair_id, ts);
"""


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def fnum(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def clob_px(v, empty: float):
    """CLOB /prices reports 0 for an empty bid side and 1 for an empty ask side: store those as missing."""
    x = fnum(v)
    return None if x is None or x == empty else x


def ts_of(s) -> int | None:
    if s is None or (isinstance(s, float) and s != s) or s == "":
        return None
    s = str(s).replace("Z", "+00:00").replace(" ", "T", 1)
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def rematch(db: sqlite3.Connection) -> None:
    t0 = time.time()
    for script in ("fetch.py", "match.py"):
        r = subprocess.run([sys.executable, str(HERE / script)], capture_output=True, text=True, timeout=1800)
        tail = (r.stderr or "").strip().splitlines()[-3:]
        log(f"{script}: rc={r.returncode} " + " | ".join(tail))
        if r.returncode != 0:
            db.execute("insert into runs values (?,?,?)", (int(time.time()), "rematch_error", f"{script} rc={r.returncode} {(r.stderr or '')[-500:]}"))
            db.commit()
            return
    df = pd.read_parquet(OUT / "pairs.parquet")
    df = df[df["matched"] & df["poly_token"].notna()]
    now = int(time.time())
    for _, r in df.iterrows():
        db.execute("insert into pairs (pair_id, kalshi_ticker, poly_id, poly_token, poly_outcome, grp, method, score, kalshi_title, kalshi_sub, poly_question, "
                   "kalshi_close, poly_end, poly_game_start, first_seen, last_seen, active) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
                   "on conflict(pair_id) do update set last_seen=excluded.last_seen, active=1, score=excluded.score, kalshi_close=excluded.kalshi_close",
                   (r["pair_id"], r["kalshi_ticker"], r["poly_id"], r["poly_token"], r["poly_outcome"], r["group"], r["method"], float(r["score"]),
                    r["kalshi_title"], r["kalshi_sub"], r["poly_question"], r["kalshi_close"], r["poly_end"], r["poly_game_start"], now, now))
    # deactivate pairs whose Kalshi market closed or whose game started more than 8 hours ago
    for pid, close, gs in db.execute("select pair_id, kalshi_close, poly_game_start from pairs where active=1").fetchall():
        c, g = ts_of(close), ts_of(gs)
        if (c and c < now) or (g and g + 8 * 3600 < now):
            db.execute("update pairs set active=0 where pair_id=?", (pid,))
    db.execute("insert into runs values (?,?,?)", (now, "rematch", f"{len(df)} matched pairs, {time.time() - t0:.0f}s"))
    db.commit()
    log(f"rematch: {len(df)} matched pairs loaded in {time.time() - t0:.0f}s; active now {db.execute('select count(*) from pairs where active=1').fetchone()[0]}")


def post_json(c: httpx.Client, url: str, body):
    for attempt in range(3):
        try:
            r = c.post(url, json=body)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504):
                log(f"{url} -> {r.status_code} {r.text[:120]}")
                return None
        except httpx.HTTPError as e:
            log(f"{url} -> {e}")
        time.sleep(1.5 * (attempt + 1))
    return None


def get_json(c: httpx.Client, url: str, params: dict):
    for attempt in range(3):
        try:
            r = c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504):
                log(f"{url} -> {r.status_code} {r.text[:120]}")
                return None
        except httpx.HTTPError as e:
            log(f"{url} -> {e}")
        time.sleep(1.5 * (attempt + 1))
    return None


def cycle(c: httpx.Client, db: sqlite3.Connection) -> tuple[int, int, int]:
    pairs = db.execute("select pair_id, kalshi_ticker, poly_token from pairs where active=1").fetchall()
    if not pairs:
        return 0, 0, 0
    tickers = sorted({p[1] for p in pairs})
    tokens = sorted({p[2] for p in pairs})
    kal: dict[str, dict] = {}
    for i in range(0, len(tickers), 300):
        d = get_json(c, f"{KALSHI}/markets", {"tickers": ",".join(tickers[i:i + 300]), "limit": 1000}) or {}
        for m in d.get("markets", []):
            kal[m["ticker"]] = m
        time.sleep(0.1)
    poly: dict[str, dict] = {}
    for i in range(0, len(tokens), 150):
        body = [{"token_id": t, "side": s} for t in tokens[i:i + 150] for s in ("BUY", "SELL")]
        d = post_json(c, f"{CLOB}/prices", body) or {}
        poly.update(d)
        time.sleep(0.1)
    now = int(time.time())
    n = 0
    for pid, kt, tok in pairs:
        m, pr = kal.get(kt), poly.get(tok)
        if not m and not pr:
            continue
        m = m or {}
        pr = pr or {}
        db.execute("insert or ignore into ticks values (?,?,?,?,?,?,?,?,?)",
                   (now, pid, fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")), fnum(m.get("last_price_dollars")),
                    fnum(m.get("volume_24h_fp")), fnum(m.get("open_interest_fp")), clob_px(pr.get("BUY"), 0.0), clob_px(pr.get("SELL"), 1.0)))
        n += 1
    db.commit()
    return len(pairs), n, len(kal)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(OUT / "xvenue.db"))
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--rematch-hours", type=float, default=6)
    ap.add_argument("--cycles", type=int, default=0, help="0 = run forever")
    a = ap.parse_args()
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)
    last = db.execute("select max(ts) from runs where kind='rematch'").fetchone()[0] or 0
    i = 0
    with httpx.Client(timeout=30, headers={"User-Agent": "predictionlab-xvenue/0.1"}) as c:
        while True:
            t0 = time.time()
            if time.time() - last > a.rematch_hours * 3600 or not db.execute("select 1 from pairs where active=1 limit 1").fetchone():
                rematch(db)
                last = time.time()
            n_pairs, n_rows, n_k = cycle(c, db)
            log(f"cycle {i}: pairs={n_pairs} rows={n_rows} kalshi_ok={n_k} took {time.time() - t0:.0f}s")
            i += 1
            if a.cycles and i >= a.cycles:
                break
            time.sleep(max(0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
