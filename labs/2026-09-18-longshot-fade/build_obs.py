"""Stage 3: turn markets + daily histories (parquet store) into observations.

One row per (market, horizon): the last daily YES price observed at least `horizon` days
before the *event time*, and whether YES resolved true.

Event time is when the uncertainty ends, not when Polymarket booked the resolution:
  - sports: gameStartTime (the game decides it; anything after start is contaminated)
  - "by <Month> <day>, <year>" / "by end of <Month> <year>" questions: that deadline (end of day UTC)
  - otherwise the earliest of endDate and closedTime
Observations taken before the market started trading, the exact-0.50 placeholder that
exists before the first trade, and untraded opening quotes near 0.50 are dropped.

Output: data/labs/longshot/observations.parquet
"""
from __future__ import annotations

import calendar
import re
import sys
from datetime import datetime, timezone

import pandas as pd

from common import HORIZONS_DAYS, OBS_PARQUET, category_of, parse_ts, resolved_yes

DAY = 86400
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
DEADLINE_RE = re.compile(
    r"\b(?:by|before|on|until|through)\s+(?:end of\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
EOM_RE = re.compile(r"\b(?:by|before|until|through)\s+(?:the\s+)?end of\s+([A-Za-z]{3,9})\.?\s+(\d{4})", re.I)


def deadline_from_question(q: str) -> int | None:
    e = EOM_RE.search(q or "")
    if e:
        mon = MONTHS.get(e.group(1).lower())
        if mon:
            last = calendar.monthrange(int(e.group(2)), mon)[1]
            return int(datetime(int(e.group(2)), mon, last, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    m = DEADLINE_RE.search(q or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        dt = datetime(int(m.group(3)), mon, int(m.group(2)), 23, 59, 59, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def event_time(m: dict) -> tuple[int | None, str]:
    closed = parse_ts(m.get("closedTime")) or parse_ts(m.get("umaEndDate"))
    end = parse_ts(m.get("endDate"))
    game = parse_ts(m.get("gameStartTime"))
    if game:
        return game, "game_start"
    dl = deadline_from_question(m.get("question"))
    cands = [t for t in (closed, end) if t]
    if dl and (not cands or dl < min(cands)):
        return dl, "deadline_in_question"
    if end and closed:
        return (end, "end_date") if end <= closed else (closed, "closed_time")
    if closed:
        return closed, "closed_time"
    if end:
        return end, "end_date"
    return None, "none"


def main() -> None:
    from store import histories_by_market, markets_records  # local import keeps event_time importable elsewhere

    hists = histories_by_market()
    rows = []
    n_m = n_hist = 0
    src_counts: dict[str, int] = {}
    for m in markets_records():
        y = resolved_yes(m)
        if y is None:
            continue
        n_m += 1
        pts = hists.get(str(m["id"]))
        if not pts or len(pts) < 2:
            continue
        n_hist += 1
        t_event, src = event_time(m)
        if not t_event:
            continue
        src_counts[src] = src_counts.get(src, 0) + 1
        t_close = parse_ts(m.get("closedTime")) or parse_ts(m.get("umaEndDate")) or parse_ts(m.get("endDate")) or t_event
        t_start = parse_ts(m.get("acceptingOrdersTimestamp")) or parse_ts(m.get("startDate")) or parse_ts(m.get("createdAt")) or pts[0][0]
        first_t = pts[0][0]
        base = {
            "market_id": str(m["id"]),
            "condition_id": m.get("conditionId"),
            "question": m.get("question"),
            "category": category_of(m),
            "event_src": src,
            "neg_risk": bool(m.get("negRisk")),
            "volume": float(m.get("volumeNum") or 0.0),
            "event_ts": t_event,
            "close_ts": t_close,
            "close_month": pd.to_datetime(t_event, unit="s").strftime("%Y-%m"),
            "life_days": (t_event - t_start) / DAY,
            "y_yes": int(y),
        }
        for h in HORIZONS_DAYS:
            cutoff = t_event - h * DAY
            cand = [(t, p) for t, p in pts if t <= cutoff and t >= t_start]
            if not cand:
                continue
            t, p = cand[-1]
            if p <= 0.0 or p >= 1.0:
                continue
            if t == first_t and abs(p - 0.5) < 1e-9:
                continue  # pre-trade placeholder
            if 0.4 <= p <= 0.6 and all(abs(pp - p) < 1e-9 for _, pp in cand):
                continue  # untraded opening quote
            rows.append({**base, "horizon_days": h, "obs_ts": t, "p_yes": p})
    df = pd.DataFrame(rows)
    df.to_parquet(OBS_PARQUET, index=False)
    print(f"markets resolved={n_m} with_history={n_hist} observations={len(df)} -> {OBS_PARQUET}", file=sys.stderr)
    print("event time source:", src_counts, file=sys.stderr)
    if len(df):
        print(df.groupby("horizon_days").size().to_string(), file=sys.stderr)
        (OBS_PARQUET.parent / "data-window.txt").write_text(
            f"markets with observations: {df['market_id'].nunique()}\n"
            f"event time range: {pd.to_datetime(df['event_ts'].min(), unit='s')} .. {pd.to_datetime(df['event_ts'].max(), unit='s')}\n"
            f"event time source: {src_counts}\n"
            f"observations by horizon: {df.groupby('horizon_days').size().to_dict()}\n"
            f"filters: closed markets, volume >= $5k, clean 0/1 resolution, >=2 daily points, lifetime >= 1.5d\n"
            f"built: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")


if __name__ == "__main__":
    main()
