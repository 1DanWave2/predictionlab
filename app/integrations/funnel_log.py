"""Lightweight JSONL funnel log per [GPT 11].

Writes one JSON line per stage event to /app/data/funnel.jsonl.
Stages: signal_generated, risk_rejected, order_filled.

No migration. Analyze with:
    cat funnel.jsonl | jq 'select(.stage=="risk_rejected")'
"""
from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_PATH = Path(os.getenv("FUNNEL_LOG_PATH", "/app/data/funnel.jsonl"))


def funnel_log(stage: str, **fields: Any) -> None:
    """Append a JSON line. Best-effort, never raises."""
    try:
        record = {"ts": datetime.now(UTC).isoformat(), "stage": stage, **fields}
        line = json.dumps(record, default=str, separators=(",", ":"))
        with _LOCK:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            with _PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
