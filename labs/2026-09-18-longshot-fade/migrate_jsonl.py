"""One-off: convert the first run's raw files (markets.jsonl + history/*.json) into the parquet store.

    python3 migrate_jsonl.py            # reads data/labs/longshot/markets.jsonl and history/
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

from common import DATA, HIST
from store import HIST_PQ, HSTAT_PQ, MARKETS_PQ, STR_FIELDS, market_row


def main() -> None:
    now = int(time.time())
    src = DATA / "markets.jsonl"
    rows, bad = [], 0
    with src.open(encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(market_row(json.loads(line), now))
            except Exception:  # noqa: BLE001
                bad += 1
    df = pd.DataFrame(rows)
    for k in STR_FIELDS:
        df[k] = df[k].astype("string")
    df = df.drop_duplicates("id", keep="last").reset_index(drop=True)
    df.to_parquet(MARKETS_PQ, index=False)
    print(f"markets: {len(df)} rows ({bad} bad lines) -> {MARKETS_PQ} {MARKETS_PQ.stat().st_size/1e6:.0f} MB", file=sys.stderr)

    ids, ts, ps, status = [], [], [], []
    files = list(HIST.glob("*.json"))
    for i, p in enumerate(files, 1):
        mid = p.stem
        try:
            h = json.loads(p.read_text()).get("history") or []
        except Exception:  # noqa: BLE001
            status.append((mid, "error"))
            continue
        pts = [(int(x["t"]), float(x["p"])) for x in h if x.get("p") is not None]
        status.append((mid, "ok" if pts else "no_history"))
        for t, pv in pts:
            ids.append(mid); ts.append(t); ps.append(pv)
        if i % 50000 == 0:
            print(f"  {i}/{len(files)}", file=sys.stderr)
    hdf = pd.DataFrame({"market_id": pd.Series(ids, dtype="string"), "t": pd.Series(ts, dtype="int64"), "p": pd.Series(ps, dtype="float64")})
    hdf = hdf.drop_duplicates(["market_id", "t"], keep="last")
    hdf.to_parquet(HIST_PQ, index=False)
    sdf = pd.DataFrame([{"market_id": m, "status": s, "attempts": 1, "fetched_at": now} for m, s in status])
    sdf["market_id"] = sdf["market_id"].astype("string"); sdf["status"] = sdf["status"].astype("string")
    sdf.to_parquet(HSTAT_PQ, index=False)
    print(f"histories: {len(hdf)} points from {len(files)} files -> {HIST_PQ} {HIST_PQ.stat().st_size/1e6:.0f} MB; "
          f"status: {sdf['status'].value_counts().to_dict()}", file=sys.stderr)


if __name__ == "__main__":
    main()
