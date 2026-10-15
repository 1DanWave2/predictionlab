"""Lab 3, stage 5: what four weeks of matched Kalshi <-> Polymarket quotes say.

    python3 analyze.py --db data/labs/xvenue/xvenue.db [--meta data/labs/xvenue/meta.json] [--json results.json]

Reads the collector's `ticks` (one row per pair per 5-minute cycle: Kalshi yes bid/ask, Polymarket
bid/ask of the outcome token tied to the Kalshi YES) and `pairs`, plus the settlement/fee metadata
from settle.py. Produces results.json, tables.md and figures/*.png:

  gaps        mid-vs-mid gap per pair category (games before kick-off / in play, season futures,
              Fed, text-matched politics & events): median, p90, share above 5c and 10c
  executable  the cross-venue arbitrage after fees: buy YES on one venue, NO on the other;
              Kalshi taker fee 7% x p(1-p), Polymarket per-market feeSchedule (settle.py)
  episodes    how long a net-positive gap lasts (runs of consecutive 5-minute samples), minutes
              per pair-day it exists, and the AR(1) half-life of the mid gap
  leadlag     cross-correlation of 5-minute mid changes at lags -2..+2 and the error-correction
              split: when the venues disagree, which one moves toward the other next
  resolution  for pairs settled inside the window: did both venues settle the same way (a
              mismatch = wrong pair), and whose price one hour before the close was closer to
              the outcome (Brier score, paired by market)
Intervals are pair-level bootstraps (pairs resampled with replacement), because ticks of one
pair are not independent.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
FIG = HERE / "figures"
STEP = 300                     # collector cycle, seconds
CONSEC = 420                   # two samples this close count as consecutive
MAX_SPREAD = 0.10              # a side with a wider spread is treated as not quoted
KALSHI_FEE = 0.07              # taker fee: 0.07 x p x (1-p) per contract (standard schedule)
POLY_DEFAULT_RATE = 0.04       # when settle.py has no schedule for a market
REF_BEFORE = 3600              # reference quote: last valid sample at least this long before the close / kick-off
CATS = ["games_pre", "games_live", "futures", "fed", "text"]
CAT_LABEL = {"games_pre": "games, before kick-off", "games_live": "games, in play", "futures": "season futures & awards", "fed": "Fed decisions", "text": "politics & events (text match)"}
RNG = np.random.default_rng(3)


def boot(fn, pair_stats: pd.DataFrame, n: int = 1000) -> tuple[float, float, float]:
    """fn(DataFrame of per-pair sufficient statistics) -> float; bootstrap over pairs."""
    est = float(fn(pair_stats))
    idx = pair_stats.index.to_numpy(); vals = []
    for _ in range(n):
        s = pair_stats.loc[RNG.choice(idx, len(idx))]
        try:
            vals.append(float(fn(s)))
        except ZeroDivisionError:
            continue
    return est, float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


def load(db: Path, meta: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = sqlite3.connect(db)
    pairs = pd.read_sql("select * from pairs", c)
    t = pd.read_sql("select ts, pair_id, k_bid, k_ask, p_bid, p_ask from ticks", c)
    pairs["cat0"] = np.where(pairs["grp"].str.startswith("game"), "games", np.where(pairs["method"] == "fed", "fed", np.where(pairs["grp"].str.startswith("group"), "futures", "text")))
    epoch = pd.Timestamp(0, tz="UTC")
    gs = pd.to_datetime(pairs["poly_game_start"], utc=True, errors="coerce")
    pairs["game_start_ts"] = ((gs - epoch) / pd.Timedelta(seconds=1)).where(gs.notna(), np.nan)   # unit-safe (pandas may parse to [us])
    kc = pd.to_datetime(pairs["kalshi_close"], utc=True, errors="coerce")
    pairs["close_ts"] = ((kc - epoch) / pd.Timedelta(seconds=1)).where(kc.notna(), np.nan)
    # polymarket taker fee rate per pair
    rate = {}
    for pid, m in meta.get("poly", {}).items():
        sch = m.get("feeSchedule") if m.get("feesEnabled") else None
        rate[pid] = float(sch["rate"]) if sch else 0.0
    pairs["p_rate"] = pairs["poly_id"].map(rate).fillna(POLY_DEFAULT_RATE)
    t = t.merge(pairs[["pair_id", "cat0", "game_start_ts", "p_rate"]], on="pair_id", how="left")
    t["cat"] = t["cat0"]
    g = t["cat0"] == "games"
    t.loc[g, "cat"] = np.where(t.loc[g, "ts"] >= t.loc[g, "game_start_ts"].fillna(np.inf), "games_live", "games_pre")
    t["k_ok"] = (t["k_bid"] > 0) & (t["k_ask"] < 1) & (t["k_ask"] - t["k_bid"] <= MAX_SPREAD)
    t["p_ok"] = (t["p_bid"] > 0) & (t["p_ask"] < 1) & (t["p_ask"] - t["p_bid"] <= MAX_SPREAD)
    t["ok"] = t["k_ok"] & t["p_ok"]
    t["k_mid"] = (t["k_bid"] + t["k_ask"]) / 2
    t["p_mid"] = (t["p_bid"] + t["p_ask"]) / 2
    t["gap_c"] = (t["k_mid"] - t["p_mid"]) * 100
    fk = lambda p: KALSHI_FEE * p * (1 - p)                                      # noqa: E731
    fp = lambda p, r: r * p * (1 - p)                                            # noqa: E731
    # route A: buy YES on Kalshi at k_ask, buy NO on Polymarket at 1 - p_bid; route B: the mirror
    t["netA"] = (t["p_bid"] - t["k_ask"] - fk(t["k_ask"]) - fp(1 - t["p_bid"], t["p_rate"])) * 100
    t["netB"] = (t["k_bid"] - t["p_ask"] - fk(1 - t["k_bid"]) - fp(t["p_ask"], t["p_rate"])) * 100
    t["gross"] = np.maximum(t["p_bid"] - t["k_ask"], t["k_bid"] - t["p_ask"]) * 100
    t["net"] = np.maximum(t["netA"], t["netB"])
    t = t.sort_values(["pair_id", "ts"]).reset_index(drop=True)
    return pairs, t


def gap_stats(t: pd.DataFrame) -> dict:
    out = {}
    for cat in CATS:
        x = t[(t["cat"] == cat) & t["ok"]]
        if len(x) < 100:
            continue
        a = x["gap_c"].abs()
        per_pair = x.groupby("pair_id")["gap_c"].agg(["mean", "size"])
        out[cat] = {"label": CAT_LABEL[cat], "ticks": int(len(x)), "pairs": int(x["pair_id"].nunique()),
                    "abs_gap_median_c": round(float(a.median()), 2), "abs_gap_p75_c": round(float(a.quantile(0.75)), 2), "abs_gap_p90_c": round(float(a.quantile(0.9)), 2),
                    "mean_gap_kalshi_minus_poly_c": round(float(x["gap_c"].mean()), 2), "share_abs_gt5_pct": round(float((a > 5).mean() * 100), 1), "share_abs_gt10_pct": round(float((a > 10).mean() * 100), 1),
                    "pairs_mean_gap_gt5_pct": round(float((per_pair["mean"].abs() > 5).mean() * 100), 1),
                    "kalshi_spread_median_c": round(float(((x["k_ask"] - x["k_bid"]) * 100).median()), 2), "poly_spread_median_c": round(float(((x["p_ask"] - x["p_bid"]) * 100).median()), 2)}
    return out


def executable(t: pd.DataFrame) -> dict:
    out = {}
    for cat in CATS:
        x = t[(t["cat"] == cat) & t["ok"]]
        if len(x) < 100:
            continue
        pos = x[x["net"] > 0]
        out[cat] = {"label": CAT_LABEL[cat], "ticks": int(len(x)), "crossed_gross_pct": round(float((x["gross"] > 0).mean() * 100), 2),
                    "net_positive_pct": round(float((x["net"] > 0).mean() * 100), 2), "net_gt1c_pct": round(float((x["net"] > 1).mean() * 100), 2),
                    "net_when_positive_median_c": round(float(pos["net"].median()), 2) if len(pos) else None, "net_when_positive_p90_c": round(float(pos["net"].quantile(0.9)), 2) if len(pos) else None,
                    "pairs_ever_net_positive_pct": round(float((x.groupby("pair_id")["net"].max() > 0).mean() * 100), 1),
                    "route_A_share_pct": round(float((pos["netA"] >= pos["netB"]).mean() * 100), 1) if len(pos) else None,
                    "fees_round_trip_median_c": round(float(((x["gross"] - x["net"])).median()), 2)}
    return out


def episodes(t: pd.DataFrame) -> tuple[dict, dict]:
    out, surv = {}, {}
    grid = np.array([5, 10, 15, 30, 60, 120, 240])
    for cat in CATS:
        x = t[(t["cat"] == cat) & t["ok"]].copy()
        if len(x) < 100:
            continue
        x["prev_ts"] = x.groupby("pair_id")["ts"].shift()
        x["new"] = (x["ts"] - x["prev_ts"] > CONSEC) | x["prev_ts"].isna()
        pos = x["net"] > 0
        # an episode starts at a positive sample that follows a non-positive or non-consecutive one
        prev_pos = pos.groupby(x["pair_id"]).shift().fillna(False).astype(bool)
        start = pos & (~prev_pos | x["new"])
        ep_id = start.cumsum().where(pos)
        e = x[pos].assign(ep=ep_id[pos]).groupby("ep").agg(pair=("pair_id", "first"), t0=("ts", "min"), t1=("ts", "max"), n=("ts", "size"), net_med=("net", "median"))
        e["min"] = (e["t1"] - e["t0"] + STEP) / 60
        days = x.assign(d=(x["ts"] // 86400)).groupby(["pair_id", "d"]).size().shape[0]
        # AR(1) persistence of the mid gap on consecutive samples
        cons = x[~x["new"]]
        prev_gap = x.groupby("pair_id")["gap_c"].shift()[~x["new"]]
        phi = float(np.corrcoef(cons["gap_c"], prev_gap)[0, 1]) if len(cons) > 100 else np.nan
        hl = STEP / 60 * np.log(0.5) / np.log(phi) if 0 < phi < 1 else np.nan
        out[cat] = {"label": CAT_LABEL[cat], "episodes": int(len(e)), "pair_days": int(days), "minutes_per_pair_day": round(float(e["min"].sum() / max(1, days)), 1),
                    "duration_median_min": round(float(e["min"].median()), 1) if len(e) else None, "duration_p75_min": round(float(e["min"].quantile(0.75)), 1) if len(e) else None,
                    "duration_p90_min": round(float(e["min"].quantile(0.9)), 1) if len(e) else None, "share_single_sample_pct": round(float((e["n"] == 1).mean() * 100), 1) if len(e) else None,
                    "share_ge_15min_pct": round(float((e["min"] >= 15).mean() * 100), 1) if len(e) else None, "share_ge_60min_pct": round(float((e["min"] >= 60).mean() * 100), 1) if len(e) else None,
                    "gap_ar1_phi": round(phi, 4) if phi == phi else None, "gap_half_life_min": round(float(hl), 0) if hl == hl else None}
        surv[cat] = {int(g): round(float((e["min"] >= g).mean()), 4) for g in grid} if len(e) else {}
    return out, surv


def leadlag(t: pd.DataFrame) -> dict:
    out = {}
    for cat in CATS:
        x = t[(t["cat"] == cat) & t["ok"]].copy()
        if len(x) < 1000:
            continue
        g = x.groupby("pair_id")
        x["dts"] = x["ts"] - g["ts"].shift()
        x["dk"] = (x["k_mid"] - g["k_mid"].shift()) * 100
        x["dp"] = (x["p_mid"] - g["p_mid"].shift()) * 100
        ok1 = x["dts"] <= CONSEC
        x.loc[~ok1, ["dk", "dp"]] = np.nan
        g = x.groupby("pair_id")
        x["dk_prev"], x["dp_prev"] = g["dk"].shift(), g["dp"].shift()
        x["dk_next"], x["dp_next"] = g["dk"].shift(-1), g["dp"].shift(-1)
        x["gap_pk"] = (x["p_mid"] - x["k_mid"]) * 100          # positive: Polymarket above Kalshi
        def corr(a, b):
            m = x[a].notna() & x[b].notna(); return float(np.corrcoef(x.loc[m, a], x.loc[m, b])[0, 1]), int(m.sum())
        c0, n0 = corr("dk", "dp"); ck1, _ = corr("dk_prev", "dp"); cp1, _ = corr("dp_prev", "dk")
        ck2, _ = corr("dk_prev", "dp_next"); cp2, _ = corr("dp_prev", "dk_next")   # lag 2 via prev->next
        # error correction: next move of each venue on the current disagreement (per-pair sufficient stats for the bootstrap)
        m = x["dk_next"].notna() & x["dp_next"].notna()
        y = x[m]
        st = y.assign(gx=y["gap_pk"], gxx=y["gap_pk"] ** 2, kxy=y["gap_pk"] * y["dk_next"], pxy=-y["gap_pk"] * y["dp_next"], one=1.0).groupby("pair_id")[["gx", "gxx", "kxy", "pxy", "one"]].sum()
        def beta_k(s): return (s["kxy"].sum() - s["gx"].sum() * 0) / s["gxx"].sum()        # intercept-free: gaps are centred by construction (both signs)
        def beta_p(s): return s["pxy"].sum() / s["gxx"].sum()
        def share_k(s): return beta_k(s) / (beta_k(s) + beta_p(s))
        bk, bp, sk = boot(beta_k, st), boot(beta_p, st), boot(share_k, st)
        # pair-level bootstrap for the lead-lag correlations too (sums of products per pair)
        def cstats(a, b):
            mm = x[a].notna() & x[b].notna(); z = x[mm]
            return z.assign(ab=z[a] * z[b], aa=z[a] ** 2, bb=z[b] ** 2, a=z[a], b=z[b], n=1.0).groupby("pair_id")[["ab", "aa", "bb", "a", "b", "n"]].sum()
        def corr_of(s):
            n = s["n"].sum(); ma, mb = s["a"].sum() / n, s["b"].sum() / n
            return (s["ab"].sum() / n - ma * mb) / np.sqrt((s["aa"].sum() / n - ma**2) * (s["bb"].sum() / n - mb**2))
        ck1b, cp1b = boot(corr_of, cstats("dk_prev", "dp")), boot(corr_of, cstats("dp_prev", "dk"))
        out[cat] = {"label": CAT_LABEL[cat], "samples": n0, "pairs": int(x["pair_id"].nunique()),
                    "corr_same_5min": round(c0, 4), "corr_kalshi_leads_1": {"est": round(ck1, 4), "lo": round(ck1b[1], 4), "hi": round(ck1b[2], 4)},
                    "corr_poly_leads_1": {"est": round(cp1, 4), "lo": round(cp1b[1], 4), "hi": round(cp1b[2], 4)},
                    "corr_kalshi_leads_2": round(ck2, 4), "corr_poly_leads_2": round(cp2, 4),
                    "ec_beta_kalshi_toward_poly": {"est": round(bk[0], 4), "lo": round(bk[1], 4), "hi": round(bk[2], 4)},
                    "ec_beta_poly_toward_kalshi": {"est": round(bp[0], 4), "lo": round(bp[1], 4), "hi": round(bp[2], 4)},
                    "ec_share_adjustment_by_kalshi_pct": {"est": round(sk[0] * 100, 1), "lo": round(sk[1] * 100, 1), "hi": round(sk[2] * 100, 1)},
                    "share_samples_kalshi_moved_pct": round(float((x["dk"].abs() > 0).mean() * 100), 1), "share_samples_poly_moved_pct": round(float((x["dp"].abs() > 0).mean() * 100), 1)}
    return out


def resolution(pairs: pd.DataFrame, t: pd.DataFrame, meta: dict) -> tuple[dict, pd.DataFrame]:
    rows = []
    for p in pairs.itertuples():
        km = meta["kalshi"].get(p.kalshi_ticker, {}); pm = meta["poly"].get(p.poly_id, {})
        if km.get("status") != "finalized" or km.get("result") not in ("yes", "no"):
            continue
        k_out = 1.0 if km["result"] == "yes" else 0.0
        p_out = None
        if pm.get("closed") and pm.get("clobTokenIds") and pm.get("outcomePrices"):
            try:
                i = pm["clobTokenIds"].index(str(p.poly_token)); px = float(pm["outcomePrices"][i]); p_out = px if px in (0.0, 1.0) else None
            except (ValueError, IndexError):
                p_out = None
        rows.append({"pair_id": p.pair_id, "cat0": p.cat0, "k_out": k_out, "p_out": p_out, "ref_ts": (p.game_start_ts if p.cat0 == "games" and p.game_start_ts == p.game_start_ts else p.close_ts)})
    r = pd.DataFrame(rows)
    out = {"kalshi_finalized_pairs": int(len(r)), "both_settled_pairs": int(r["p_out"].notna().sum())}
    both = r[r["p_out"].notna()].copy()
    both["match"] = both["k_out"] == both["p_out"]
    out["settlement_mismatch_pairs"] = int((~both["match"]).sum())
    out["settlement_mismatch_pct"] = round(float((~both["match"]).mean() * 100), 2) if len(both) else None
    out["mismatch_by_cat"] = {c: {"pairs": int(len(g)), "mismatch": int((~g["match"]).sum())} for c, g in both.groupby("cat0")}
    good = both[both["match"]]
    # reference quote: last valid sample at least REF_BEFORE before the reference time
    tv = t[t["ok"]][["pair_id", "ts", "k_mid", "p_mid"]]
    ref = good.merge(tv, on="pair_id", how="inner")
    ref = ref[ref["ts"] <= ref["ref_ts"] - REF_BEFORE].sort_values("ts").groupby("pair_id").tail(1)
    ref["bk"] = (ref["k_mid"] - ref["k_out"]) ** 2; ref["bp"] = (ref["p_mid"] - ref["k_out"]) ** 2
    ref["diff"] = ref["bk"] - ref["bp"]                                  # positive: Polymarket closer
    ref["dis"] = (ref["k_mid"] - ref["p_mid"]).abs() * 100
    out["pairs_with_reference_quote"] = int(len(ref))
    def brier_block(z: pd.DataFrame) -> dict:
        st = z[["bk", "bp", "diff"]].copy(); st["n"] = 1.0; st.index = z["pair_id"]
        d = boot(lambda s: s["diff"].sum() / s["n"].sum(), st, 600)
        dis = z[z["dis"] > 5]
        closer = boot(lambda s: s["pc"].sum() / s["n"].sum(), dis.assign(pc=(dis["bp"] < dis["bk"]).astype(float), n=1.0).set_index("pair_id")[["pc", "n"]], 600) if len(dis) > 20 else (np.nan,) * 3
        return {"pairs": int(len(z)), "brier_kalshi": round(float(z["bk"].mean()), 4), "brier_poly": round(float(z["bp"].mean()), 4),
                "brier_diff_kalshi_minus_poly": {"est": round(d[0], 4), "lo": round(d[1], 4), "hi": round(d[2], 4)},
                "disagreements_gt5c": int(len(dis)), "poly_closer_when_disagree_pct": {"est": round(closer[0] * 100, 1), "lo": round(closer[1] * 100, 1), "hi": round(closer[2] * 100, 1)} if len(dis) > 20 else None,
                "mean_abs_disagreement_c": round(float(z["dis"].mean()), 2)}
    out["calibration_all"] = brier_block(ref) if len(ref) > 20 else None
    out["calibration_by_cat"] = {c: brier_block(g) for c, g in ref.groupby("cat0") if len(g) > 20}
    return out, ref


def figures(t: pd.DataFrame, surv: dict, ll: dict, res: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIG.mkdir(exist_ok=True)
    cols = {"games_pre": "#2a9d8f", "games_live": "#8ab17d", "futures": "#e9c46a", "fed": "#f4a261", "text": "#e76f51"}
    fig, ax = plt.subplots(figsize=(9, 4.4))
    for cat in CATS:
        x = t[(t["cat"] == cat) & t["ok"]]["gap_c"].abs().to_numpy()
        if len(x) < 100:
            continue
        xs = np.sort(x); ax.plot(xs, np.arange(1, len(xs) + 1) / len(xs), label=f"{CAT_LABEL[cat]} ({len(xs):,})", color=cols[cat])
    ax.set_xlim(0, 20); ax.set_xlabel("|Kalshi mid − Polymarket mid|, cents"); ax.set_ylabel("share of samples below"); ax.set_title("How far apart the two venues quote the same outcome")
    ax.grid(alpha=0.3); ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(FIG / "gap_cdf.png", dpi=160); plt.close(fig)
    fig, ax = plt.subplots(figsize=(9, 4.4))
    for cat, s in surv.items():
        if s:
            ax.plot(list(s.keys()), [v * 100 for v in s.values()], marker="o", label=CAT_LABEL[cat], color=cols[cat])
    ax.set_xscale("log"); ax.set_xlabel("minutes since the gap first cleared both fees"); ax.set_ylabel("% of episodes still open"); ax.set_title("How long a tradeable gap survives")
    ax.grid(alpha=0.3, which="both"); ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(FIG / "gap_survival.png", dpi=160); plt.close(fig)
    fig, ax = plt.subplots(figsize=(9, 4.4))
    cats = [c for c in CATS if c in ll]; xx = np.arange(len(cats))
    ax.bar(xx - 0.2, [ll[c]["corr_kalshi_leads_1"]["est"] for c in cats], 0.4, label="corr(ΔKalshi t−1, ΔPoly t): Kalshi leads", color="#264653")
    ax.bar(xx + 0.2, [ll[c]["corr_poly_leads_1"]["est"] for c in cats], 0.4, label="corr(ΔPoly t−1, ΔKalshi t): Polymarket leads", color="#e76f51")
    for i, c in enumerate(cats):
        sh = ll[c]["ec_share_adjustment_by_kalshi_pct"]
        lab = f"Kalshi closes {sh['est']:.0f}% of a disagreement" if sh["hi"] - sh["lo"] < 60 else "closing split: not measurable"
        ax.text(i, max(0, ll[c]["corr_kalshi_leads_1"]["est"], ll[c]["corr_poly_leads_1"]["est"]) + 0.006, lab, ha="center", fontsize=7.5)
    ax.set_xticks(xx); ax.set_xticklabels([CAT_LABEL[c] for c in cats], fontsize=8); ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("correlation of 5-minute mid changes"); ax.set_title("Who moves first"); ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(FIG / "lead_lag.png", dpi=160); plt.close(fig)
    cb = res.get("calibration_by_cat", {})
    if cb:
        fig, ax = plt.subplots(figsize=(9, 4.4)); cats = list(cb.keys()); xx = np.arange(len(cats))
        ax.bar(xx - 0.2, [cb[c]["brier_kalshi"] for c in cats], 0.4, label="Kalshi", color="#264653"); ax.bar(xx + 0.2, [cb[c]["brier_poly"] for c in cats], 0.4, label="Polymarket", color="#e76f51")
        nice = {"games": "games", "futures": "season futures & awards", "fed": "Fed decisions", "text": "politics & events (text match)"}
        ax.set_xticks(xx); ax.set_xticklabels([f"{nice.get(c, c)} ({cb[c]['pairs']})" for c in cats]); ax.set_ylabel("Brier score of the quote 1 h before close (lower = better)")
        ax.set_title("Whose price was closer to the outcome"); ax.legend(); fig.tight_layout(); fig.savefig(FIG / "brier.png", dpi=160); plt.close(fig)


def tables(out: dict) -> None:
    md = [f"# Lab 3 tables ({out['coverage']['from']} to {out['coverage']['to']}, {out['coverage']['days']} days)", ""]
    md += ["## Gap, mid vs mid (both venues quoted, spread <= 10c)", "", "| Category | Pairs | Samples | Median abs gap | p90 | > 5c | > 10c | Mean Kalshi - Poly |", "|---|---|---|---|---|---|---|---|"]
    for c, v in out["gaps"].items():
        md.append(f"| {v['label']} | {v['pairs']:,} | {v['ticks']:,} | {v['abs_gap_median_c']}c | {v['abs_gap_p90_c']}c | {v['share_abs_gt5_pct']}% | {v['share_abs_gt10_pct']}% | {v['mean_gap_kalshi_minus_poly_c']:+}c |")
    md += ["", "## Executable gap after fees", "", "| Category | Books crossed | Net > 0 | Net > 1c | Median net when > 0 | p90 | Pairs ever net > 0 | Round-trip fees (median) |", "|---|---|---|---|---|---|---|---|"]
    for c, v in out["executable"].items():
        md.append(f"| {v['label']} | {v['crossed_gross_pct']}% | {v['net_positive_pct']}% | {v['net_gt1c_pct']}% | {v['net_when_positive_median_c']}c | {v['net_when_positive_p90_c']}c | {v['pairs_ever_net_positive_pct']}% | {v['fees_round_trip_median_c']}c |")
    md += ["", "## Episodes of a net-positive gap", "", "| Category | Episodes | Minutes per pair-day | Median duration | p75 | >= 15 min | >= 60 min | Mid-gap half-life |", "|---|---|---|---|---|---|---|---|"]
    for c, v in out["episodes"].items():
        md.append(f"| {v['label']} | {v['episodes']:,} | {v['minutes_per_pair_day']} | {v['duration_median_min']} min | {v['duration_p75_min']} min | {v['share_ge_15min_pct']}% | {v['share_ge_60min_pct']}% | {v['gap_half_life_min']} min |")
    md += ["", "## Lead-lag of 5-minute mid changes (95% pair bootstrap)", "", "| Category | Samples | Same interval | Kalshi leads (lag 1) | Polymarket leads (lag 1) | Share of a disagreement closed by Kalshi |", "|---|---|---|---|---|---|"]
    for c, v in out["leadlag"].items():
        k, pl, sh = v["corr_kalshi_leads_1"], v["corr_poly_leads_1"], v["ec_share_adjustment_by_kalshi_pct"]
        md.append(f"| {v['label']} | {v['samples']:,} | {v['corr_same_5min']} | {k['est']} [{k['lo']}; {k['hi']}] | {pl['est']} [{pl['lo']}; {pl['hi']}] | {sh['est']}% [{sh['lo']}; {sh['hi']}] |")
    r = out["resolution"]
    md += ["", "## Settlement and calibration", "", f"Settled on both venues: {r['both_settled_pairs']:,}; settled differently (wrong pair): {r['settlement_mismatch_pairs']} ({r['settlement_mismatch_pct']}%).", ""]
    md += ["| Category | Pairs | Brier Kalshi | Brier Polymarket | Difference (K - P) | Disagreements > 5c at the reference quote |", "|---|---|---|---|---|---|"]
    blocks = {"all": r["calibration_all"], **r["calibration_by_cat"]}
    for c, v in blocks.items():
        if v:
            d = v["brier_diff_kalshi_minus_poly"]
            md.append(f"| {c} | {v['pairs']:,} | {v['brier_kalshi']} | {v['brier_poly']} | {d['est']:+} [{d['lo']}; {d['hi']}] | {v['disagreements_gt5c']} |")
    (HERE / "tables.md").write_text("\n".join(md) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--meta", default=None)
    ap.add_argument("--json", default=str(HERE / "results.json"))
    a = ap.parse_args()
    db = Path(a.db); meta = json.loads(Path(a.meta or db.parent / "meta.json").read_text())
    pairs, t = load(db, meta)
    print(f"ticks {len(t):,} pairs {t['pair_id'].nunique():,}", file=sys.stderr)
    cov = {"from": pd.to_datetime(t["ts"].min(), unit="s").strftime("%Y-%m-%d"), "to": pd.to_datetime(t["ts"].max(), unit="s").strftime("%Y-%m-%d"),
           "days": round((t["ts"].max() - t["ts"].min()) / 86400, 1), "cycles": int(t["ts"].nunique()), "ticks": int(len(t)), "pairs": int(t["pair_id"].nunique()),
           "ticks_both_quoted_pct": round(float(t["ok"].mean() * 100), 1), "ticks_kalshi_unquoted_pct": round(float((~t["k_ok"]).mean() * 100), 1), "ticks_poly_unquoted_pct": round(float((~t["p_ok"]).mean() * 100), 1),
           "by_cat": {c: {"ticks": int((t["cat"] == c).sum()), "pairs": int(t.loc[t["cat"] == c, "pair_id"].nunique()), "both_quoted_pct": round(float(t.loc[t["cat"] == c, "ok"].mean() * 100), 1)} for c in CATS}}
    gaps = gap_stats(t); ex = executable(t); ep, surv = episodes(t); ll = leadlag(t); res, ref = resolution(pairs, t, meta)
    out = {"coverage": cov, "gaps": gaps, "executable": ex, "episodes": ep, "survival": surv, "leadlag": ll, "resolution": res,
           "assumptions": {"quoted": f"both sides quoted with spread <= {int(MAX_SPREAD*100)}c; Kalshi empty side = bid 0 or ask 1",
                           "fees": f"Kalshi taker {KALSHI_FEE} x p(1-p) per contract (cent rounding ignored); Polymarket per-market feeSchedule rate x p(1-p), {POLY_DEFAULT_RATE} when unknown; makers pay nothing on either venue",
                           "arbitrage": "route A: buy YES on Kalshi at the ask, buy NO on Polymarket at 1 - bid; route B: the mirror; net = payout 1 - both legs - both fees",
                           "episodes": f"consecutive samples <= {CONSEC}s apart; duration = span + {STEP}s", "reference_quote": f"last valid sample >= {REF_BEFORE}s before kick-off (games) or the Kalshi close (others)",
                           "intervals": "95% pair-level bootstrap"}}
    Path(a.json).write_text(json.dumps(out, indent=1))
    figures(t, surv, ll, res)
    tables(out)
    print(json.dumps({k: out[k] for k in ("coverage", "gaps", "executable", "episodes", "leadlag", "resolution")}, indent=1))


if __name__ == "__main__":
    main()
