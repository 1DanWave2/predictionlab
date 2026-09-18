"""SM weather wallet mirror backtest per [Claude 47/48] §4.2 proposal.

Question: 12,494 SM signals from 11 wallets. Would mirroring them have made money?
          If yes — which wallets pass markout filter?

Method:
  1. Load weather_resolutions.jsonl → map (city, date) → winner_temp
  2. Load sm_weather_signals.jsonl → all wallet trades
  3. For each signal where (city,date) is resolved:
       parse title → bucket_temp
       is_winner = (bucket_temp == winner_temp)
       BUY YES: pnl = (1 - price - $0.005) if winner else (-price - $0.005)
       BUY NO:  pnl = (-price - $0.005)    if winner else (1 - price - $0.005)
       (SELL: skip for v1)
  4. Group by wallet → n, WR, avg_markout, Sharpe
  5. Filter wallets: n ≥ 30, avg_markout ≥ 0.02

Output: stdout + /app/data/sm_mirror_backtest.jsonl
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RESOLUTIONS = Path("/app/data/weather_resolutions.jsonl")
SIGNALS = Path("/app/data/sm_weather_signals.jsonl")
OUTPUT = Path("/app/data/sm_mirror_backtest.jsonl")
SPREAD_COST = 0.005


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def parse_temp(text: str) -> int | None:
    """Extract integer temperature in °C from text."""
    m = re.search(r"(\d{1,2})\s*°?\s*c", text.lower())
    if m:
        return int(m.group(1))
    return None


def parse_city_date(text: str) -> tuple[str | None, str | None]:
    """Parse city + date from title. Cities: tokyo/taipei/jakarta/etc.
    Date: 'may N' or 'on may N'."""
    t = text.lower()
    cities = ["tokyo", "taipei", "jakarta", "manila", "bangkok", "singapore",
              "hong kong", "seoul", "shanghai", "beijing", "mumbai", "delhi"]
    city = next((c for c in cities if c in t), None)
    # date — month name + day number
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})", t)
    date = f"{m.group(1)} {m.group(2)}" if m else None
    return city, date


def main() -> None:
    if not RESOLUTIONS.exists():
        print("weather_resolutions.jsonl missing")
        return
    if not SIGNALS.exists():
        print("sm_weather_signals.jsonl missing")
        return

    print("=" * 78)
    print(f"  SM MIRROR BACKTEST  ({datetime.now(timezone.utc).isoformat()})")
    print(f"  per [Claude 48] §4.2 proposal")
    print("=" * 78)

    # 1. Build resolution map: (city, date) -> winner_temp
    resolutions = []
    with RESOLUTIONS.open() as fh:
        for ln in fh:
            try:
                resolutions.append(json.loads(ln))
            except Exception:
                pass
    res_map: dict[tuple[str, str], int] = {}
    for r in resolutions:
        city, date = parse_city_date(r.get("title", ""))
        winner_temp = parse_temp(r.get("winner_bucket", ""))
        if city and date and winner_temp:
            res_map[(city, date)] = winner_temp
    print(f"\n  Resolutions loaded:        {len(resolutions)}")
    print(f"  Resolution map (city,date): {len(res_map)} entries")
    for k, v in list(res_map.items())[:8]:
        print(f"    {k} → winner={v}°C")

    # 2. Load signals
    signals = []
    with SIGNALS.open() as fh:
        for ln in fh:
            try:
                signals.append(json.loads(ln))
            except Exception:
                pass
    print(f"\n  Signals loaded:            {len(signals)}")

    # 3. Score each signal
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    n_resolved = 0
    n_unresolved = 0
    n_skip = 0
    for s in signals:
        wallet = s.get("wallet", "?")
        side = s.get("side", "")
        outcome = s.get("outcome", "")  # 'Yes' or 'No'
        price = s.get("price", 0.0)
        size = s.get("size", 0.0)
        title = (s.get("title") or "").lower()

        city, date = parse_city_date(title)
        bucket_temp = parse_temp(title)
        if not (city and date and bucket_temp):
            n_skip += 1
            continue
        if (city, date) not in res_map:
            n_unresolved += 1
            continue

        winner_temp = res_map[(city, date)]
        is_winner = bucket_temp == winner_temp

        # Markout per share (resolution outcome)
        if side == "BUY" and outcome == "Yes":
            markout = (1.0 - price - SPREAD_COST) if is_winner else (-price - SPREAD_COST)
        elif side == "BUY" and outcome == "No":
            markout = (-price - SPREAD_COST) if is_winner else (1.0 - price - SPREAD_COST)
        else:
            n_skip += 1
            continue
        n_resolved += 1
        by_wallet[wallet].append({
            "title": title[:50],
            "side": side, "outcome": outcome,
            "price": price, "size": size,
            "bucket": bucket_temp, "winner": winner_temp,
            "is_winner": is_winner,
            "markout_per_share": markout,
            "pnl_per_signal": markout * size,  # mirroring at signal's size
        })

    print(f"  Signals resolved:          {n_resolved}")
    print(f"  Signals unresolved (no res match): {n_unresolved}")
    print(f"  Signals skipped (parse/SELL): {n_skip}")

    if n_resolved == 0:
        print("\n  ❌ No resolved signals to backtest. Need more weather_resolutions.")
        return

    # 4. Per-wallet stats
    print(f"\n{'─' * 78}")
    print(f"  PER-WALLET MARKOUT")
    print(f"{'─' * 78}")
    print(
        f"  {'wallet':<20} {'n':<5} {'WR':<7} "
        f"{'avg_markout':<13} {'med_markout':<13} {'std':<8} {'Sharpe':<8} {'tot_pnl_$1':<12}"
    )

    rows = []
    for wallet, trades in sorted(by_wallet.items(), key=lambda x: -len(x[1])):
        n = len(trades)
        markouts = [t["markout_per_share"] for t in trades]
        wins = sum(1 for m in markouts if m > 0)
        wr = wins / n if n else 0
        avg_m = sum(markouts) / n
        med_m = sorted(markouts)[n // 2]
        std_m = stdev(markouts)
        sharpe = avg_m / std_m if std_m > 0 else 0.0
        # total PnL if we'd mirrored at $1 per signal
        tot_pnl_dollar = sum(t["markout_per_share"] for t in trades)  # $1/share × markout
        rows.append({
            "wallet": wallet,
            "n": n, "wins": wins, "wr": wr,
            "avg_markout": avg_m, "med_markout": med_m,
            "stdev": std_m, "sharpe": sharpe,
            "tot_pnl_dollar": tot_pnl_dollar,
        })
        print(
            f"  {wallet[:18]:<20} {n:<5} {wr*100:>5.1f}%  "
            f"{avg_m:>+10.4f}    {med_m:>+10.4f}    {std_m:>6.4f}  "
            f"{sharpe:>+6.3f}  {tot_pnl_dollar:>+9.4f}"
        )

    # 5. Filter — wallets that pass [GPT 40] mirror gate
    print(f"\n{'─' * 78}")
    print(f"  WALLETS PASSING FILTER (n≥30, avg_markout≥2%)")
    print(f"{'─' * 78}")
    passing = [r for r in rows if r["n"] >= 30 and r["avg_markout"] >= 0.02]
    if not passing:
        # Soft filter: maybe lower bar
        loose = [r for r in rows if r["n"] >= 10 and r["avg_markout"] > 0]
        if not loose:
            print(f"  ❌ no wallets pass even loose filter (n≥10, avg>0)")
            print(f"  Verdict: SM mirror NO-GO with current data")
        else:
            print(f"  ⚠️  No wallets pass strict filter, but loose hits {len(loose)}:")
            for r in loose:
                print(f"    {r['wallet'][:18]} n={r['n']} avg={r['avg_markout']:.4f}")
            print(f"  Verdict: collect more resolution data")
    else:
        print(f"  ✅ {len(passing)} wallet(s) pass — candidates for mirror canary")
        for r in passing:
            print(f"    {r['wallet']} n={r['n']} avg={r['avg_markout']:+.4f} Sharpe={r['sharpe']:+.3f}")
        print(f"\n  RECOMMENDATION:")
        print(f"  Deploy sm_mirror_canary at $1 sizing on top wallet(s).")
        print(f"  Apply [GPT 40] ramp gates: 25 trades + 24h + correlation diversity.")

    # Save report
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "sm_mirror_v1",
            "n_signals": len(signals),
            "n_resolutions": len(resolutions),
            "n_resolved": n_resolved,
            "n_unresolved": n_unresolved,
            "n_skip": n_skip,
            "spread_cost": SPREAD_COST,
            "rows": rows,
            "passing": passing,
        }) + "\n")
    print(f"\n  ✓ summary appended to {OUTPUT}")


if __name__ == "__main__":
    main()
