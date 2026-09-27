"""Lab 2, shadow run: what the markout cannot see. Fills are not round trips; the net inventory
that stays on the book after each fill is paid out at resolution, not at t+15 min.

    python3 shadow_inventory.py --db data/labs/maker/shadow.db [--json results_shadow_inventory.json]

For every market in the shadow DB the script asks the CLOB (GET /markets/{condition_id}) whether
it has resolved and which token won, then values every virtual fill at its true payout (1 or 0)
instead of the mid 15 minutes later. Markets still open are valued at the last mid of the snapshot.
The gap between "at payout" and "15-min markout" on the same fills is the inventory P&L the
markout misses. Resolution answers are cached next to the DB (clob_status.json) so re-runs are free.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shadow_report import load, markouts  # noqa: E402

CLOB = "https://clob.polymarket.com/markets/"


def clob_status(cids: list[str], cache: Path) -> dict[str, dict]:
    st = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [c for c in cids if c not in st or not st[c].get("closed")]     # re-ask open ones only
    s = requests.Session()
    for i, cid in enumerate(todo):
        try:
            m = s.get(CLOB + cid, timeout=20).json()
        except Exception as e:                                             # noqa: BLE001
            print(f"  {cid[:10]} error {e}", file=sys.stderr)
            continue
        toks = m.get("tokens") or []
        yes = next((t for t in toks if t.get("outcome") == "Yes"), None)
        winner = next((t["outcome"] for t in toks if t.get("winner")), None)
        st[cid] = {"closed": bool(m.get("closed")), "accepting_orders": bool(m.get("accepting_orders")),
                   "winner": winner, "yes_price": (yes or {}).get("price"), "end_date_iso": m.get("end_date_iso"),
                   "question": m.get("question"), "checked": int(time.time())}
        if i % 50 == 49:
            print(f"  clob {i + 1}/{len(todo)}", file=sys.stderr)
        time.sleep(0.05)
    cache.write_text(json.dumps(st, indent=1))
    return st


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--json", default=str(Path(__file__).resolve().parent / "results_shadow_inventory.json"))
    a = ap.parse_args()
    db = Path(a.db)
    q, f, m = load(db)
    q = q.sort_values("ts")
    days = (q["ts"].max() - q["ts"].min()) / 86400
    st = clob_status(m["condition_id"].tolist(), db.parent / "clob_status.json")
    f = markouts(q, f)
    f["sign"] = np.where(f["side"] == "buy_yes", 1.0, -1.0)
    last_mid = q.groupby("condition_id")["mid"].last()
    f["last_mid"] = f["condition_id"].map(last_mid)
    rows = []
    for cid, g in f.groupby("condition_id"):
        s = st.get(cid, {})
        resolved = s.get("closed") and s.get("winner") in ("Yes", "No")
        payout = 1.0 if s.get("winner") == "Yes" else 0.0
        mark = payout if resolved else float(g["last_mid"].iloc[0])
        qty = g["our_pro"].to_numpy()
        sgn, ex = g["sign"].to_numpy(), g["exec"].to_numpy()
        net = float((sgn * qty).sum())
        gross = float(qty.sum())
        pnl_mark = float((sgn * (mark - ex) * qty).sum())                  # every fill at payout / last mid
        pnl_15 = float(np.nansum(sgn * (g["mid_15"].to_numpy() - ex) * qty))
        # split: the matched part is what buys and sells net out to; the rest rides to the mark
        avg_buy = float(np.average(ex[sgn > 0], weights=qty[sgn > 0])) if (sgn > 0).any() else np.nan
        avg_sell = float(np.average(ex[sgn < 0], weights=qty[sgn < 0])) if (sgn < 0).any() else np.nan
        matched = float(min(qty[sgn > 0].sum(), qty[sgn < 0].sum()))
        pnl_matched = matched * (avg_sell - avg_buy) if matched else 0.0
        rows.append({"condition_id": cid, "question": (s.get("question") or "")[:80], "resolved": bool(resolved), "winner": s.get("winner"),
                     "mark": round(mark, 3), "fills": int(len(g)), "shares": round(gross), "net_shares": round(net),
                     "avg_buy": None if np.isnan(avg_buy) else round(avg_buy, 4), "avg_sell": None if np.isnan(avg_sell) else round(avg_sell, 4),
                     "pnl_matched_usd": round(pnl_matched, 2), "pnl_inventory_usd": round(pnl_mark - pnl_matched, 2),
                     "pnl_at_mark_usd": round(pnl_mark, 2), "pnl_markout15_usd": round(pnl_15, 2), "gap_usd": round(pnl_mark - pnl_15, 2)})
    t = pd.DataFrame(rows)
    res_t = t[t["resolved"]]
    open_t = t[~t["resolved"]]

    def agg(x: pd.DataFrame) -> dict:
        return {"markets": int(len(x)), "fills": int(x["fills"].sum()), "shares": int(x["shares"].sum()), "net_shares_abs": int(x["net_shares"].abs().sum()),
                "pnl_at_mark_usd": round(float(x["pnl_at_mark_usd"].sum()), 2), "pnl_matched_usd": round(float(x["pnl_matched_usd"].sum()), 2),
                "pnl_inventory_usd": round(float(x["pnl_inventory_usd"].sum()), 2), "pnl_markout15_usd": round(float(x["pnl_markout15_usd"].sum()), 2),
                "gap_usd": round(float(x["gap_usd"].sum()), 2), "markets_pnl_negative": int((x["pnl_at_mark_usd"] < 0).sum())}

    out = {"snapshot_days": round(days, 2), "clob_checked": len(st),
           "resolved": agg(res_t), "open_at_last_mid": agg(open_t), "all": agg(t),
           "per_day_usd": {"markout15_all": round(float(t["pnl_markout15_usd"].sum()) / days, 2), "at_mark_all": round(float(t["pnl_at_mark_usd"].sum()) / days, 2)},
           "resolved_markets": res_t.sort_values("pnl_at_mark_usd").to_dict("records"),
           "worst_open": open_t.sort_values("pnl_at_mark_usd").head(10).to_dict("records"),
           "best_open": open_t.sort_values("pnl_at_mark_usd", ascending=False).head(5).to_dict("records"),
           "method": {"exec": "our touch quote from the last sample before the taker trade",
                      "mark": "payout 1/0 from CLOB winner for resolved markets, last mid of the snapshot otherwise",
                      "matched": "min(bought, sold) shares at size-weighted average prices; inventory = the rest, valued at the mark",
                      "caveat": "markets still open are marked at a mid, which is not a payout; re-run when more have resolved"}}
    Path(a.json).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("snapshot_days", "resolved", "open_at_last_mid", "all", "per_day_usd")}, indent=1))
    print("\nresolved markets:")
    print(res_t[["question", "winner", "fills", "shares", "net_shares", "avg_buy", "avg_sell", "pnl_matched_usd", "pnl_inventory_usd", "pnl_at_mark_usd", "pnl_markout15_usd"]].to_string(index=False))


if __name__ == "__main__":
    main()
