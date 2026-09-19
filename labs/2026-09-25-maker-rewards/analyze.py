"""Lab 2 analysis: what a passive maker earns on Polymarket, May 2026.

Inputs (from extract.py): data/labs/maker/snapshots.parquet, data/labs/maker/fills.parquet.
Outputs: results.json, tables.md, figures/.

Three questions, three models, all assumptions explicit:

A. Rewards. At every snapshot we pretend to rest S shares on each side at the current best
   bid and best ask (joining the queue). Polymarket's score is ((v - s)/v)^2 * size with
   v = rewardsMaxSpread and s = our distance from mid in cents, sampled per minute; the
   two-sided rule is max(min(Q1,Q2), max(Q1,Q2)/3) inside [0.10, 0.90] and min(Q1,Q2)
   outside. Our share of the daily pool = Q_us / (Q_us + Q_book). The snapshots carry only
   best-level depth, so the competing score is best-level score times a corridor factor K
   calibrated on live CLOB books (data/labs/maker/live_book_calibration.csv, 36 markets,
   2026-09-19): reward-weighted corridor score / best-level score = 1.3 (p25), 1.7 (median),
   5.3 (p75). Eligibility: S >= rewardsMinSize, s <= v, pool > 0, and the $1 minimum payout.
   Capital for two-sided S shares ~ S dollars (bid at p costs p*S, ask at 1-p costs (1-p)*S).

B. Adverse selection. For every taker fill whose price matches the snapshot's touch on the
   right side (within one tick, snapshot at most 90 s old) we take the YES mid just before
   and 5 / 15 / 60 minutes after. The maker on the other side captured (price - mid) of
   spread and lost the signed move of the mid: markout. Net = capture - markout (+ rebate).

C. Net. Per market-day: rewards + expected fills * (capture - markout_60 + rebate). Expected
   fills per taker fill = fill size * S / (best depth + S), capped at S (we re-quote after
   each fill, never more than our size at once).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / "data" / "labs" / "maker"
FIG = HERE / "figures"
FIG.mkdir(exist_ok=True)

SIZES = (100, 200, 500, 1000, 5000)     # shares per side
# Competition inside the rewards corridor, anchored on Gamma's `liquidity` field (USD), which the
# snapshots carry. Live CLOB books on 2026-09-19 (37 markets, mid 20-80c): corridor shares =
# ALPHA * liquidity / mid with ALPHA p25/p50/p75 = 0.27 / 0.65 / 0.90; the average reward weight of
# corridor orders relative to an order at the touch is RHO (median of the same sample).
KFACT = {"k13": 0.27, "k17": 0.65, "k53": 0.90}   # keys kept for the figure/table code: low / central / high competition
RHO = 0.55
QUEUE_BETA = 0.13   # best-level depth as a share of liquidity-shares on live books; floor for the fill-queue estimate
HORIZONS = {"5m": 300, "15m": 900, "60m": 3600}
MID_BUCKETS = [0.0, 0.35, 0.45, 0.55, 0.65, 1.0]
MID_LABELS = ["30–35¢", "35–45¢", "45–55¢", "55–65¢", "65–80¢"]
REBATE = {"sports": 0.15, "crypto": 0.20, "event": 0.25, "financial": 0.25}
TAKER_RATE = {"sports": 0.05, "crypto": 0.07, "event": 0.04, "financial": 0.04}
MIN_PAYOUT = 1.0
SNAP_TOL = 90      # seconds: max age of the snapshot that defines "the book at the fill"
AFTER_TOL = 120    # seconds: how close the post-fill snapshot must be to the horizon


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    s = pd.read_parquet(DATA / "snapshots.parquet")
    s = s.dropna(subset=["bid", "ask"]).copy()
    s = s[(s["bid"] > 0) & (s["ask"] < 1) & (s["ask"] > s["bid"])]
    s["mid"] = (s["bid"] + s["ask"]) / 2
    s["half_spread_c"] = (s["ask"] - s["bid"]) / 2 * 100
    s["tick"] = s["tick"].fillna(0.01)
    s["day"] = pd.to_datetime(s["ts"], unit="s").dt.date.astype(str)
    s["mid_bucket"] = pd.cut(s["mid"], MID_BUCKETS, labels=MID_LABELS, include_lowest=True).astype(str)
    f = pd.read_parquet(DATA / "fills.parquet")
    return s, f


# ---------------------------------------------------------------- A. rewards
def two_sided(q1: np.ndarray, q2: np.ndarray, mid: np.ndarray) -> np.ndarray:
    inside = (mid >= 0.10) & (mid <= 0.90)
    return np.where(inside, np.maximum(np.minimum(q1, q2), np.maximum(q1, q2) / 3.0), np.minimum(q1, q2))


def rewards_model(s: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    v = s["reward_max_spread"].fillna(0).to_numpy()
    hs = s["half_spread_c"].to_numpy()
    mid = s["mid"].to_numpy()
    rate = s["reward_rate"].fillna(0).to_numpy()
    minsz = s["reward_min_size"].fillna(0).to_numpy()
    liq_shares = (s["liquidity"].fillna(0).to_numpy() / np.clip(mid, 0.05, 0.95))
    with np.errstate(divide="ignore", invalid="ignore"):
        w = np.where((v > 0) & (hs <= v), ((v - hs) / np.where(v > 0, v, 1)) ** 2, 0.0)
    # competing score per unit ALPHA: corridor shares split evenly across the two sides, weighted RHO * w
    q_best = RHO * w * liq_shares / 2.0  # per side; two-sided rule with equal sides returns the same
    rows = []
    for S in SIZES:
        elig = (rate > 0) & (w > 0) & (S >= minsz)
        q_us = np.where(elig, w * S, 0.0)
        base = {"market_id": s["market_id"].to_numpy(), "day": s["day"].to_numpy(), "category": s["category"].to_numpy(),
                "mid_bucket": s["mid_bucket"].to_numpy(), "rate": rate, "elig": elig.astype(float)}
        for name, k in KFACT.items():
            denom = q_us + k * q_best
            base[f"share_{name}"] = np.where(denom > 0, q_us / np.where(denom > 0, denom, 1), 0.0)
        df = pd.DataFrame(base)
        agg = {"category": ("category", "first"), "mid_bucket": ("mid_bucket", "first"), "rate": ("rate", "max"), "elig": ("elig", "mean")}
        agg.update({f"share_{n}": (f"share_{n}", "mean") for n in KFACT})
        g = df.groupby(["market_id", "day"]).agg(**agg).reset_index()
        g["size"] = S
        for n in KFACT:
            r = g["rate"] * g[f"share_{n}"]
            g[f"reward_{n}"] = np.where(r >= MIN_PAYOUT, r, 0.0)
            g[f"yield_{n}"] = g[f"reward_{n}"] / S
        rows.append(g)
    out = pd.concat(rows, ignore_index=True)
    summary = {}
    for S in SIZES:
        d = out[out["size"] == S]
        e = d[d["elig"] > 0.5]
        item = {"market_days": int(len(d)), "eligible_market_days": int(len(e)), "share_eligible": float(len(e) / max(len(d), 1))}
        for n in KFACT:
            item[f"median_reward_{n}"] = float(e[f"reward_{n}"].median()) if len(e) else 0.0
            item[f"mean_reward_{n}"] = float(e[f"reward_{n}"].mean()) if len(e) else 0.0
            item[f"median_yield_{n}_pct"] = float(e[f"yield_{n}"].median() * 100) if len(e) else 0.0
            item[f"mean_yield_{n}_pct"] = float(e[f"yield_{n}"].mean() * 100) if len(e) else 0.0
            item[f"market_days_paying_{n}"] = int((e[f"reward_{n}"] > 0).sum())
        item["by_mid_bucket"] = {k: {"n": int(len(x)), **{f"yield_{n}_pct": float(x[f"yield_{n}"].median() * 100) for n in KFACT}}
                                 for k, x in e.groupby("mid_bucket") if len(x) >= 20}
        item["by_rate"] = {str(int(k)): {"n": int(len(x)), **{f"yield_{n}_pct": float(x[f"yield_{n}"].median() * 100) for n in KFACT}}
                           for k, x in e.groupby("rate") if len(x) >= 20}
        summary[str(S)] = item
    return out, summary


# ---------------------------------------------------------------- B. adverse selection
def align_fills(s: pd.DataFrame, f: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    tok = s.dropna(subset=["yes_token", "condition_id"]).drop_duplicates("condition_id")[["condition_id", "yes_token", "market_id", "category"]]
    f = f.merge(tok, on="condition_id", how="inner")
    n_in = len(f)
    is_yes = f["asset"].astype(str) == f["yes_token"].astype(str)
    f["p_yes"] = np.where(is_yes, f["price"], 1 - f["price"])
    f["taker_buys_yes"] = np.where(is_yes, f["side"] == "BUY", f["side"] == "SELL")
    f = f[(f["p_yes"] > 0) & (f["p_yes"] < 1)].copy()
    out = []
    for mid_, g in f.groupby("market_id", sort=False):
        snap = s[s["market_id"] == mid_].sort_values("ts")
        if len(snap) < 10:
            continue
        ts = snap["ts"].to_numpy(); mids = snap["mid"].to_numpy(); bids = snap["bid"].to_numpy(); asks = snap["ask"].to_numpy()
        bbs = snap["best_bid_size"].fillna(0).to_numpy(); bas = snap["best_ask_size"].fillna(0).to_numpy(); ticks = snap["tick"].to_numpy()
        ft = g["ts"].to_numpy()
        i0 = np.clip(np.searchsorted(ts, ft, side="right") - 1, 0, len(ts) - 1)
        ok = (ft - ts[i0] <= SNAP_TOL) & (ft >= ts[i0])
        liqs = (snap["liquidity"].fillna(0).to_numpy() / np.clip(mids, 0.05, 0.95)) * QUEUE_BETA / 2.0
        depth_rec = np.where(g["taker_buys_yes"], bas[i0], bbs[i0])
        g = g.assign(mid0=np.where(ok, mids[i0], np.nan), bid0=np.where(ok, bids[i0], np.nan), ask0=np.where(ok, asks[i0], np.nan),
                     tick0=ticks[i0], depth_rec=depth_rec, depth0=np.maximum(depth_rec, liqs[i0]))
        for name, h in HORIZONS.items():
            i1 = np.clip(np.searchsorted(ts, ft + h, side="left"), 0, len(ts) - 1)
            ok1 = (np.abs(ts[i1] - (ft + h)) <= AFTER_TOL)
            g[f"mid_{name}"] = np.where(ok1, mids[i1], np.nan)
        out.append(g)
    f = pd.concat(out, ignore_index=True)
    f = f.dropna(subset=["mid0"])
    n_aligned = len(f)
    # the fill must sit on the touch of the snapshot: taker buys YES at the ask, sells YES at the bid
    touch = np.where(f["taker_buys_yes"], f["ask0"], f["bid0"])
    f = f[np.abs(f["p_yes"] - touch) <= f["tick0"] + 1e-9].copy()
    n_touch = len(f)
    sign = np.where(f["taker_buys_yes"], 1.0, -1.0)
    f["capture"] = (sign * (f["p_yes"] - f["mid0"])).clip(lower=0)
    for name in HORIZONS:
        f[f"markout_{name}"] = sign * (f[f"mid_{name}"] - f["p_yes"])  # maker loss per share, positive = loss
    f["mid_bucket"] = pd.cut(f["mid0"], MID_BUCKETS, labels=MID_LABELS, include_lowest=True).astype(str)
    f["size_bucket"] = pd.cut(f["notional"], [0, 20, 100, 500, 1e9], labels=["<$20", "$20–100", "$100–500", ">$500"]).astype(str)
    return f, {"fills_on_snapshot_markets": int(n_in), "fills_with_fresh_snapshot": int(n_aligned), "fills_at_touch": int(n_touch)}


def markout_summary(f: pd.DataFrame) -> dict:
    def agg(d: pd.DataFrame) -> dict:
        o = {"n": int(len(d)), "capture_c": float(d["capture"].mean() * 100), "notional": float(d["notional"].sum())}
        for name in HORIZONS:
            m = d[f"markout_{name}"].dropna()
            if not len(m):
                continue
            o[f"markout_{name}_c"] = float(m.mean() * 100)
            o[f"net_{name}_c"] = float((d.loc[m.index, "capture"] - m).mean() * 100)
            w = d.loc[m.index, "size"]
            o[f"net_{name}_c_sizew"] = float(((d.loc[m.index, "capture"] - m) * w).sum() / w.sum() * 100)
            o[f"markout_{name}_c_sizew"] = float((m * w).sum() / w.sum() * 100)
        return o
    res = {"fills": int(len(f)), "markets": int(f["market_id"].nunique()), "all": agg(f)}
    res["by_mid_bucket"] = {k: agg(d) for k, d in f.groupby("mid_bucket") if len(d) >= 200}
    res["by_category"] = {k: agg(d) for k, d in f.groupby("category") if len(d) >= 200}
    res["by_taker_side"] = {("taker buys YES" if k else "taker sells YES"): agg(d) for k, d in f.groupby("taker_buys_yes")}
    res["by_fill_size"] = {k: agg(d) for k, d in f.groupby("size_bucket") if len(d) >= 200}
    big = f[f["size"] > f["depth0"].fillna(0)]   # fills that exhausted the touch: what a last-in-queue maker gets
    res["fills_exhausting_touch"] = agg(big) if len(big) >= 200 else {"n": int(len(big))}
    # bootstrap by market for the headline 60m net
    rng = np.random.default_rng(3)
    m = f.dropna(subset=["markout_60m"])
    per = m.assign(net=(m["capture"] - m["markout_60m"]) * m["size"]).groupby("market_id").agg(net=("net", "sum"), sz=("size", "sum"))
    net, sz = per["net"].to_numpy(), per["sz"].to_numpy()
    boots = np.empty(2000)
    for a in range(0, 2000, 100):
        idx = rng.integers(0, len(net), size=(100, len(net)))
        boots[a:a + 100] = net[idx].sum(axis=1) / sz[idx].sum(axis=1)
    res["net_60m_sizew_ci_c"] = [float(np.percentile(boots, 2.5) * 100), float(np.percentile(boots, 97.5) * 100)]
    return res


# ---------------------------------------------------------------- C. net maker P&L
def net_model(f: pd.DataFrame, rewards: pd.DataFrame) -> dict:
    f = f.dropna(subset=["markout_60m"]).copy()
    f["day"] = pd.to_datetime(f["ts"], unit="s").dt.date.astype(str)
    f["edge_60"] = f["capture"] - f["markout_60m"]
    fee_rate = f["category"].map(TAKER_RATE).fillna(0.05)
    f["rebate"] = f["category"].map(REBATE).fillna(0.25) * fee_rate * f["p_yes"] * (1 - f["p_yes"])
    out = {}
    for S in SIZES:
        share = S / (f["depth0"].fillna(0).clip(lower=0) + S)
        f["our_shares"] = np.minimum(f["size"] * share, S)                       # pro-rata at the touch
        f["our_shares_last"] = np.minimum(np.maximum(f["size"] - f["depth0"].fillna(0), 0), S)  # last in queue
        f["our_capture"] = f["our_shares"] * f["capture"]
        f["our_markout"] = f["our_shares"] * f["markout_60m"]
        f["our_rebate"] = f["our_shares"] * f["rebate"]
        f["our_pnl"] = f["our_capture"] - f["our_markout"] + f["our_rebate"]
        f["our_pnl_last"] = f["our_shares_last"] * (f["edge_60"] + f["rebate"])
        md = f.groupby(["market_id", "day"]).agg(fill_pnl=("our_pnl", "sum"), shares=("our_shares", "sum"), capture=("our_capture", "sum"),
                                                markout=("our_markout", "sum"), rebate=("our_rebate", "sum"), fills=("our_shares", "size"),
                                                fill_pnl_last=("our_pnl_last", "sum"), shares_last=("our_shares_last", "sum")).reset_index()
        r = rewards[rewards["size"] == S][["market_id", "day", "elig", "mid_bucket", "category", "rate"] + [f"reward_{n}" for n in KFACT]]
        md = r.merge(md, on=["market_id", "day"], how="left").fillna({"fill_pnl": 0, "shares": 0, "capture": 0, "markout": 0, "rebate": 0, "fills": 0,
                                                                           "fill_pnl_last": 0, "shares_last": 0})
        md = md[md["elig"] > 0.5]
        for n in KFACT:
            md[f"net_{n}"] = md[f"reward_{n}"] + md["fill_pnl"]
            md[f"netlast_{n}"] = md[f"reward_{n}"] + md["fill_pnl_last"]
        pct = lambda c: float(md[c].mean() / S * 100)  # noqa: E731
        item = {"market_days": int(len(md)), "shares_filled_per_day": float(md["shares"].mean()),
                "turnover_per_day": float(md["shares"].mean() / S), "turnover_per_day_last": float(md["shares_last"].mean() / S),
                "per_day_pct_of_capital": {c: pct(c) for c in ["capture", "markout", "rebate", "fill_pnl", "fill_pnl_last"] + [f"reward_{n}" for n in KFACT] + [f"net_{n}" for n in KFACT] + [f"netlast_{n}" for n in KFACT]},
                "median_net_pct": {n: float(md[f"net_{n}"].median() / S * 100) for n in KFACT},
                "share_days_positive": {n: float((md[f"net_{n}"] > 0).mean()) for n in KFACT},
                "by_mid_bucket": {k: {"n": int(len(x)), **{f"net_{n}_pct": float(x[f"net_{n}"].mean() / S * 100) for n in KFACT},
                                      "reward_k17_pct": float(x["reward_k17"].mean() / S * 100), "fill_pnl_pct": float(x["fill_pnl"].mean() / S * 100)}
                                  for k, x in md.groupby("mid_bucket") if len(x) >= 20},
                "by_category": {k: {"n": int(len(x)), **{f"net_{n}_pct": float(x[f"net_{n}"].mean() / S * 100) for n in KFACT}}
                                for k, x in md.groupby("category") if len(x) >= 20},
                "by_rate": {str(int(k)): {"n": int(len(x)), "net_k17_pct": float(x["net_k17"].mean() / S * 100), "reward_k17_pct": float(x["reward_k17"].mean() / S * 100)}
                            for k, x in md.groupby("rate") if len(x) >= 20}}
        out[str(S)] = item
    return out


# ---------------------------------------------------------------- figures
def fig_rewards(summary: dict) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.4))
    x = np.arange(len(SIZES))
    for j, (n, col, lab) in enumerate((("k13", "#176B4A", "corridor = 0.27 × liquidity shares (p25, thin)"), ("k17", "#9A6612", "0.65 × (median, live books)"), ("k53", "#A3372B", "0.90 × (p75, deep)"))):
        ax.bar(x + (j - 1) * 0.25, [summary[str(S)][f"mean_yield_{n}_pct"] for S in SIZES], width=0.25, color=col, label=lab)
    for i, S in enumerate(SIZES):
        ax.text(i, max(summary[str(S)][f"mean_yield_{n}_pct"] for n in KFACT) + 0.02, f"eligible {summary[str(S)]['share_eligible']:.0%}", ha="center", fontsize=7)
    ax.set_xticks(x); ax.set_xticklabels([f"{S} shares/side" for S in SIZES], fontsize=8)
    ax.set_ylabel("mean liquidity reward, % of capital per day"); ax.grid(axis="y", alpha=.25); ax.legend(fontsize=8)
    ax.set_title("Liquidity rewards for a two-sided quote at the touch, eligible market-days (May 2026)")
    fig.tight_layout(); fig.savefig(FIG / "reward_yield.png", dpi=160); plt.close(fig)


def fig_markout(res: dict) -> None:
    fig, ax = plt.subplots(figsize=(9, 4.4))
    b = res["by_mid_bucket"]
    labels = [k for k in MID_LABELS if k in b]
    x = np.arange(len(labels))
    ax.bar(x - 0.3, [b[k]["capture_c"] for k in labels], width=0.2, color="#B7B7B7", label="spread captured")
    for i, (name, col) in enumerate((("5m", "#9A6612"), ("15m", "#176B4A"), ("60m", "#A3372B"))):
        ax.bar(x - 0.1 + 0.2 * i, [b[k].get(f"markout_{name}_c", 0) for k in labels], width=0.2, color=col, label=f"mid move against the maker after {name} (negative = in the maker's favor)")
    for i, k in enumerate(labels):
        ax.text(i, max(b[k]["capture_c"], b[k].get("markout_60m_c", 0)) + 0.03, f"n={b[k]['n']:,}", ha="center", fontsize=7)
    ax.axhline(0, color="#999", lw=1); ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=8)
    ax.set_ylabel("cents per share"); ax.set_title("Fills at the touch: spread captured vs the mid's move afterwards (May 2026, 347k fills)")
    ax.grid(axis="y", alpha=.25); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(FIG / "markout.png", dpi=160); plt.close(fig)


def fig_net(net: dict) -> None:
    fig, ax = plt.subplots(figsize=(9.5, 4.4))
    keys = ["reward_k17", "capture", "markout", "rebate", "net_k13", "net_k17", "net_k53"]
    names = ["rewards (median comp.)", "spread captured", "markout 60m", "fee rebate", "net (thin comp.)", "net (median)", "net (deep comp.)"]
    x = np.arange(len(keys))
    cols = ("#A3372B", "#C46A3A", "#9A6612", "#176B4A", "#2F4F7F")
    for j, (S, col) in enumerate(zip(SIZES, cols)):
        vals = [net[str(S)]["per_day_pct_of_capital"][k] * (-1 if k == "markout" else 1) for k in keys]
        ax.bar(x + (j - 2) * 0.16, vals, width=0.16, color=col, label=f"S={S}")
    ax.axhline(0, color="#999", lw=1); ax.set_xticks(x); ax.set_xticklabels(names, fontsize=8)
    ax.set_ylabel("% of capital per day (mean over eligible market-days)")
    ax.set_title("Maker P&L decomposition per market-day by quote size")
    ax.grid(axis="y", alpha=.25); ax.legend(fontsize=8, ncol=5)
    fig.tight_layout(); fig.savefig(FIG / "net_pnl.png", dpi=160); plt.close(fig)


def main() -> None:
    argparse.ArgumentParser().parse_known_args()
    s, f = load()
    res = {"snapshots": int(len(s)), "markets": int(s["market_id"].nunique()),
           "window": [str(pd.to_datetime(s["ts"].min(), unit="s").date()), str(pd.to_datetime(s["ts"].max(), unit="s").date())],
           "snapshot_gap_median_s": float(s.sort_values(["market_id", "ts"]).groupby("market_id")["ts"].diff().median()),
           "rewards_markets": int(s.loc[s["reward_rate"] > 0, "market_id"].nunique()),
           "spread_median_c": float(s["half_spread_c"].median() * 2),
           "min_size_dist": {str(int(k)): int(v) for k, v in s["reward_min_size"].fillna(0).value_counts().head(6).items()},
           "assumptions": {"sizes": SIZES, "corridor_alpha": KFACT, "rho": RHO, "queue_beta": QUEUE_BETA, "min_payout": MIN_PAYOUT, "rebate": REBATE,
                           "taker_rate": TAKER_RATE, "snap_tol_s": SNAP_TOL, "after_tol_s": AFTER_TOL}}
    print("snapshots", res["snapshots"], "markets", res["markets"], "gap", res["snapshot_gap_median_s"], file=sys.stderr)
    rew, res["rewards"] = rewards_model(s)
    print("rewards model done", file=sys.stderr)
    fa, counts = align_fills(s, f)
    res["fill_counts"] = counts
    res["markout"] = markout_summary(fa)
    print("markout done", counts, file=sys.stderr)
    res["net"] = net_model(fa, rew)
    (HERE / "results.json").write_text(json.dumps(res, indent=1, default=float))
    fig_rewards(res["rewards"]); fig_markout(res["markout"]); fig_net(res["net"])
    md = ["# Tables", "", "## A. Rewards by quote size (eligible market-days; capital ≈ S dollars)", "",
          "| S shares/side | eligible md | % eligible | mean $/day thin | median comp. | deep | mean %/day thin | median | deep | md paying (median) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for S in SIZES:
        r = res["rewards"][str(S)]
        md.append(f"| {S} | {r['eligible_market_days']:,} | {r['share_eligible']:.0%} | {r['mean_reward_k13']:.2f} | {r['mean_reward_k17']:.2f} | {r['mean_reward_k53']:.2f} | "
                  f"{r['mean_yield_k13_pct']:.3f} | {r['mean_yield_k17_pct']:.3f} | {r['mean_yield_k53_pct']:.3f} | {r['market_days_paying_k17']:,} |")
    md += ["", "## B. Markout by mid bucket (cents per share, fills at the touch)", "", "| bucket | n | capture | markout 5m | 15m | 60m | net 60m | net 60m size-w |", "|---|---|---|---|---|---|---|---|"]
    for k in MID_LABELS:
        b = res["markout"]["by_mid_bucket"].get(k)
        if b:
            md.append(f"| {k} | {b['n']:,} | {b['capture_c']:.2f} | {b.get('markout_5m_c', float('nan')):.2f} | {b.get('markout_15m_c', float('nan')):.2f} | {b.get('markout_60m_c', float('nan')):.2f} | {b.get('net_60m_c', float('nan')):.2f} | {b.get('net_60m_c_sizew', float('nan')):.2f} |")
    md += ["", "## C. Net per market-day (% of capital per day)", "", "| S | md | turnover/day | rewards (median comp.) | capture | markout | rebate | net thin | net median | net deep | days>0 (median) |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for S in SIZES:
        n = res["net"][str(S)]; p = n["per_day_pct_of_capital"]
        md.append(f"| {S} | {n['market_days']:,} | {n['turnover_per_day']:.2f} | {p['reward_k17']:.3f} | {p['capture']:.3f} | {p['markout']:.3f} | {p['rebate']:.3f} | "
                  f"{p['net_k13']:.3f} | {p['net_k17']:.3f} | {p['net_k53']:.3f} | {n['share_days_positive']['k17']:.0%} |")
    (HERE / "tables.md").write_text("\n".join(md) + "\n")
    print(json.dumps({"fill_counts": counts, "markout_all": res["markout"]["all"], "net60_ci": res["markout"]["net_60m_sizew_ci_c"],
                      "rewards": {k: {kk: round(v[kk], 3) for kk in ("share_eligible", "mean_reward_k17", "mean_yield_k13_pct", "mean_yield_k17_pct", "mean_yield_k53_pct")} for k, v in res["rewards"].items()},
                      "net": {k: {"turnover": round(v["turnover_per_day"], 3), **{kk: round(vv, 3) for kk, vv in v["per_day_pct_of_capital"].items()}} for k, v in res["net"].items()}}, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
