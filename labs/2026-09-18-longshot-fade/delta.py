"""Compare two results.json files, prepend an entry to CHANGELOG.md, write delta.json.

    python3 delta.py <previous results.json> <current results.json>

An *alert* is raised when a headline number changes sign, when a bootstrap interval starts
or stops covering zero, or when a band's win rate moves by more than 0.5 points. Alerts
are what should reach a human (Telegram, a PR); everything else is the routine drift of
a growing dataset.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHANGELOG = HERE / "CHANGELOG.md"
DELTA_JSON = HERE / "delta.json"
HEADLINE_H = ("1", "7", "30")
BANDS = ("0.00-0.05", "0.05-0.10", "0.10-0.20", "0.00-0.20")
REALIZED_PP = 0.5


def covers_zero(v: dict) -> bool:
    return v["ci_lo"] <= 0.0 <= v["ci_hi"]


def compare(prev: dict, cur: dict) -> dict:
    rows, alerts = [], []
    for h in HEADLINE_H:
        for b in BANDS:
            p = prev.get("fade_by_horizon", {}).get(h, {}).get(b, {})
            c = cur.get("fade_by_horizon", {}).get(h, {}).get(b, {})
            if not c.get("n"):
                continue
            row = {"h": int(h), "band": b, "n": [p.get("n"), c["n"]],
                   "realized": [p.get("realized"), c["realized"]],
                   "gross": [p.get("gross", {}).get("ret"), c["gross"]["ret"]],
                   "net": [p.get("net", {}).get("ret"), c["net"]["ret"]]}
            rows.append(row)
            if not p.get("n"):
                continue
            label = f"h={h}d band {b}"
            if (p["gross"]["ret"] >= 0) != (c["gross"]["ret"] >= 0):
                alerts.append(f"{label}: gross sell return changed sign {p['gross']['ret']*100:+.2f}% -> {c['gross']['ret']*100:+.2f}%")
            if covers_zero(p["gross"]) != covers_zero(c["gross"]):
                alerts.append(f"{label}: gross 95% CI {'now' if covers_zero(c['gross']) else 'no longer'} covers zero")
            if abs(p["realized"] - c["realized"]) * 100 > REALIZED_PP:
                alerts.append(f"{label}: realized win rate moved {p['realized']*100:.2f}% -> {c['realized']*100:.2f}%")
    for cat, c in cur.get("by_category_longshots", {}).get("1", {}).items():
        p = prev.get("by_category_longshots", {}).get("1", {}).get(cat, {})
        if p.get("n") and c.get("n") and (p["gross"]["ret"] >= 0) != (c["gross"]["ret"] >= 0):
            alerts.append(f"category {cat} at h=1d: gross sell return changed sign {p['gross']['ret']*100:+.2f}% -> {c['gross']['ret']*100:+.2f}%")
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "markets": [prev.get("markets"), cur.get("markets")],
            "observations": [prev.get("observations"), cur.get("observations")],
            "rows": rows, "alerts": alerts}


def fmt_entry(d: dict) -> str:
    m0, m1 = d["markets"]; o0, o1 = d["observations"]
    lines = [f"## {d['date']}", "",
             f"Markets with observations: {m0 or 0:,} -> {m1:,} ({(m1 - (m0 or 0)):+,}); observations: {o0 or 0:,} -> {o1:,}.", ""]
    if d["alerts"]:
        lines.append("**Alerts (a human should look):**")
        lines += [f"- {a}" for a in d["alerts"]]
        lines.append("")
    else:
        lines.append("No sign changes or interval flips in the headline bands.")
        lines.append("")
    lines.append("| horizon | band | n | realized | sell gross | sell net |")
    lines.append("|---|---|---|---|---|---|")
    for r in d["rows"]:
        def pair(v, f):
            a, b = v
            return f"{f(b)}" if a is None else f"{f(a)} -> {f(b)}"
        lines.append(f"| {r['h']}d | {r['band']} | {pair(r['n'], lambda x: f'{x:,}')} | "
                     f"{pair(r['realized'], lambda x: f'{x*100:.2f}%')} | "
                     f"{pair(r['gross'], lambda x: f'{x*100:+.2f}%')} | {pair(r['net'], lambda x: f'{x*100:+.2f}%')} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    prev_p, cur_p = Path(sys.argv[1]), Path(sys.argv[2])
    prev = json.loads(prev_p.read_text()) if prev_p.exists() else {}
    cur = json.loads(cur_p.read_text())
    d = compare(prev, cur)
    DELTA_JSON.write_text(json.dumps(d, indent=1))
    entry = fmt_entry(d)
    old = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    header = "# Changelog of the numbers\n\nOne entry per nightly refresh, newest first. Written by `delta.py`; the prose in README is revised by hand when an alert appears.\n\n"
    body = old[len(header):] if old.startswith(header) else old
    # replace today's entry if the job ran twice in a day
    if body.startswith(f"## {d['date']}"):
        nxt = body.find("\n## ", 1)
        body = body[nxt + 1:] if nxt != -1 else ""
    CHANGELOG.write_text(header + entry + body, encoding="utf-8")
    print(f"delta: {len(d['alerts'])} alerts; markets {d['markets']}, observations {d['observations']}", file=sys.stderr)
    for a in d["alerts"]:
        print("  ALERT", a, file=sys.stderr)


if __name__ == "__main__":
    main()
