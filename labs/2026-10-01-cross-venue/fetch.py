"""Lab 3, stage 1: snapshot the open markets of Kalshi and Polymarket into parquet.

    python3 fetch.py            # writes <data>/kalshi.parquet and <data>/polymarket.parquet
    <data> = $XVENUE_DATA_DIR or data/labs/xvenue

Kalshi: /trade-api/v2/markets?status=open&min_close_ts=now&mve_filter=exclude, paginated by cursor
(without mve_filter the listing is 200k+ parlay legs and the game markets never come; the
`liquidity_dollars` field is always zero on this endpoint, so open_interest_fp and volume_24h_fp
are the activity signals). The daily game series are listed explicitly as well, and event titles /
sub-titles / categories come from /events.
Polymarket: Gamma active markets by 24h volume (offset cap ~2000) plus the sports moneyline
listing, with outcomes and CLOB token ids kept (JSON strings) so a Kalshi team market can be tied
to one outcome of a Polymarket moneyline market.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent   # repo root when run from labs/<lab>/, harmless elsewhere
OUT = Path(os.environ.get("XVENUE_DATA_DIR") or ROOT / "data" / "labs" / "xvenue")
OUT.mkdir(parents=True, exist_ok=True)
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
GAME_SERIES = ["KXNFLGAME", "KXMLBGAME", "KXNBAGAME", "KXNHLGAME", "KXWNBAGAME", "KXNCAAFGAME", "KXEPLGAME", "KXLALIGAGAME",
               "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLIGUE1GAME", "KXMLSGAME", "KXUCLGAME"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def fnum(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def get(c: httpx.Client, url: str, params: dict | None = None):
    for attempt in range(4):
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


def kalshi_row(m: dict) -> dict:
    return {"ticker": m["ticker"], "event_ticker": m.get("event_ticker"), "series": m["ticker"].split("-")[0], "title": m.get("title"),
            "yes_sub_title": m.get("yes_sub_title"), "rules": (m.get("rules_primary") or "")[:400],
            "yes_bid": fnum(m.get("yes_bid_dollars")), "yes_ask": fnum(m.get("yes_ask_dollars")), "last": fnum(m.get("last_price_dollars")),
            "volume_24h": fnum(m.get("volume_24h_fp")), "open_interest": fnum(m.get("open_interest_fp")), "close_time": m.get("close_time"),
            "expiration_time": m.get("expiration_time"), "strike_type": m.get("strike_type")}


def fetch_kalshi(c: httpx.Client) -> pd.DataFrame:
    now = int(time.time())
    rows: dict[str, dict] = {}
    cursor = None
    for page in range(2000):
        p = {"limit": 1000, "status": "open", "min_close_ts": now, "mve_filter": "exclude"}
        if cursor:
            p["cursor"] = cursor
        d = get(c, f"{KALSHI}/markets", p) or {}
        ms = d.get("markets", [])
        for m in ms:
            if m.get("mve_collection_ticker") or m.get("market_type") != "binary":
                continue
            rows[m["ticker"]] = kalshi_row(m)
        cursor = d.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(0.05)
    n_general = len(rows)
    for s in GAME_SERIES:
        cursor = None
        for page in range(20):
            p = {"limit": 1000, "status": "open", "series_ticker": s}
            if cursor:
                p["cursor"] = cursor
            d = get(c, f"{KALSHI}/markets", p) or {}
            ms = d.get("markets", [])
            for m in ms:
                if m.get("market_type") == "binary":
                    rows[m["ticker"]] = kalshi_row(m)
            cursor = d.get("cursor")
            if not cursor or not ms:
                break
    df = pd.DataFrame(list(rows.values()))
    # event titles / sub-titles / categories
    ev: dict[str, tuple] = {}
    cursor = None
    for page in range(500):
        p = {"limit": 200, "status": "open", "with_nested_markets": "false"}
        if cursor:
            p["cursor"] = cursor
        d = get(c, f"{KALSHI}/events", p) or {}
        es = d.get("events", [])
        for e in es:
            ev[e["event_ticker"]] = (e.get("title"), e.get("category"), e.get("sub_title"))
        cursor = d.get("cursor")
        if not cursor or not es:
            break
        time.sleep(0.05)
    missing = sorted(set(df["event_ticker"].dropna()) - set(ev))
    for et in missing[:400]:
        e = (get(c, f"{KALSHI}/events/{et}", {"with_nested_markets": "false"}) or {}).get("event") or {}
        if e:
            ev[et] = (e.get("title"), e.get("category"), e.get("sub_title"))
    df["event_title"] = df["event_ticker"].map(lambda t: (ev.get(t) or (None, None, None))[0])
    df["category"] = df["event_ticker"].map(lambda t: (ev.get(t) or (None, None, None))[1])
    df["event_sub_title"] = df["event_ticker"].map(lambda t: (ev.get(t) or (None, None, None))[2])
    df["fetched_at"] = now
    df.to_parquet(OUT / "kalshi.parquet", index=False)
    log(f"kalshi: {len(df)} binary non-parlay open markets ({n_general} from the general listing, {len(df) - n_general} extra from game series), "
        f"{df['event_ticker'].nunique()} events ({len(missing)} looked up one by one); open_interest>=1000: {(df['open_interest'].fillna(0) >= 1000).sum()}")
    return df


def poly_row(m: dict) -> dict:
    toks = m.get("clobTokenIds")
    toks = json.loads(toks) if isinstance(toks, str) else (toks or [])
    outs = m.get("outcomes")
    outs = json.loads(outs) if isinstance(outs, str) else (outs or [])
    ev = (m.get("events") or [{}])[0]
    return {"market_id": str(m["id"]), "condition_id": m.get("conditionId"), "question": m.get("question"), "event_title": ev.get("title"),
            "event_slug": ev.get("slug"), "slug": m.get("slug"), "description": (m.get("description") or "")[:400],
            "best_bid": fnum(m.get("bestBid")), "best_ask": fnum(m.get("bestAsk")), "last": fnum(m.get("lastTradePrice")),
            "volume_24h": fnum(m.get("volume24hr")), "liquidity": fnum(m.get("liquidityNum") or m.get("liquidity")),
            "end_date": m.get("endDate"), "game_start": m.get("gameStartTime"), "neg_risk": bool(m.get("negRisk")),
            "sports_market_type": m.get("sportsMarketType"), "line": fnum(m.get("line")),
            "outcomes": json.dumps(outs), "tokens": json.dumps(toks), "yes_token": (toks[0] if toks else None)}


def fetch_poly(c: httpx.Client) -> pd.DataFrame:
    rows: dict[str, dict] = {}
    base = {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100}
    passes = [("volume", {}), ("moneyline", {"sports_market_types": "moneyline"})]
    for name, extra in passes:
        n0 = len(rows)
        for offset in range(0, 2100, 100):
            page = get(c, f"{GAMMA}/markets", {**base, **extra, "offset": offset})
            if not isinstance(page, list) or not page:
                break
            for m in page:
                rows[str(m["id"])] = poly_row(m)
            if len(page) < 100:
                break
            time.sleep(0.05)
        log(f"polymarket pass '{name}': +{len(rows) - n0} markets")
    df = pd.DataFrame(list(rows.values()))
    df["fetched_at"] = int(time.time())
    df.to_parquet(OUT / "polymarket.parquet", index=False)
    log(f"polymarket: {len(df)} active markets; volume24h>=$10k: {(df['volume_24h'].fillna(0) >= 10000).sum()}; "
        f"moneyline: {(df['sports_market_type'] == 'moneyline').sum()}")
    return df


def main() -> None:
    with httpx.Client(timeout=30, headers={"User-Agent": "predictionlab-labs/0.1"}) as c:
        fetch_kalshi(c)
        fetch_poly(c)


if __name__ == "__main__":
    main()
