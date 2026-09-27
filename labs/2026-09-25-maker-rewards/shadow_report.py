"""Lab 2, shadow run: what a virtual two-sided quote would have earned on live books.

    python3 shadow_report.py --db data/labs/maker/shadow.db [--json results_shadow.json]

Input: the SQLite written by shadow_maker.py (quotes once a minute per market with the exact
reward share of a 1000-share bid / 500-share ask at the touch; every taker trade at our price
as a virtual fill with the pro-rata share). Output: reward accrual as a yield on the capital the
quotes tie up, fill turnover, and the markout of the virtual fills at 5 / 15 / 60 minutes
(mid after the fill vs the fill price, signed in our favour), split into the captured half-spread
and the drift after the fill, with a bootstrap interval over fills.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BID_SIZE, ASK_SIZE = 1000.0, 500.0
HORIZONS = (5, 15, 60)
TOL = 180                                   # a quote within this many seconds after t+h counts


def load(db: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    c = sqlite3.connect(db)
    q = pd.read_sql("select * from quotes", c)
    f = pd.read_sql("select * from fills", c)
    m = pd.read_sql("select * from markets", c)
    return q, f, m


def markouts(q: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    """Attach the touch at fill time and mid at t+h for every fill from the same market's quote stream.

    A taker that sweeps through several levels prints below our bid (or above our ask); our
    virtual order would have been filled at our own quote, so the execution price is the touch
    from the last quote sample before the trade, not the print. `price` (the print) is kept."""
    f = f.copy()
    f["exec"] = np.nan
    for h in HORIZONS:
        f[f"mid_{h}"] = np.nan
    for cid, fq in q.groupby("condition_id"):
        fq = fq.sort_values("ts")
        ts, mids = fq["ts"].to_numpy(), fq["mid"].to_numpy()
        bids, asks = fq["bid"].to_numpy(), fq["ask"].to_numpy()
        idx = f.index[f["condition_id"] == cid]
        if not len(idx):
            continue
        t0 = f.loc[idx, "trade_ts"].to_numpy()
        pos0 = np.clip(np.searchsorted(ts, t0, side="right") - 1, 0, len(ts) - 1)
        buy = (f.loc[idx, "side"] == "buy_yes").to_numpy()
        f.loc[idx, "exec"] = np.where(buy, bids[pos0], asks[pos0])
        for h in HORIZONS:
            target = f.loc[idx, "trade_ts"].to_numpy() + h * 60
            pos = np.searchsorted(ts, target, side="left")
            ok = (pos < len(ts)) & (np.where(pos < len(ts), ts[np.minimum(pos, len(ts) - 1)], 0) - target <= TOL)
            vals = np.full(len(idx), np.nan)
            vals[ok] = mids[pos[ok]]
            f.loc[idx, f"mid_{h}"] = vals
    sign = np.where(f["side"] == "buy_yes", 1.0, -1.0)          # we bought YES at our bid / sold YES at our ask
    f["capture_print_c"] = sign * (f["mid_at"] - f["price"]) * 100   # vs the taker's print (overstates sweeps)
    f["capture_c"] = sign * (f["mid_at"] - f["exec"]) * 100
    for h in HORIZONS:
        f[f"net_{h}_c"] = sign * (f[f"mid_{h}"] - f["exec"]) * 100
        f[f"drift_{h}_c"] = f[f"net_{h}_c"] - f["capture_c"]
    return f


def boot_mean(x: np.ndarray, w: np.ndarray | None, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    x = np.asarray(x, float)
    w = np.ones_like(x) if w is None else np.asarray(w, float)
    keep = ~np.isnan(x) & (w > 0)
    x, w = x[keep], w[keep]
    if not len(x):
        return float("nan"), float("nan"), float("nan")
    est = float(np.average(x, weights=w))
    idx = rng.integers(0, len(x), size=(n, len(x)))
    boots = (x[idx] * w[idx]).sum(1) / w[idx].sum(1)
    return est, float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", default=str(Path(__file__).resolve().parent / "results_shadow.json"))
    a = ap.parse_args()
    q, f, m = load(Path(a.db))
    q = q.sort_values("ts")
    hours = (q["ts"].max() - q["ts"].min()) / 3600
    days = hours / 24
    # capital tied up by the quotes: USDC behind the bid + YES held for the ask, valued at the touch
    q["capital"] = BID_SIZE * q["bid"] + ASK_SIZE * q["ask"]
    per_min = q.groupby("ts").agg(capital=("capital", "sum"), accrual=("accrual", "sum"), n=("condition_id", "size"))
    accrual_total = float(q["accrual"].sum())
    accrual_day = accrual_total / days
    capital_mean = float(per_min["capital"].mean())
    cov = {"hours": round(hours, 1), "days": round(days, 2), "markets": int(q["condition_id"].nunique()), "quotes": int(len(q)),
           "minutes_sampled": int(len(per_min)), "markets_per_minute": round(float(per_min["n"].mean()), 1),
           "coverage_pct": round(len(per_min) / max(1, hours * 60) * 100, 1)}
    rewards = {"accrued_usd": round(accrual_total, 2), "per_day_usd": round(accrual_day, 2), "capital_mean_usd": round(capital_mean),
               "yield_pct_per_day": round(accrual_day / capital_mean * 100, 3), "median_share_pct": round(float(q["share"].median() * 100), 2),
               "mean_share_pct": round(float(q["share"].mean() * 100), 2), "median_pool_usd_day": round(float(q["pool"].median()), 1),
               "median_corridor_shares": round(float(q["corridor_shares"].median())), "share_of_minutes_eligible": round(float((q["q_us"] > 0).mean() * 100), 1)}
    f = markouts(q, f)
    qq = q[["depth_bid", "depth_ask"]]
    fills = {"n": int(len(f)), "by_side": f["side"].value_counts().to_dict(), "taker_shares": round(float(f["size"].sum())),
             "touch_depth_median_bid": round(float(qq["depth_bid"].median())), "touch_depth_median_ask": round(float(qq["depth_ask"].median())),
             "minutes_our_bid_exceeds_touch_pct": round(float((qq["depth_bid"] < BID_SIZE).mean() * 100)),
             "spread_median_c": round(float(((q["ask"] - q["bid"]) * 100).median()), 2), "spread_mean_c": round(float(((q["ask"] - q["bid"]) * 100).mean()), 2),
             "our_shares_pro_rata": round(float(f["our_pro"].sum())), "our_shares_last_in_queue": round(float(f["our_last"].sum())),
             "our_shares_per_day": round(float(f["our_pro"].sum()) / days), "turnover_per_day_x": round(float(f["our_pro"].sum()) / days / (BID_SIZE + ASK_SIZE) / max(1, cov["markets_per_minute"]), 2),
             "median_taker_size": round(float(f["size"].median()), 1), "fills_per_market_day": round(len(f) / days / max(1, cov["markets_per_minute"]), 1)}
    mo = {}
    w = f["our_pro"].to_numpy()
    cap = boot_mean(f["capture_c"].to_numpy(), w)
    mo["capture_c_sizew"] = {"est": round(cap[0], 3), "lo": round(cap[1], 3), "hi": round(cap[2], 3)}
    capp = boot_mean(f["capture_print_c"].to_numpy(), w)
    mo["capture_print_c_sizew"] = {"est": round(capp[0], 3), "lo": round(capp[1], 3), "hi": round(capp[2], 3)}
    mo["sweep_share_pct"] = round(float(((f["side"] == "buy_yes") & (f["price"] < f["exec"] - 1e-9) | (f["side"] == "sell_yes") & (f["price"] > f["exec"] + 1e-9)).mean() * 100), 1)
    for h in HORIZONS:
        for key, wt in (("sizew", w), ("equal", None)):
            n = boot_mean(f[f"net_{h}_c"].to_numpy(), wt)
            d = boot_mean(f[f"drift_{h}_c"].to_numpy(), wt)
            mo[f"net_{h}m_c_{key}"] = {"est": round(n[0], 3), "lo": round(n[1], 3), "hi": round(n[2], 3), "n": int(f[f"net_{h}_c"].notna().sum())}
            mo[f"drift_{h}m_c_{key}"] = {"est": round(d[0], 3), "lo": round(d[1], 3), "hi": round(d[2], 3)}
        for side, g in f.groupby("side"):
            n = boot_mean(g[f"net_{h}_c"].to_numpy(), g["our_pro"].to_numpy())
            mo[f"net_{h}m_c_sizew_{side}"] = {"est": round(n[0], 3), "lo": round(n[1], 3), "hi": round(n[2], 3), "n": int(len(g))}
    # dollars per day: fills x net markout (size-weighted), rewards, and both as % of capital
    fill_pnl_day = {h: round(float(np.nansum(f["our_pro"] * f[f"net_{h}_c"] / 100)) / days, 2) for h in HORIZONS}
    econ = {"reward_usd_day": round(accrual_day, 2), "fill_pnl_usd_day": fill_pnl_day,
            "reward_pct_day": round(accrual_day / capital_mean * 100, 3),
            "fill_pnl_pct_day": {h: round(v / capital_mean * 100, 3) for h, v in fill_pnl_day.items()},
            "total_pct_day_15m": round((accrual_day + fill_pnl_day[15]) / capital_mean * 100, 3)}
    # inventory: fills are not round trips; what stays on the book is resolution risk the markout cannot see
    signed = np.where(f["side"] == "buy_yes", f["our_pro"], -f["our_pro"])
    inv = pd.DataFrame({"cid": f["condition_id"], "s": signed, "g": f["our_pro"]}).groupby("cid").agg(net=("s", "sum"), gross=("g", "sum"))
    econ["net_inventory_shares_total"] = round(float(inv["net"].abs().sum()))
    econ["net_inventory_pct_of_gross"] = round(float(inv["net"].abs().sum() / inv["gross"].sum() * 100), 1)
    econ["net_inventory_shares_per_day"] = round(float(inv["net"].abs().sum()) / days)
    econ["net_inventory_per_market_median"] = round(float(inv["net"].abs().median()))
    econ["net_inventory_per_market_p90"] = round(float(inv["net"].abs().quantile(0.9)))
    # per-market dispersion of reward yield
    pm = q.groupby("condition_id").agg(acc=("accrual", "sum"), cap=("capital", "mean"), mins=("ts", "size"), pool=("pool", "first"), share=("share", "median"))
    pm["yield_day"] = pm["acc"] / (pm["mins"] / 1440) / pm["cap"] * 100
    disp = {"market_yield_pct_day_median": round(float(pm["yield_day"].median()), 3), "market_yield_pct_day_p10": round(float(pm["yield_day"].quantile(0.1)), 3),
            "market_yield_pct_day_p90": round(float(pm["yield_day"].quantile(0.9)), 3), "markets_with_fills": int(f["condition_id"].nunique())}
    res = {"coverage": cov, "rewards": rewards, "fills": fills, "markout": mo, "economics": econ, "dispersion": disp,
           "assumptions": {"bid_size": BID_SIZE, "ask_size": ASK_SIZE, "capital": "1000 x bid + 500 x ask per market, averaged over sampled minutes",
                           "markout": "mid at t+h vs fill price, signed in our favour; capture = mid at fill vs price; drift = net - capture",
                           "fill_share": "pro-rata S/(depth+S) of each taker trade at our price, capped at S; last-in-queue variant reported as a floor"}}
    Path(a.json).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
