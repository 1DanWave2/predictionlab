"""Run the longshot-fade lab end to end.

    python3 labs/2026-09-18-longshot-fade/run.py --stage all
    python3 labs/2026-09-18-longshot-fade/run.py --stage markets|history|obs|analyze|fills

Stages are idempotent and resume from what is already on disk under data/labs/longshot/.
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
ORDER = ["markets", "history", "obs", "analyze", "fills"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", *ORDER])
    a, rest = ap.parse_known_args()
    stages = ORDER if a.stage == "all" else [a.stage]
    for s in stages:
        print(f"== stage {s}", file=sys.stderr)
        subprocess.run([sys.executable, str(HERE / STAGES[s][0]), *rest], check=True, cwd=str(HERE))


if __name__ == "__main__":
    main()
