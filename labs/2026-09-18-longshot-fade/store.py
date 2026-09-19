"""Parquet store for the lab: markets, price histories, history-fetch status.

Three files under data/labs/longshot/:
  markets.parquet         one row per market (selected Gamma fields, newest fetch wins)
  histories.parquet       one row per (market_id, t, p) daily point of the YES token
  history_status.parquet  one row per market_id: ok | no_history | error, attempts, fetched_at

Everything else (observations, results, figures) is derived from these three.
"""
from __future__ import annotations

import json
import time

import pandas as pd

from common import DATA

MARKETS_PQ = DATA / "markets.parquet"
HIST_PQ = DATA / "histories.parquet"
HSTAT_PQ = DATA / "history_status.parquet"

MARKET_FIELDS = [
    "id", "question", "conditionId", "slug", "endDate", "closedTime", "umaEndDate", "startDate",
    "createdAt", "acceptingOrdersTimestamp", "gameStartTime", "sportsMarketType", "volumeNum",
    "negRisk", "outcomes", "outcomePrices", "clobTokenIds", "umaResolutionStatus", "closed", "active",
]
STR_FIELDS = [f for f in MARKET_FIELDS if f not in ("volumeNum", "negRisk", "closed", "active")]


def market_row(m: dict, fetched_at: int | None = None) -> dict:
    row = {}
    for k in MARKET_FIELDS:
        v = m.get(k)
        if isinstance(v, (list, dict)):
            v = json.dumps(v)
        row[k] = v
    row["id"] = str(row["id"])
    row["volumeNum"] = float(row["volumeNum"] or 0.0)
    row["negRisk"] = bool(row["negRisk"])
    row["closed"] = bool(row["closed"])
    row["active"] = bool(row["active"]) if row["active"] is not None else False
    for k in STR_FIELDS:
        if row[k] is not None and not isinstance(row[k], str):
            row[k] = str(row[k])
    row["fetched_at"] = int(fetched_at or time.time())
    return row


def _empty_markets() -> pd.DataFrame:
    cols = {k: "string" for k in STR_FIELDS}
    cols.update({"volumeNum": "float64", "negRisk": "bool", "closed": "bool", "active": "bool", "fetched_at": "int64"})
    return pd.DataFrame({k: pd.Series(dtype=t) for k, t in cols.items()})


def load_markets() -> pd.DataFrame:
    if MARKETS_PQ.exists():
        return pd.read_parquet(MARKETS_PQ)
    return _empty_markets()


def upsert_markets(rows: list[dict]) -> tuple[int, int]:
    """Insert or replace by id (newest fetched_at wins). Returns (new, updated)."""
    if not rows:
        return 0, 0
    old = load_markets()
    new = pd.DataFrame(rows)
    for k in STR_FIELDS:
        new[k] = new[k].astype("string")
    known = set(old["id"]) if len(old) else set()
    n_new = int((~new["id"].isin(known)).sum())
    n_upd = int(len(new) - n_new)
    df = pd.concat([old, new], ignore_index=True)
    df = df.sort_values("fetched_at").drop_duplicates("id", keep="last").reset_index(drop=True)
    df.to_parquet(MARKETS_PQ, index=False)
    return n_new, n_upd


def markets_records(df: pd.DataFrame | None = None) -> list[dict]:
    """Rows as plain dicts with None for missing values (what common.* helpers expect)."""
    df = load_markets() if df is None else df
    cols = list(df.columns)
    arrays = [df[c].to_numpy(dtype=object) for c in cols]
    recs = []
    for row in zip(*arrays):
        recs.append({c: (None if (v is None or v is pd.NA or (isinstance(v, float) and v != v)) else v)
                     for c, v in zip(cols, row)})
    return recs


def load_histories() -> pd.DataFrame:
    if HIST_PQ.exists():
        return pd.read_parquet(HIST_PQ)
    return pd.DataFrame({"market_id": pd.Series(dtype="string"), "t": pd.Series(dtype="int64"), "p": pd.Series(dtype="float64")})


def append_histories(rows: list[tuple[str, int, float]]) -> int:
    if not rows:
        return 0
    old = load_histories()
    new = pd.DataFrame(rows, columns=["market_id", "t", "p"])
    new["market_id"] = new["market_id"].astype("string")
    df = pd.concat([old, new], ignore_index=True).drop_duplicates(["market_id", "t"], keep="last")
    df.to_parquet(HIST_PQ, index=False)
    return int(len(new))


def load_status() -> pd.DataFrame:
    if HSTAT_PQ.exists():
        return pd.read_parquet(HSTAT_PQ)
    return pd.DataFrame({"market_id": pd.Series(dtype="string"), "status": pd.Series(dtype="string"),
                         "attempts": pd.Series(dtype="int64"), "fetched_at": pd.Series(dtype="int64")})


def update_status(entries: list[tuple[str, str]]) -> None:
    """entries: (market_id, status). Attempts accumulate across runs."""
    if not entries:
        return
    old = load_status()
    prev = old.set_index("market_id")["attempts"].to_dict() if len(old) else {}
    now = int(time.time())
    new = pd.DataFrame([{"market_id": m, "status": s, "attempts": int(prev.get(m, 0)) + 1, "fetched_at": now}
                        for m, s in entries])
    new["market_id"] = new["market_id"].astype("string")
    new["status"] = new["status"].astype("string")
    df = pd.concat([old, new], ignore_index=True).drop_duplicates("market_id", keep="last")
    df.to_parquet(HSTAT_PQ, index=False)


def histories_by_market(df: pd.DataFrame | None = None) -> dict[str, list[tuple[int, float]]]:
    """{market_id: [(t, p), ...]} sorted by t. Pure numpy split: seconds, not minutes."""
    import numpy as np

    df = load_histories() if df is None else df
    if not len(df):
        return {}
    df = df.sort_values(["market_id", "t"], kind="stable")
    ids = df["market_id"].astype(str).to_numpy()
    ts = df["t"].to_numpy().tolist()
    ps = df["p"].to_numpy().tolist()
    starts = np.flatnonzero(np.r_[True, ids[1:] != ids[:-1]])
    ends = np.r_[starts[1:], len(ids)]
    return {ids[a]: list(zip(ts[a:b], ps[a:b])) for a, b in zip(starts.tolist(), ends.tolist())}
