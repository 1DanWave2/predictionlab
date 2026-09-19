"""Summarise a shadow_maker.py run: reward accrual, fills, markouts, net.

    python3 report.py --db data/labs/maker/shadow.db [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import numpy as np
import pandas as pd

HORIZONS = {"5m": 300, "15m": 900, "60m": 3600}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/labs/maker/shadow.db")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    q = pd.read_sql_query("select * from quotes", con)
    f = pd.read_sql_query("select * from fills", con)
    m = pd.read_sql_query("select * from markets", con)
    if q.empty:
        print("no quotes yet")
        return
    q["day"] = pd.to_datetime(q["ts"], unit="s").dt.date.astype(str)
    hours = (q["ts"].max() - q["ts"].min()) / 3600
    out = {"quotes": int(len(q)), "markets": int(q["condition_id"].nunique()), "hours": round(hours, 1),
           "accrued_reward_usd": float(q["accrual"].sum()), "reward_usd_per_day": float(q["accrual"].sum() / max(hours / 24, 1e-9)),
           "median_share_pct": float(q["share"].median() * 100), "median_pool": float(q["pool"].median()),
           "median_corridor_shares": float(q["corridor_shares"].median())}
    # markouts from logged mids
    if not f.empty:
        q_sorted = q.sort_values(["condition_id", "ts"])
        rows = []
        for cid, g in f.groupby("condition_id"):
            qs = q_sorted[q_sorted["condition_id"] == cid]
            ts, mids = qs["ts"].to_numpy(), qs["mid"].to_numpy()
            for _, r in g.iterrows():
                d = {"side": r["side"], "price": r["price"], "our_pro": r["our_pro"], "our_last": r["our_last"], "mid_at": r["mid_at"]}
                sign = -1.0 if r["side"] == "buy_yes" else 1.0  # maker bought YES: loses if mid falls
                for name, h in HORIZONS.items():
                    i = np.searchsorted(ts, r["trade_ts"] + h)
                    d[f"markout_{name}"] = (sign * (mids[i] - r["price"])) if i < len(ts) and ts[i] - (r["trade_ts"] + h) < 180 else np.nan
                d["capture"] = abs(r["price"] - r["mid_at"])
                rows.append(d)
        fm = pd.DataFrame(rows)
        out["fills"] = int(len(fm)); out["fills_by_side"] = fm["side"].value_counts().to_dict()
        out["our_shares_pro_rata"] = float(fm["our_pro"].sum()); out["our_shares_last_in_queue"] = float(fm["our_last"].sum())
        for name in HORIZONS:
            v = fm[f"markout_{name}"].dropna()
            if len(v):
                out[f"markout_{name}_c_per_share"] = float(v.mean() * 100)
        out["capture_c_per_share"] = float(fm["capture"].mean() * 100)
        if "markout_60m_c_per_share" in out:
            edge = (out["capture_c_per_share"] - out["markout_60m_c_per_share"]) / 100
            out["fill_pnl_usd_pro_rata"] = float(out["our_shares_pro_rata"] * edge)
            out["fill_pnl_usd_last"] = float(out["our_shares_last_in_queue"] * edge)
        by_side = fm.groupby("side").agg(n=("price", "size"), capture_c=("capture", lambda x: x.mean() * 100),
                                         markout_60m_c=("markout_60m", lambda x: x.mean() * 100)).round(3)
        out["by_side"] = by_side.to_dict(orient="index")
    out["per_market_top"] = q.groupby("condition_id").agg(accrual=("accrual", "sum"), share=("share", "median"), pool=("pool", "max")) \
        .sort_values("accrual", ascending=False).head(8).round(4).reset_index().merge(m[["condition_id", "question"]], on="condition_id", how="left") \
        [["question", "pool", "share", "accrual"]].to_dict(orient="records")
    if a.json:
        print(json.dumps(out, indent=1, default=float))
    else:
        for k, v in out.items():
            if k not in ("per_market_top", "by_side"):
                print(f"{k}: {v}")
        print("by_side:", out.get("by_side"))
        print("top markets by accrual:")
        for r in out["per_market_top"]:
            print(f"  ${r['pool']:>6.0f}/day  share {r['share']*100:5.2f}%  accrued ${r['accrual']:6.3f}  {str(r['question'])[:60]}")


if __name__ == "__main__":
    main()
