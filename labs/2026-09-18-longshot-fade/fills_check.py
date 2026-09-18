"""Stage 5: replicate the longshot question on our own fill stream (pm_fills, May 2026).

Rows are fills below 20 cents. Resolution comes from the CLOB market object (tokens[].winner),
cached under data/labs/longshot/clob_markets/. Reports row-level (fill-weighted) versus
market-level results and how concentrated the PnL is.

Usage: python3 fills_check.py [--db data/paper_bot_prod_2026-05-29.db]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from common import CLOB, CLOBM, ROOT, client, get_json

RNG = np.random.default_rng(7)


def clob_market(cid: str) -> dict | None:
    path = CLOBM / f"{cid}.json"
    if path.exists():
        return json.loads(path.read_text())
    with client() as c:
        d = get_json(c, f"{CLOB}/markets/{cid}")
    if isinstance(d, dict) and "tokens" in d:
        path.write_text(json.dumps(d))
        return d
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "paper_bot_prod_2026-05-29.db"))
    ap.add_argument("--max-price", type=float, default=0.20)
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    f = pd.read_sql_query(
        "select fill_ts, wallet, side, condition_id, outcome, price, size, notional, title "
        "from pm_fills where price < ? and price > 0", con, params=(a.max_price,))
    print(f"fills below {a.max_price}: {len(f)} on {f['condition_id'].nunique()} markets", file=sys.stderr)

    cids = sorted(f["condition_id"].unique())
    with ThreadPoolExecutor(max_workers=8) as ex:
        markets = dict(zip(cids, ex.map(clob_market, cids)))
    win_map = {}
    closed = 0
    for cid, m in markets.items():
        if not m or not m.get("closed"):
            continue
        closed += 1
        for t in m.get("tokens", []):
            if t.get("winner") is not None:
                win_map[(cid, t["outcome"])] = int(bool(t["winner"]))
    f["win"] = [win_map.get((c, o)) for c, o in zip(f["condition_id"], f["outcome"])]
    f = f.dropna(subset=["win"]).copy()
    f["win"] = f["win"].astype(int)
    print(f"resolved markets: {closed}/{len(cids)}, fills with outcome: {len(f)}", file=sys.stderr)

    res: dict = {"fills": int(len(f)), "markets": int(f["condition_id"].nunique()),
                 "window": [str(pd.to_datetime(f["fill_ts"].min(), unit="s").date()),
                            str(pd.to_datetime(f["fill_ts"].max(), unit="s").date())]}
    bins = [0, 0.02, 0.05, 0.10, 0.15, 0.20]
    f["bucket"] = pd.cut(f["price"], bins, right=False).astype(str)

    # Row level: each fill is one observation (this is what a naive "signals" study does)
    row = f.groupby("bucket").agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean"),
                                  notional=("notional", "sum")).reset_index()
    row["ratio"] = row["realized"] / row["implied"]
    res["row_level"] = row.round(4).to_dict(orient="records")
    row_all = {"n": int(len(f)), "implied": float(f["price"].mean()), "realized": float(f["win"].mean())}
    res["row_level_all"] = row_all

    # Notional-weighted realized rate (what the money experienced)
    w = f["notional"] / f["notional"].sum()
    res["notional_weighted"] = {"implied": float((f["price"] * w).sum()), "realized": float((f["win"] * w).sum())}

    # Market level: one observation per (market, outcome) at its volume-weighted average price
    g = f.groupby(["condition_id", "outcome"]).apply(
        lambda d: pd.Series({"avg_price": (d["price"] * d["size"]).sum() / d["size"].sum(),
                             "win": int(d["win"].iloc[0]), "fills": len(d), "notional": d["notional"].sum()}),
        include_groups=False).reset_index()
    g["pnl_buy_1usd"] = g["win"] / g["avg_price"] - 1  # buy $1 of the longshot, hold to resolution
    mk = {"n": int(len(g)), "implied": float(g["avg_price"].mean()), "realized": float(g["win"].mean()),
          "pnl_total": float(g["pnl_buy_1usd"].sum()), "pnl_mean": float(g["pnl_buy_1usd"].mean())}
    top = g.sort_values("pnl_buy_1usd", ascending=False)
    mk["pnl_ex_top10"] = float(top["pnl_buy_1usd"].iloc[10:].sum())
    mk["top10_share_of_gains"] = float(top["pnl_buy_1usd"].head(10).sum() / max(top.loc[top["pnl_buy_1usd"] > 0, "pnl_buy_1usd"].sum(), 1e-9))
    idx = RNG.integers(0, len(g), size=(2000, len(g)))
    boots = g["pnl_buy_1usd"].to_numpy()[idx].mean(axis=1)
    mk["pnl_mean_ci"] = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    res["market_level"] = mk

    # First-touch design: one observation per (market, outcome) at the FIRST fill below the
    # threshold inside the window. Removes the "winners spend less time cheap" weighting.
    ft = f.sort_values("fill_ts").groupby(["condition_id", "outcome"]).first().reset_index()
    ft["pnl_buy_1usd"] = ft["win"] / ft["price"] - 1
    idx2 = RNG.integers(0, len(ft), size=(2000, len(ft)))
    b2 = ft["pnl_buy_1usd"].to_numpy()[idx2].mean(axis=1)
    ft["bucket"] = pd.cut(ft["price"], bins, right=False).astype(str)
    ftb = ft.groupby("bucket").agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean")).reset_index()
    ftb["ratio"] = ftb["realized"] / ftb["implied"]
    res["first_touch"] = {"n": int(len(ft)), "implied": float(ft["price"].mean()), "realized": float(ft["win"].mean()),
                          "pnl_mean": float(ft["pnl_buy_1usd"].mean()),
                          "pnl_mean_ci": [float(np.percentile(b2, 2.5)), float(np.percentile(b2, 97.5))],
                          "by_bucket": ftb.round(4).to_dict(orient="records")}
    ft["is_sports"] = ft["title"].str.contains(r"\bvs\.?\b", case=False, regex=True)
    res["first_touch_by_sports"] = ft.groupby("is_sports").agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean")).reset_index().round(4).to_dict(orient="records")

    # Who is on which side: BUY = taker bought the longshot, SELL = taker sold it
    side = f.groupby("side").agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean"), notional=("notional", "sum")).reset_index()
    res["by_taker_side"] = side.round(4).to_dict(orient="records")

    # Category split by title heuristics (sports if ' vs' in title)
    f["is_sports"] = f["title"].str.contains(r"\bvs\.?\b", case=False, regex=True)
    cat = f.groupby("is_sports").agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean")).reset_index()
    res["by_sports_flag"] = cat.round(4).to_dict(orient="records")

    out = ROOT / "labs" / "2026-09-18-longshot-fade" / f"results_fills_{int(a.max_price*100)}c.json"
    out.write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float), file=sys.stderr)


if __name__ == "__main__":
    main()
