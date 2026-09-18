from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select

from app.db import db_session
from app.models import MarketSnapshot


@dataclass
class Snap:
    ts: datetime
    bid: float
    ask: float
    last: float
    fair: float
    category: str
    question: str

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last or 0.5

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0) if self.ask and self.bid else 0.0


def load_timelines() -> dict[str, list[Snap]]:
    timelines: dict[str, list[Snap]] = defaultdict(list)
    with db_session() as session:
        rows = session.execute(
            select(MarketSnapshot).order_by(MarketSnapshot.market_id, MarketSnapshot.created_at)
        ).scalars().all()
    for r in rows:
        timelines[r.market_id].append(
            Snap(
                ts=r.created_at,
                bid=float(r.best_bid or 0),
                ask=float(r.best_ask or 0),
                last=float(r.last_price or 0),
                fair=float(r.fair_price or 0),
                category=r.category or "",
                question=r.question or "",
            )
        )
    return timelines


def simulate_buy(timeline: list[Snap], entry_idx: int, entry_price: float, tp: float, sl: float, max_age_min: float) -> tuple[float, str, float]:
    entry_ts = timeline[entry_idx].ts
    for i in range(entry_idx + 1, len(timeline)):
        snap = timeline[i]
        age_min = (snap.ts - entry_ts).total_seconds() / 60.0
        mid = snap.mid
        if mid <= 0:
            continue
        pnl_pct = (mid - entry_price) / entry_price
        if pnl_pct >= tp:
            return pnl_pct, "TP", age_min
        if pnl_pct <= -sl and age_min >= 5:
            return pnl_pct, "SL", age_min
        if age_min >= max_age_min:
            return pnl_pct, "timeout", age_min
    final_mid = timeline[-1].mid if timeline else entry_price
    return (final_mid - entry_price) / entry_price, "eod", (timeline[-1].ts - entry_ts).total_seconds() / 60.0


# === Strategies ===

def strat_quant_buy(snap: Snap, idx: int, timeline: list[Snap], threshold: float = 0.07) -> tuple[str, float] | None:
    if not snap.ask or not snap.fair:
        return None
    edge = snap.fair - snap.ask - snap.spread / 2
    if edge >= threshold:
        return ("BUY", snap.ask)
    return None


def strat_quant_reverse(snap: Snap, idx: int, timeline: list[Snap], threshold: float = 0.07) -> tuple[str, float] | None:
    if not snap.bid or not snap.fair or not snap.ask:
        return None
    edge = snap.bid - snap.fair + snap.spread / 2
    if edge >= threshold:
        return ("BUY", snap.ask)
    return None


def strat_momentum(snap: Snap, idx: int, timeline: list[Snap], lookback: int = 5, threshold: float = 0.02) -> tuple[str, float] | None:
    if idx < lookback or not snap.ask:
        return None
    past = timeline[idx - lookback]
    if past.mid <= 0:
        return None
    delta = (snap.mid - past.mid) / past.mid
    if delta >= threshold:
        return ("BUY", snap.ask)
    return None


def strat_mean_reversion(snap: Snap, idx: int, timeline: list[Snap], lookback: int = 5, threshold: float = 0.02) -> tuple[str, float] | None:
    if idx < lookback or not snap.ask:
        return None
    past = timeline[idx - lookback]
    if past.mid <= 0:
        return None
    delta = (snap.mid - past.mid) / past.mid
    if delta <= -threshold:
        return ("BUY", snap.ask)
    return None


def strat_breakout_high(snap: Snap, idx: int, timeline: list[Snap], lookback: int = 20) -> tuple[str, float] | None:
    if idx < lookback or not snap.ask:
        return None
    window = timeline[max(0, idx - lookback):idx]
    if not window:
        return None
    high = max(s.mid for s in window if s.mid > 0)
    if snap.mid > high * 1.02:
        return ("BUY", snap.ask)
    return None


def strat_buy_dip(snap: Snap, idx: int, timeline: list[Snap], lookback: int = 20) -> tuple[str, float] | None:
    if idx < lookback or not snap.ask:
        return None
    window = timeline[max(0, idx - lookback):idx]
    if not window:
        return None
    low = min(s.mid for s in window if s.mid > 0)
    if snap.mid < low * 0.98 and snap.mid > 0:
        return ("BUY", snap.ask)
    return None


STRATEGIES: dict[str, Callable[..., tuple[str, float] | None]] = {
    "quant_buy": strat_quant_buy,
    "quant_reverse": strat_quant_reverse,
    "momentum": strat_momentum,
    "mean_reversion": strat_mean_reversion,
    "breakout_high": strat_breakout_high,
    "buy_dip": strat_buy_dip,
}


def run_one(
    name: str,
    timelines: dict[str, list[Snap]],
    tp: float = 0.30,
    sl: float = 0.03,
    max_age_min: float = 120.0,
    min_price: float = 0.25,
    max_price: float = 0.85,
    cooldown_min: float = 30.0,
    max_spread: float = 0.02,
    min_volume_proxy: int = 0,
) -> dict[str, Any]:
    fn = STRATEGIES[name]
    trades: list[dict[str, Any]] = []
    for market_id, timeline in timelines.items():
        if len(timeline) < 10:
            continue
        cooldown_until: datetime | None = None
        for idx, snap in enumerate(timeline):
            if snap.mid < min_price or snap.mid > max_price:
                continue
            if snap.spread > max_spread:
                continue
            if cooldown_until is not None and snap.ts < cooldown_until:
                continue
            sig = fn(snap, idx, timeline)
            if sig is None:
                continue
            _, entry_price = sig
            if not entry_price or entry_price <= 0:
                continue
            pnl_pct, exit_reason, age_min = simulate_buy(timeline, idx, entry_price, tp, sl, max_age_min)
            trades.append(
                {
                    "market_id": market_id,
                    "category": snap.category,
                    "entry_price": entry_price,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                    "age_min": age_min,
                }
            )
            cooldown_until = snap.ts + timedelta(minutes=cooldown_min)

    if not trades:
        return {"strategy": name, "tp": tp, "sl": sl, "trades": 0}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] < 0]
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl_pct = sum(t["pnl_pct"] for t in trades)
    rr = abs(avg_win / avg_loss) if avg_loss < 0 else 0
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t["exit_reason"]] += 1
    return {
        "strategy": name,
        "tp": tp,
        "sl": sl,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win_pct": round(avg_win * 100, 2),
        "avg_loss_pct": round(avg_loss * 100, 2),
        "rr": round(rr, 2),
        "total_pnl_pct": round(total_pnl_pct * 100, 1),
        "expectancy_pct": round(total_pnl_pct / len(trades) * 100, 3),
        "exits": dict(by_reason),
    }


def main() -> None:
    timelines = load_timelines()
    total_snaps = sum(len(t) for t in timelines.values())
    print(f"Loaded {len(timelines)} markets, {total_snaps} total snapshots")
    if not timelines:
        return

    print(f"\n{'strategy':<28} {'TP/SL':<10} {'trades':>7} {'WR%':>6} {'win%':>7} {'loss%':>7} {'R:R':>5} {'total%':>8} {'EV%':>7}")
    print("-" * 110)
    grids = [(0.30, 0.03), (0.20, 0.05), (0.15, 0.07), (0.10, 0.10)]
    rows: list[dict[str, Any]] = []
    for name in STRATEGIES:
        for tp, sl in grids:
            r = run_one(name, timelines, tp=tp, sl=sl)
            rows.append(r)
            if r.get("trades", 0) == 0:
                continue
            print(
                f"{name:<28} {f'{int(tp*100)}/{int(sl*100)}':<10} "
                f"{r['trades']:>7} {r['win_rate_pct']:>6.1f} "
                f"{r['avg_win_pct']:>7.2f} {r['avg_loss_pct']:>7.2f} {r['rr']:>5.2f} "
                f"{r['total_pnl_pct']:>8.1f} {r['expectancy_pct']:>7.3f}"
            )
    print("\n=== Top 5 by expectancy ===")
    valid = [r for r in rows if r.get("trades", 0) >= 10]
    valid.sort(key=lambda x: -x["expectancy_pct"])
    for r in valid[:5]:
        print(
            f"  {r['strategy']:<25} TP={int(r['tp']*100)}% SL={int(r['sl']*100)}% "
            f"trades={r['trades']} WR={r['win_rate_pct']}% EV={r['expectancy_pct']}% total={r['total_pnl_pct']}% "
            f"exits={r['exits']}"
        )


if __name__ == "__main__":
    main()
