"""Stage 4: calibration by price bucket, longshot returns (gross and net), robustness cuts, figures.

Reads data/labs/longshot/observations.parquet, writes results.json, tables.md and figures/.

Net returns use Polymarket's 2026 taker fee, fee = rate * p * (1 - p) per share, with the
category rates in FEE_RATE (docs.polymarket.com/trading/fees, July 2026 schedule), plus a
flat SPREAD_HAIRCUT per share for crossing the spread. Makers pay no fee; a maker would
keep the haircut too, so "gross" is the maker-side ceiling and "net" the taker-side floor.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import FIG, OBS_PARQUET, RESULTS_JSON  # noqa: E402

BUCKETS = [0.0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98, 1.0]
LONGSHOT_MAX = 0.20
LS_BANDS = ((0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.0, 0.20))
MAIN_H = (1, 7)
FEE_RATE = {"sports": 0.05, "crypto": 0.07, "politics_macro": 0.04, "culture": 0.05, "other": 0.05}
SPREAD_HAIRCUT = 0.01  # $ per share paid for crossing the spread (taker)
RNG = np.random.default_rng(42)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (c - h, c + h)


def sides(df: pd.DataFrame) -> pd.DataFrame:
    """Each market-horizon row becomes two side rows: YES at p and NO at 1-p."""
    a = df.assign(side="YES", price=df["p_yes"], win=df["y_yes"])
    b = df.assign(side="NO", price=1 - df["p_yes"], win=1 - df["y_yes"])
    return pd.concat([a, b], ignore_index=True)


def bucket_table(s: pd.DataFrame) -> pd.DataFrame:
    s = s.copy()
    s["bucket"] = pd.cut(s["price"], BUCKETS, right=False, include_lowest=True)
    g = s.groupby("bucket", observed=True)
    out = g.agg(n=("win", "size"), implied=("price", "mean"), realized=("win", "mean"),
                markets=("market_id", "nunique")).reset_index()
    ci = [wilson(int(round(r * n)), int(n)) for r, n in zip(out["realized"], out["n"])]
    out["ci_lo"] = [c[0] for c in ci]
    out["ci_hi"] = [c[1] for c in ci]
    out["excess_pp"] = (out["realized"] - out["implied"]) * 100
    out["buy_ret"] = out["realized"] / out["implied"] - 1
    out["fade_ret"] = (1 - out["realized"]) / (1 - out["implied"]) - 1
    out["bucket"] = out["bucket"].astype(str)
    return out


def fade_pnl(s: pd.DataFrame, net) -> tuple[np.ndarray, np.ndarray]:
    """Per-market cost and payoff of selling every longshot side in s.

    Selling a longshot at p = buying one share of the complement at (1 - p).
    Payoff is 1 if the longshot loses. Net adds the taker fee and the spread haircut."""
    p = s["price"].to_numpy()
    cost = 1 - p
    if net:
        rate = s["category"].map(FEE_RATE).fillna(0.05).to_numpy()
        cost = cost + rate * p * (1 - p) + (SPREAD_HAIRCUT if net is True else 0.0)
    pay = 1 - s["win"].to_numpy()
    tmp = pd.DataFrame({"m": s["market_id"].to_numpy(), "cost": cost, "pay": pay}).groupby("m").sum()
    return tmp["cost"].to_numpy(), tmp["pay"].to_numpy()


def bootstrap_fade(s: pd.DataFrame, n_boot: int = 2000) -> dict:
    if s.empty:
        return {"n": 0}
    out = {"n": int(len(s)), "markets": int(s["market_id"].nunique()),
           "implied": float(s["price"].mean()), "realized": float(s["win"].mean())}
    for label, net in (("gross", False), ("net_fee", "fee"), ("net", True)):
        cost, pay = fade_pnl(s, net)
        point = pay.sum() / cost.sum() - 1
        idx = RNG.integers(0, len(cost), size=(n_boot, len(cost)))
        boots = pay[idx].sum(axis=1) / cost[idx].sum(axis=1) - 1
        out[label] = {"ret": float(point), "ci_lo": float(np.percentile(boots, 2.5)),
                      "ci_hi": float(np.percentile(boots, 97.5))}
    # breakeven spread haircut per share (cents): edge left after fees, spread over positions
    cost_f, pay_f = fade_pnl(s, "fee")
    out["breakeven_haircut_cents"] = float((pay_f.sum() - cost_f.sum()) / len(s) * 100)
    # convenience aliases (gross)
    out["ret"], out["ci_lo"], out["ci_hi"] = out["gross"]["ret"], out["gross"]["ci_lo"], out["gross"]["ci_hi"]
    return out


def fig_calibration(df: pd.DataFrame) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), sharey=True)
    for ax, h in zip(axes, MAIN_H):
        s = df[df["horizon_days"] == h]
        t = bucket_table(s.assign(side="YES", price=s["p_yes"], win=s["y_yes"]))
        ax.plot([0, 1], [0, 1], color="#999", lw=1, ls="--", label="perfect calibration")
        ax.errorbar(t["implied"], t["realized"], yerr=[t["realized"] - t["ci_lo"], t["ci_hi"] - t["realized"]],
                    fmt="o", color="#176B4A", ms=5, capsize=3, label="YES token, 95% CI")
        for _, r in t.iterrows():
            ax.annotate(f"n={int(r['n'])}", (r["implied"], r["realized"]), fontsize=6.5,
                        xytext=(4, -9), textcoords="offset points", color="#555")
        ax.set_title(f"{h} day(s) before resolution")
        ax.set_xlabel("market price of YES")
        ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.grid(alpha=.25)
    axes[0].set_ylabel("share that resolved YES")
    axes[0].legend(loc="upper left", fontsize=8)
    fig.suptitle("Polymarket calibration by price bucket (closed markets, volume ≥ $5k)")
    fig.tight_layout()
    fig.savefig(FIG / "calibration.png", dpi=160)
    plt.close(fig)


def fig_longshots(df: pd.DataFrame) -> None:
    s_all = sides(df)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6))
    for ax, h in zip(axes, MAIN_H):
        s = s_all[(s_all["horizon_days"] == h) & (s_all["price"] < LONGSHOT_MAX)]
        t = bucket_table(s)
        x = np.arange(len(t))
        ax.bar(x - 0.2, t["implied"] * 100, width=0.4, color="#B7B7B7", label="implied (mean price)")
        ax.bar(x + 0.2, t["realized"] * 100, width=0.4, color="#176B4A", label="realized win rate")
        ax.errorbar(x + 0.2, t["realized"] * 100, yerr=[(t["realized"] - t["ci_lo"]) * 100, (t["ci_hi"] - t["realized"]) * 100],
                    fmt="none", ecolor="#0b3d2a", capsize=3)
        ax.set_xticks(x); ax.set_xticklabels(t["bucket"], fontsize=8)
        for i, n in enumerate(t["n"]):
            ax.text(i, max(t["realized"].iloc[i], t["implied"].iloc[i]) * 100 + 0.6, f"n={int(n)}", ha="center", fontsize=7)
        ax.set_title(f"Longshots (either side < 20¢), {h} day(s) before resolution")
        ax.set_ylabel("%"); ax.grid(axis="y", alpha=.25)
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "longshots.png", dpi=160)
    plt.close(fig)


def fig_fade_by_horizon(res: dict) -> None:
    hs = sorted(int(k) for k in res["fade_by_horizon"])
    fig, ax = plt.subplots(figsize=(8, 4.4))
    for (lo, hi), col in zip(LS_BANDS[:3], ("#A3372B", "#9A6612", "#176B4A")):
        key = f"{lo:.2f}-{hi:.2f}"
        g = [res["fade_by_horizon"][str(h)][key] for h in hs]
        ax.plot(hs, [x["gross"]["ret"] * 100 for x in g], marker="o", color=col, label=f"sell {key}, gross")
        ax.fill_between(hs, [x["gross"]["ci_lo"] * 100 for x in g], [x["gross"]["ci_hi"] * 100 for x in g], color=col, alpha=.10)
        ax.plot(hs, [x["net"]["ret"] * 100 for x in g], marker="x", ls="--", color=col, label=f"sell {key}, net of fee + 1¢")
    ax.axhline(0, color="#999", lw=1)
    ax.set_xlabel("days before resolution when the position is opened")
    ax.set_ylabel("return on capital, %")
    ax.set_xscale("log"); ax.set_xticks(hs); ax.set_xticklabels([str(h) for h in hs])
    ax.grid(alpha=.25); ax.legend(fontsize=7.5, ncol=2)
    ax.set_title("Selling longshots and holding to resolution (95% bootstrap CI by market, gross)")
    fig.tight_layout()
    fig.savefig(FIG / "fade_by_horizon.png", dpi=160)
    plt.close(fig)


def fig_category(res: dict) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharey=True)
    for ax, h in zip(axes, MAIN_H):
        d = res["by_category_longshots"][str(h)]
        cats = [c for c in ("sports", "politics_macro", "crypto", "culture", "other") if c in d and d[c].get("n", 0) > 0]
        x = np.arange(len(cats))
        ax.bar(x - 0.2, [d[c]["implied"] * 100 for c in cats], width=0.4, color="#B7B7B7", label="implied")
        ax.bar(x + 0.2, [d[c]["realized"] * 100 for c in cats], width=0.4, color="#176B4A", label="realized")
        for i, c in enumerate(cats):
            ax.text(i, max(d[c]["implied"], d[c]["realized"]) * 100 + 0.4, f"n={d[c]['n']}\nnet {d[c]['net']['ret']*100:+.1f}%", ha="center", fontsize=7)
        ax.set_xticks(x); ax.set_xticklabels(cats, fontsize=8)
        ax.set_ylim(0, max(max(d[c]["implied"], d[c]["realized"]) for c in cats) * 100 * 1.25)
        ax.set_title(f"Longshots < 20¢ by category, {h} day(s) before resolution")
        ax.grid(axis="y", alpha=.25)
    axes[0].set_ylabel("%"); axes[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "by_category.png", dpi=160)
    plt.close(fig)


def main() -> None:
    df = pd.read_parquet(OBS_PARQUET)
    res: dict = {
        "markets": int(df["market_id"].nunique()),
        "observations": int(len(df)),
        "close_window": [str(pd.to_datetime(df["close_ts"].min(), unit="s").date()),
                         str(pd.to_datetime(df["close_ts"].max(), unit="s").date())],
        "by_category": df[df["horizon_days"] == 1].groupby("category")["market_id"].nunique().to_dict(),
        "fee_rate": FEE_RATE, "spread_haircut": SPREAD_HAIRCUT,
        "calibration": {}, "longshot_buckets": {}, "fade_by_horizon": {},
        "by_category_longshots": {}, "by_month": {}, "by_volume": {}, "neg_risk": {}, "by_life": {},
        "by_event_src": {}, "clean_under5c": {},
    }
    s_all = sides(df)
    md = []
    for h in sorted(df["horizon_days"].unique()):
        h = int(h)
        s = df[df["horizon_days"] == h]
        cal = bucket_table(s.assign(side="YES", price=s["p_yes"], win=s["y_yes"]))
        res["calibration"][str(h)] = cal.to_dict(orient="records")
        ls = bucket_table(s_all[(s_all["horizon_days"] == h) & (s_all["price"] < LONGSHOT_MAX)])
        res["longshot_buckets"][str(h)] = ls.to_dict(orient="records")
        fb = {}
        for lo, hi in LS_BANDS:
            sub = s_all[(s_all["horizon_days"] == h) & (s_all["price"] >= lo) & (s_all["price"] < hi)]
            fb[f"{lo:.2f}-{hi:.2f}"] = bootstrap_fade(sub)
        res["fade_by_horizon"][str(h)] = fb
        if h in MAIN_H:
            md.append(f"\n### Horizon {h} day(s): YES-token calibration\n")
            md.append(cal[["bucket", "n", "implied", "realized", "ci_lo", "ci_hi", "excess_pp"]].round(4).to_markdown(index=False))
            md.append(f"\n### Horizon {h} day(s): longshot sides (< 20¢)\n")
            md.append(ls[["bucket", "n", "markets", "implied", "realized", "ci_lo", "ci_hi", "excess_pp", "buy_ret", "fade_ret"]].round(4).to_markdown(index=False))
            rows = [{"band": k, **{kk: v[kk] for kk in ("n", "markets", "implied", "realized")},
                     "gross": v["gross"]["ret"], "gross_lo": v["gross"]["ci_lo"], "gross_hi": v["gross"]["ci_hi"],
                     "net_fee": v["net_fee"]["ret"], "breakeven_c": v["breakeven_haircut_cents"], "net": v["net"]["ret"], "net_lo": v["net"]["ci_lo"], "net_hi": v["net"]["ci_hi"]} for k, v in fb.items() if v.get("n")]
            md.append(f"\n### Horizon {h} day(s): return of selling longshots (per $ of capital)\n")
            md.append(pd.DataFrame(rows).round(4).to_markdown(index=False))

    for h in MAIN_H:
        s = s_all[(s_all["horizon_days"] == h) & (s_all["price"] < LONGSHOT_MAX)]
        res["by_category_longshots"][str(h)] = {c: bootstrap_fade(g) for c, g in s.groupby("category")}
        res["by_month"][str(h)] = {m: bootstrap_fade(g, 500) for m, g in s.groupby("close_month")}
        vol_q = pd.qcut(s["volume"], 3, labels=["low", "mid", "high"], duplicates="drop")
        res["by_volume"][str(h)] = {str(k): bootstrap_fade(g) for k, g in s.groupby(vol_q, observed=True)}
        res["neg_risk"][str(h)] = {str(k): bootstrap_fade(g) for k, g in s.groupby("neg_risk")}
        life_q = pd.cut(s["life_days"], [0, 3, 14, 60, 10_000], labels=["<3d", "3-14d", "14-60d", ">60d"])
        res["by_life"][str(h)] = {str(k): bootstrap_fade(g) for k, g in s.groupby(life_q, observed=True)}
        res["by_event_src"][str(h)] = {str(k): bootstrap_fade(g) for k, g in s.groupby("event_src")}
        # the cleanest subsample: under 5 cents, event time not taken from closedTime
        clean = s[(s["price"] < 0.05) & (s["event_src"] != "closed_time")]
        res["clean_under5c"][str(h)] = bootstrap_fade(clean)
        md.append(f"\n### Horizon {h} day(s): longshots < 20¢ by category\n")
        md.append(pd.DataFrame([{"category": c, "n": v["n"], "markets": v["markets"], "implied": v["implied"], "realized": v["realized"],
                                 "gross": v["gross"]["ret"], "net": v["net"]["ret"], "net_lo": v["net"]["ci_lo"], "net_hi": v["net"]["ci_hi"]}
                                for c, v in res["by_category_longshots"][str(h)].items() if v.get("n")]).round(4).to_markdown(index=False))

    RESULTS_JSON.write_text(json.dumps(res, indent=1, default=float))
    (FIG.parent / "tables.md").write_text("# Tables\n" + "\n".join(md) + "\n")
    fig_calibration(df)
    fig_longshots(df)
    fig_fade_by_horizon(res)
    fig_category(res)
    print(json.dumps({k: res[k] for k in ("markets", "observations", "close_window", "by_category")}, indent=1), file=sys.stderr)
    for h in MAIN_H:
        for k, v in res["fade_by_horizon"][str(h)].items():
            if v.get("n"):
                print(f"h={h} {k}: n={v['n']} implied={v['implied']:.4f} realized={v['realized']:.4f} "
                      f"gross={v['gross']['ret']:+.4f} [{v['gross']['ci_lo']:+.4f},{v['gross']['ci_hi']:+.4f}] "
                      f"fee={v['net_fee']['ret']:+.4f} net1c={v['net']['ret']:+.4f} [{v['net']['ci_lo']:+.4f},{v['net']['ci_hi']:+.4f}] "
                      f"breakeven={v['breakeven_haircut_cents']:+.2f}c", file=sys.stderr)


if __name__ == "__main__":
    main()
