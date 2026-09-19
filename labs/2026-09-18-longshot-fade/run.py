"""Run the longshot-fade lab end to end.

    python3 labs/2026-09-18-longshot-fade/run.py --stage all --since-days 7     # nightly incremental
    python3 labs/2026-09-18-longshot-fade/run.py --stage all --start 2025-09-01  # full backfill
    python3 labs/2026-09-18-longshot-fade/run.py --stage markets|history|obs|analyze|fills [flags]

Stages are idempotent and resume from the parquet store under data/labs/longshot/.
Extra flags are passed to every stage; each stage ignores the ones it does not know.
`fills` needs the private fills database and is not part of `all`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGES = {
    "markets": ["fetch_markets.py"],
    "history": ["fetch_history.py"],
    "obs": ["build_obs.py"],
    "analyze": ["analyze.py"],
    "fills": ["fills_check.py"],
}
ORDER = ["markets", "history", "obs", "analyze"]
ALL = [*ORDER, "fills"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", *ALL])
    a, rest = ap.parse_known_args()
    stages = ORDER if a.stage == "all" else [a.stage]
    for s in stages:
        print(f"== stage {s}", file=sys.stderr)
        subprocess.run([sys.executable, str(HERE / STAGES[s][0]), *rest], check=True, cwd=str(HERE))


if __name__ == "__main__":
    main()
