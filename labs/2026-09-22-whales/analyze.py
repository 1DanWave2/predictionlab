"""Lab 4 (whales), stage 2: who topped the week, how they did it, and why it is not copyable.

    python3 analyze.py [--date YYYY-MM-DD]     # -> results.json, tables.md, README auto blocks

For the #1 wallet by weekly PnL: the position that made the week, the biggest loss, hit rate,
concentration, trade sizes, and whether the wallet earns maker rebates (a market maker, not a
bettor). For the top 25: how much of the week's PnL is one position, how many are makers, and
how many of last week's top 10 are still there (needs two snapshots).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
OUT = Path(os.environ.get("WHALES_DATA_DIR") or ROOT / "data" / "labs" / "whales")
WEEK = 7 * 86400


def category(slug: str | None) -> str:
    s = (slug or "").lower()
    if re.match(r"^(nfl|nba|mlb|nhl|epl|lal|bun|sea|fl1|cfb|mls|ucl|wnba|ncaa|atp|wta|ufc|f1|nascar|golf|boxing|cs2|lol|dota2|valorant)-", s):
        return "sports & esports"
    if any(k in s for k in ("bitcoin", "btc", "eth", "solana", "crypto", "xrp", "doge")):
        return "crypto"
    if any(k in s for k in ("election", "president", "senate", "nominee", "governor", "trump", "fed-", "rate-cut", "mayor", "parliament")):
        return "politics & macro"
    return "other"


def money(v: float) -> str:
    v = float(v)
    return f"−${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def render_readme(res: dict) -> None:
    """Rewrite the `<!-- auto:week -->` block of README.md with this snapshot's numbers."""
    w, g = res["whale"], res["top25"]
    b, x = w["best"], w["worst"]
    lines = [f"**Snapshot {res['snapshot']}** · leaderboard window: 7 days · wallets pulled in full: top {g['n']} by weekly PnL", "",
             f"**#{w['rank']} {w['name']}** — {money(w['pnl_week'])} on the weekly leaderboard, {money(w['vol_week'])} volume.", "",
             f"- The week in trades: {w['trades_week']} fills in {w['markets_week']} market(s) over {w['active_hours']} hours; "
             f"{w['big_trades']} fills of $10k+ carry {w['big_trades_share_pct']}% of the money; largest single fill {money(w['max_trade_usd'])}; "
             f"median fill {money(w['median_trade_usd'])}.",
             (f"- The position that made the week: **{b['outcome']}** on “{b['title']}” — {money(b['total_bought'])} at {b['avg_price'] * 100:.1f}¢ → "
              f"{money(b['realized_pnl'])} ({b['share_of_week_pnl']}% of the week's closed PnL)." if b else "- No closed position inside the week."),
             (f"- Biggest loss in the window: **{x['outcome']}** on “{x['title']}” — {money(x['total_bought'])} at {x['avg_price'] * 100:.1f}¢ → {money(x['realized_pnl'])}." if x and x["realized_pnl"] < 0 else "- No losing position inside the week."),
             f"- 28 days, everything counted: {w['wins_28d']} wins ({money(w['wins_usd_28d'])}) and {w['hidden_losses_28d']} resolved losers still sitting unredeemed "
             f"({money(w['hidden_losses_usd_28d'])}) → net {money(w['net_28d'])}, hit rate {w['hit_rate_28d']}%. The public closed-positions list shows only the wins.",
             f"- Maker or taker: {'maker' if w['is_maker'] else 'taker'} (maker rebates {money(w['maker_rebates_usd'])} vs taker rebates {money(w['taker_rebates_usd'])} in the window).", "",
             f"**Top {g['n']} together**: {money(g['pnl_total'])} weekly PnL, the #1 wallet is {g['top1_share']}% of it. "
             f"{g['one_position_over_half']} of {g['n']} made more than half their week on one position (median: the best position is {g['median_best_share']}% of the week). "
             f"{g['makers']} of {g['n']} earn more maker than taker rebates. {g['wallets_with_hidden_losses']} of {g['n']} carry resolved-but-unredeemed losses "
             f"({g['hidden_losses_28d']} positions, {money(g['hidden_losses_usd_28d'])} over 28 days) that the closed-positions list does not show. "
             f"Categories by notional: {', '.join(f'{k or 'unknown'} {v}' for k, v in g['cats'].items())}.",
             (f"- Week-over-week: {res['churn']['stayed_in_top10']} of last snapshot's top 10 are still in the top 10." if res.get("churn") else "- Week-over-week churn: needs a second snapshot.")]
    body = "\n".join(lines)
    rp = HERE / "README.md"
    if rp.exists():
        s = rp.read_text()
        start, end = "<!-- auto:week -->", "<!-- /auto:week -->"
        if start in s and end in s:
            s = s[: s.index(start) + len(start)] + "\n" + body + "\n" + s[s.index(end):]
            rp.write_text(s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    a = ap.parse_args()
    day = OUT / a.date
    lb = pd.read_parquet(day / "leaderboard.parquet")
    act = pd.read_parquet(day / "activity.parquet")
    cls = pd.read_parquet(day / "closed.parquet")
    pos = pd.read_parquet(day / "positions.parquet")
    hist = pd.read_parquet(OUT / "leaderboard_history.parquet")
    now = int(time.time())
    since = now - WEEK

    # Resolved losers are never "closed": a position that went to 0 stays in /positions as redeemable-for-nothing,
    # and /closed-positions lists only exits and winning redemptions. Fold them in as losses, dated by end_date.
    lost = pos[(pos["cur_price"] == 0) & (pos["current_value"] == 0) & (pos["initial_value"] > 0) & pos["redeemable"]].copy()
    lost["ts"] = pd.to_datetime(lost["end_date"], errors="coerce", utc=True).map(lambda d: int(d.timestamp()) + 86400 if pd.notna(d) else 0)
    lost["realized_pnl"] = -lost["initial_value"]
    lost["total_bought"] = lost["initial_value"]
    lost["hidden_loss"] = True
    cls = pd.concat([cls.assign(hidden_loss=False), lost[cls.columns.tolist() + ["hidden_loss"]]], ignore_index=True)

    week = lb[(lb["period"] == "WEEK") & (lb["order"] == "PNL")].sort_values("rank")
    top25 = week.head(25)
    w = top25.iloc[0]
    wid = w["wallet"]
    a_w = act[act["wallet"] == wid]
    c_w = cls[(cls["wallet"] == wid)]
    c_week = c_w[c_w["ts"] >= since]
    trades = a_w[a_w["type"] == "TRADE"]
    maker_reb = a_w[a_w["type"] == "MAKER_REBATE"]["usdc"].sum()
    taker_reb = a_w[a_w["type"] == "TAKER_REBATE"]["usdc"].sum()
    best = c_week.sort_values("realized_pnl", ascending=False).iloc[0] if len(c_week) else None
    worst = c_week.sort_values("realized_pnl").iloc[0] if len(c_week) else None
    week_pnl_closed = float(c_week["realized_pnl"].sum()) if len(c_week) else 0.0
    wins = int((c_week["realized_pnl"] > 0).sum()) if len(c_week) else 0
    n_closed = int(len(c_week))
    c_28 = c_w[c_w["ts"] >= now - 4 * WEEK]
    open_w = pos[pos["wallet"] == wid]

    def side_text(row) -> str:
        return f"{row['outcome']} on “{row['title']}”"

    hidden = c_28[c_28["hidden_loss"]]
    whale = {
        "hidden_losses_28d": int(len(hidden)), "hidden_losses_usd_28d": round(float(hidden["realized_pnl"].sum())),
        "wins_28d": int((c_28["realized_pnl"] > 0).sum()), "wins_usd_28d": round(float(c_28.loc[c_28["realized_pnl"] > 0, "realized_pnl"].sum())),
        "net_28d": round(float(c_28["realized_pnl"].sum())),
        "big_trades": int((trades["usdc"] >= 10000).sum()), "big_trades_share_pct": round(float(trades.loc[trades["usdc"] >= 10000, "usdc"].sum() / trades["usdc"].sum() * 100)) if len(trades) else 0,
        "active_hours": round((trades["ts"].max() - trades["ts"].min()) / 3600, 1) if len(trades) else 0,
        "rank": int(w["rank"]), "name": w["name"] or "(no public name)", "wallet": wid, "pnl_week": round(float(w["pnl"])), "vol_week": round(float(w["vol"])),
        "trades_week": int(len(trades)), "notional_week": round(float(trades["usdc"].sum())), "markets_week": int(trades["condition_id"].nunique()),
        "median_trade_usd": round(float(trades["usdc"].median())) if len(trades) else 0, "max_trade_usd": round(float(trades["usdc"].max())) if len(trades) else 0,
        "maker_rebates_usd": round(float(maker_reb), 2), "taker_rebates_usd": round(float(taker_reb), 2), "is_maker": bool(maker_reb > taker_reb),
        "closed_week": n_closed, "wins_week": wins, "hit_rate_week": round(wins / n_closed * 100, 1) if n_closed else None,
        "closed_28d": int(len(c_28)), "hit_rate_28d": round(float((c_28["realized_pnl"] > 0).mean() * 100), 1) if len(c_28) else None,
        "pnl_closed_week": round(week_pnl_closed),
        "best": None if best is None else {"title": best["title"], "outcome": best["outcome"], "avg_price": round(float(best["avg_price"]), 4), "total_bought": round(float(best["total_bought"])),
                                          "realized_pnl": round(float(best["realized_pnl"])), "slug": best["slug"], "event_slug": best["event_slug"], "ts": int(best["ts"]),
                                          "share_of_week_pnl": round(float(best["realized_pnl"]) / week_pnl_closed * 100, 1) if week_pnl_closed > 0 else None},
        "worst": None if worst is None else {"title": worst["title"], "outcome": worst["outcome"], "avg_price": round(float(worst["avg_price"]), 4), "total_bought": round(float(worst["total_bought"])),
                                            "realized_pnl": round(float(worst["realized_pnl"])), "slug": worst["slug"]},
        "open_positions": int(len(open_w)), "open_value": round(float(open_w["current_value"].sum())) if len(open_w) else 0,
        "categories_week": trades.assign(cat=trades["slug"].map(category)).groupby("cat")["usdc"].sum().round().sort_values(ascending=False).to_dict() if len(trades) else {},
    }

    # the top 25 as a group
    rows = []
    for _, r in top25.iterrows():
        aw = act[act["wallet"] == r["wallet"]]
        cw = cls[(cls["wallet"] == r["wallet"]) & (cls["ts"] >= since)]
        tr = aw[aw["type"] == "TRADE"]
        mk = aw[aw["type"] == "MAKER_REBATE"]["usdc"].sum(); tk = aw[aw["type"] == "TAKER_REBATE"]["usdc"].sum()
        bestp = float(cw["realized_pnl"].max()) if len(cw) else 0.0
        rows.append({"rank": int(r["rank"]), "name": r["name"] or "(no public name)", "pnl": round(float(r["pnl"])), "vol": round(float(r["vol"])),
                     "trades": int(len(tr)), "median_trade": round(float(tr["usdc"].median())) if len(tr) else 0, "closed": int(len(cw)),
                     "hit_rate": round(float((cw["realized_pnl"] > 0).mean() * 100)) if len(cw) else None,
                     "best_share": round(bestp / float(r["pnl"]) * 100) if float(r["pnl"]) > 0 and len(cw) else None,
                     "maker": bool(mk > tk and mk > 0), "cat": (tr.assign(cat=tr["slug"].map(category)).groupby("cat")["usdc"].sum().idxmax() if len(tr) else "")})
    t25 = pd.DataFrame(rows)
    hid25 = cls[cls["hidden_loss"] & cls["wallet"].isin(top25["wallet"]) & (cls["ts"] >= now - 4 * WEEK)]
    group = {"n": int(len(t25)), "pnl_total": int(t25["pnl"].sum()), "top1_share": round(float(t25["pnl"].iloc[0] / t25["pnl"].sum() * 100), 1),
             "hidden_losses_28d": int(len(hid25)), "hidden_losses_usd_28d": round(float(hid25["realized_pnl"].sum())),
             "wallets_with_hidden_losses": int(hid25["wallet"].nunique()),
             "makers": int(t25["maker"].sum()), "median_best_share": float(t25["best_share"].median()) if t25["best_share"].notna().any() else None,
             "one_position_over_half": int((t25["best_share"] >= 50).sum()), "median_hit_rate": float(t25["hit_rate"].median()) if t25["hit_rate"].notna().any() else None,
             "zero_volume": int((t25["vol"] == 0).sum()), "cats": t25["cat"].value_counts().to_dict()}

    # churn: this week's top 10 vs the previous snapshot's top 10
    snaps = sorted(hist["snapshot"].unique())
    churn = None
    if len(snaps) >= 2:
        prev = hist[(hist["snapshot"] == snaps[-2]) & (hist["period"] == "WEEK") & (hist["order"] == "PNL")].sort_values("rank").head(10)
        cur = hist[(hist["snapshot"] == snaps[-1]) & (hist["period"] == "WEEK") & (hist["order"] == "PNL")].sort_values("rank").head(10)
        stay = len(set(prev["wallet"]) & set(cur["wallet"]))
        churn = {"prev_snapshot": snaps[-2], "stayed_in_top10": stay, "left_top10": 10 - stay}
    month = lb[(lb["period"] == "MONTH") & (lb["order"] == "PNL")].sort_values("rank").head(10)
    in_month_top10 = int(wid in set(month["wallet"]))

    res = {"snapshot": a.date, "window_days": 7, "whale": whale, "top25": group, "churn": churn, "whale_in_month_top10": in_month_top10,
           "top25_rows": t25.to_dict(orient="records")}
    (HERE / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
    render_readme(res)
    tbl = t25[["rank", "name", "pnl", "vol", "trades", "median_trade", "closed", "hit_rate", "best_share", "maker", "cat"]].copy()
    tbl["name"] = tbl["name"].str[:22]
    (HERE / "tables.md").write_text("# Top 25 by weekly PnL — " + a.date + "\n\n" + tbl.to_markdown(index=False) + "\n")
    print(json.dumps({k: v for k, v in res.items() if k != "top25_rows"}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
