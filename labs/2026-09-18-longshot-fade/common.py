"""Shared helpers for the longshot-fade lab: paths, HTTP with retries, parsing."""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "labs" / "longshot"
HIST = DATA / "history"
CLOBM = DATA / "clob_markets"
FIG = Path(__file__).resolve().parent / "figures"
for d in (DATA, HIST, CLOBM, FIG):
    d.mkdir(parents=True, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

MARKETS_JSONL = DATA / "markets.jsonl"
OBS_PARQUET = DATA / "observations.parquet"
RESULTS_JSON = Path(__file__).resolve().parent / "results.json"

HORIZONS_DAYS = (1, 2, 3, 5, 7, 14, 30)


def client() -> httpx.Client:
    return httpx.Client(timeout=30.0, headers={"User-Agent": "predictionlab-labs/0.1"})


def get_json(c: httpx.Client, url: str, params: dict | None = None, retries: int = 6):
    """GET with exponential backoff on 429/5xx/network errors. Returns parsed JSON."""
    delay = 1.0
    for attempt in range(retries):
        try:
            r = c.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            # 4xx other than 429: return the error payload, caller decides
            try:
                return r.json()
            except Exception:
                return {"error": f"http {r.status_code}", "text": r.text[:200]}
        except (httpx.HTTPError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 20)
    return None


def parse_ts(s: str | None) -> int | None:
    """Parse Gamma timestamps: '2026-06-04 00:34:19+00', '2026-07-01T04:00:00Z', ISO with micros."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = s.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def loads_list(s) -> list:
    """Gamma stores lists as JSON strings; tolerate both."""
    if s is None:
        return []
    if isinstance(s, list):
        return s
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def resolved_yes(m: dict) -> int | None:
    """1 if YES (first outcome) won, 0 if lost, None if not cleanly resolved."""
    op = loads_list(m.get("outcomePrices"))
    if len(op) != 2:
        return None
    try:
        a, b = float(op[0]), float(op[1])
    except Exception:
        return None
    if a == 1.0 and b == 0.0:
        return 1
    if a == 0.0 and b == 1.0:
        return 0
    return None


def close_ts(m: dict) -> int | None:
    """Resolution time: closedTime if present, else umaEndDate, else endDate."""
    for k in ("closedTime", "umaEndDate", "endDate"):
        t = parse_ts(m.get(k))
        if t:
            return t
    return None


def category_of(m: dict) -> str:
    q = (m.get("question") or "").lower()
    slug = (m.get("slug") or "").lower()
    text = q + " " + slug
    if m.get("gameStartTime") or m.get("sportsMarketType"):
        return "sports"
    sports_kw = (" vs ", "vs.", "nba", "nfl", "nhl", "mlb", "ucl", "premier league", "la liga",
                 "serie a", "bundesliga", "atp", "wta", "ufc", "f1", "grand prix", "cs2", "esports",
                 "world cup", "open 2026", "playoffs", "finals")
    if any(k in text for k in sports_kw):
        return "sports"
    crypto_kw = ("bitcoin", "btc", "ethereum", "eth ", "solana", "sol ", "crypto", "xrp", "doge",
                 "memecoin", "pump.fun", "hyperliquid", "binance", "coinbase")
    if any(k in text for k in crypto_kw):
        return "crypto"
    pol_kw = ("election", "president", "senate", "governor", "parliament", "nominee", "primary",
              "congress", "minister", "trump", "biden", "harris", "vance", "putin", "zelensky",
              "ceasefire", "tariff", "executive order", "impeach", "supreme court", "fed ", "rate cut",
              "fomc", "cpi", "inflation", "gdp", "shutdown")
    if any(k in text for k in pol_kw):
        return "politics_macro"
    ent_kw = ("oscar", "grammy", "emmy", "box office", "rotten tomatoes", "spotify", "billboard",
              "album", "movie", "netflix", "tweets", "post", "youtube", "tiktok", "gta")
    if any(k in text for k in ent_kw):
        return "culture"
    return "other"


def append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # a line still being written by a concurrent fetch; skip it
                continue
    return rows
