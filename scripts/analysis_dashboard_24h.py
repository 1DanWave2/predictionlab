"""Cross-strategy 24-48h analysis per [GPT 31] Day 3.

Reads all shadow jsonls, produces a unified "judgment day" report:
  - hedge_shadow:        clean/incomplete/stale/error counts, edge_decay distribution
  - weather_bucket_shadow: signals, top-edge cities, hours-to-resolution histogram
  - paper_maker_sim:     proxy vs strict fill counts, median markouts, status mix
  - sm_weather_v2:       candidate count, top wallets, follow-signal recency
  - bucket_pnl:          actual realized PnL by strategy bucket

Output: stdout report + writes rolling summary to /app/data/analysis_24h.jsonl

Usage: docker exec polymarket-bot python3 -m scripts.analysis_dashboard_24h
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DB = Path("/app/data/paper_bot.db")
DATA = Path("/app/data")


def section(title: str) -> None:
    print()
    print("═" * 75)
    print(f"  {title}")
    print("═" * 75)


def load_jsonl(name: str) -> list[dict]:
    p = DATA / name
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def latest_per_event(records: list[dict]) -> list[dict]:
    by_e: dict[str, dict] = {}
    for r in records:
        eid = r.get("event_id")
        if not eid:
            continue
        if eid not in by_e or r.get("ts", 0) > by_e[eid].get("ts", 0):
            by_e[eid] = r
    return list(by_e.values())


def hist_buckets(values: list[float], bins: list[tuple[float, float, str]]) -> list[tuple[str, int]]:
    out = []
    for lo, hi, label in bins:
        n = sum(1 for v in values if lo <= v < hi)
        out.append((label, n))
    return out


def ago(ts: int) -> str:
    if not ts:
        return "?"
    delta = int(time.time()) - ts
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ──────────────────────────────────────────────────────────────────────────────

def report_hedge_shadow() -> dict:
    section("🛡️  HEDGE SHADOW — taker arb (PAUSED)")
    recs = load_jsonl("hedge_shadow.jsonl")
    if not recs:
        print("  no data")
        return {}
    # filter rate_telemetry records
    sims = [r for r in recs if r.get("kind") != "rate_telemetry"]
    rate = [r for r in recs if r.get("kind") == "rate_telemetry"]
    latest = latest_per_event(sims)
    by_status: Counter = Counter(r.get("status", "?") for r in latest)

    print(f"  total runs:        {len(sims)}")
    print(f"  unique events:     {len(latest)}")
    print(f"  status breakdown (unique events):")
    for s, n in by_status.most_common():
        print(f"    {s:<25} {n}")

    # ever-seen CLEAN
    ever_clean = {r["event_id"] for r in sims if r.get("status") == "CLEAN_EXECUTABLE"}
    print(f"  CLEAN_EXECUTABLE ever seen: {len(ever_clean)} unique events (target ≥20)")

    # rate-limit averages
    if rate:
        recent = sorted(rate, key=lambda r: r.get("ts", 0))[-10:]
        m_p95 = sum(r.get("p95_latency_ms", 0) for r in recent) / max(1, len(recent))
        m_429 = sum(r.get("http_429", 0) for r in recent)
        m_other = sum(r.get("http_other_error", 0) for r in recent)
        print(f"  recent 10-run rate: avg p95={m_p95:.0f}ms  429={m_429}  other_err={m_other}")

    # edge decay
    decays = [
        r.get("status_data", {}).get("edge_decay")
        for r in sims if r.get("status_data") and r["status_data"].get("edge_decay") is not None
    ]
    if decays:
        decays_sorted = sorted(decays)
        print(f"  edge_decay (pp): n={len(decays)} median={decays_sorted[len(decays_sorted)//2]*100:.2f}pp "
              f"min={min(decays)*100:.2f}pp max={max(decays)*100:.2f}pp")
    return {
        "vector": "hedge_shadow",
        "total_runs": len(sims),
        "unique_events": len(latest),
        "ever_clean": len(ever_clean),
        "by_status": dict(by_status),
    }


def report_weather_bucket() -> dict:
    section("🌡️  WEATHER BUCKET SHADOW — forecast vs market")
    recs = load_jsonl("weather_bucket_shadow.jsonl")
    if not recs:
        print("  no data")
        return {}
    latest = latest_per_event(recs)
    valid = [r for r in latest if "skipped" not in r and "error" not in r]
    skipped = [r for r in latest if "skipped" in r]
    sk_reasons = Counter(r.get("skipped", "?") for r in skipped)

    all_signals = []
    for r in valid:
        for s in r.get("signals", []):
            all_signals.append({**s, "event_id": r["event_id"], "city": r.get("city"), "title": r.get("title", "")})

    edge_dist = hist_buckets(
        [s.get("edge_buy_pp", 0) for s in all_signals],
        [(3, 5, "3-5pp"), (5, 10, "5-10pp"), (10, 20, "10-20pp"), (20, 50, "20-50pp"), (50, 100, "50pp+")],
    )

    print(f"  total runs:    {len(recs)}")
    print(f"  unique events: {len(latest)}  valid={len(valid)} skipped={len(skipped)}")
    if sk_reasons:
        print(f"  skip reasons:")
        for r, n in sk_reasons.most_common():
            print(f"    {r:<30} {n}")
    print(f"  total signals (≥3pp edge, depth ≥5):  {len(all_signals)}")
    print(f"  edge distribution:")
    for label, n in edge_dist:
        print(f"    {label:<10} {n}")

    if all_signals:
        all_signals.sort(key=lambda s: -s.get("edge_buy_pp", 0))
        print(f"\n  TOP 5 active signals (status=BUG-RISK HIGH until 5 manual verifies):")
        for s in all_signals[:5]:
            print(f"    {s['city']:<10} {s.get('bucket_kind')}{s.get('bucket_temp')}°C  "
                  f"forecast={s['forecast_prob']*100:.1f}%  ask=${s['ask_price']:.4f}  "
                  f"edge=+{s['edge_buy_pp']:.2f}pp  | {s['title'][:35]}")

    return {
        "vector": "weather_bucket",
        "total_runs": len(recs),
        "unique_events": len(latest),
        "signals": len(all_signals),
        "edge_dist": dict(edge_dist),
    }


def report_paper_maker() -> dict:
    section("🎯  PAPER TAIL-MAKER SIM — proxy vs strict fills")
    recs = load_jsonl("paper_maker_sim.jsonl")
    if not recs:
        print("  no data")
        return {}
    snaps = [r for r in recs if r.get("kind") == "quote_snapshot"]
    fills = [r for r in recs if r.get("kind") == "markout"]

    n_quotes = sum(s.get("n_tail_quotes", 0) or 0 for s in snaps)
    proxy_fills = [f for f in fills if f.get("crossed_quote_proxy")]
    strict_fills = [f for f in fills if f.get("strict_filled")]

    print(f"  quote snapshots:   {len(snaps)}")
    print(f"  total quotes ever: {n_quotes}")
    print(f"  fills evaluated:   {len(fills)}")

    by_status = Counter(f.get("fill_status", "?") for f in fills)
    print(f"  fill_status breakdown:")
    for s, n in by_status.most_common():
        print(f"    {s:<30} {n}")

    print(f"\n  PROXY (upper-bound) filled: {len(proxy_fills)}")
    if proxy_fills:
        proxy_pnls = [f.get("markout_pnl_per_share_proxy") or 0 for f in proxy_fills]
        proxy_total = sum(
            (f.get("markout_pnl_per_share_proxy") or 0) * (f.get("shares") or 0) for f in proxy_fills
        )
        print(f"    median markout/sh: ${sorted(proxy_pnls)[len(proxy_pnls)//2]:.4f}")
        print(f"    total paper PnL:   ${proxy_total:.2f}")

    print(f"\n  STRICT (trade-flow) filled: {len(strict_fills)}")
    if strict_fills:
        strict_pnls = [f.get("markout_pnl_per_share_strict") or 0 for f in strict_fills]
        strict_total = sum(
            (f.get("markout_pnl_per_share_strict") or 0) * (f.get("shares") or 0) for f in strict_fills
        )
        print(f"    median markout/sh: ${sorted(strict_pnls)[len(strict_pnls)//2]:.4f}")
        print(f"    total paper PnL:   ${strict_total:.2f}")
    else:
        print(f"    (no strict fills yet — accumulating)")

    return {
        "vector": "paper_maker",
        "snapshots": len(snaps),
        "fills_eval": len(fills),
        "proxy_filled": len(proxy_fills),
        "strict_filled": len(strict_fills),
        "fill_status": dict(by_status),
    }


def report_sm_weather() -> dict:
    section("🐳  SMART MONEY v2 — Weather Specialists (WATCHLIST)")
    cands = load_jsonl("sm_weather_candidates.jsonl")
    sigs = load_jsonl("sm_weather_signals.jsonl")
    if cands:
        latest = max(cands, key=lambda c: c.get("ts", 0))
        clist = latest.get("candidates", [])
        print(f"  latest snapshot:  {ago(latest.get('ts', 0))}")
        print(f"  candidates:       {len(clist)}")
        print(f"\n  TOP 5 specialist wallets:")
        for c in clist[:5]:
            print(f"    {c['wallet'][:14]}..  trades={c['n_temp_trades']}  "
                  f"events={c['n_unique_events']}  avg=${c['avg_notional_usd']:.0f}  "
                  f"last={c['last_active_days_ago']:.1f}d")
    if sigs:
        cutoff = int(time.time()) - 7 * 86400
        recent = [s for s in sigs if s.get("ts", 0) >= cutoff]
        print(f"\n  follow-signals (7d): {len(recent)}")
        # unique wallets
        unique_w = len({s.get("wallet") for s in recent})
        print(f"  unique wallets active: {unique_w}")
        # most-traded condition
        cid = Counter(s.get("condition_id") for s in recent if s.get("condition_id"))
        if cid:
            top_cid, top_n = cid.most_common(1)[0]
            print(f"  most-traded market: {top_cid[:18]}...  ({top_n} fills)")
    return {
        "vector": "sm_weather",
        "candidates": len(cands),
        "signals_total": len(sigs),
    }


def report_bucket_pnl() -> dict:
    section("💰  PER-BUCKET PnL — actual live realized")
    if not DB.exists():
        print("  no DB")
        return {}
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    print(f"  {'bucket':<22} {'n':<4} {'W/L':<8} {'WR':<6} {'total':<10} {'avg':<10}")
    rows = c.execute("""
        SELECT bucket, COUNT(*),
          SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),
          SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END),
          ROUND(SUM(realized_pnl), 2),
          ROUND(AVG(realized_pnl), 4)
        FROM positions WHERE quantity = 0
        GROUP BY bucket ORDER BY 5 DESC
    """).fetchall()
    summary = []
    for b, n, w, l, t, a in rows:
        w = w or 0; l = l or 0
        wr = (w / max(1, w + l)) * 100
        print(f"  {b or 'none':<22} {n:<4} {w}/{l:<5} {wr:.0f}%   ${t or 0:<8.2f} ${a or 0:<8.4f}")
        summary.append({"bucket": b, "n": n, "wins": w, "losses": l, "total": t, "avg": a})
    print()
    open_rows = c.execute("""
        SELECT bucket, COUNT(*), ROUND(SUM(unrealized_pnl), 2)
        FROM positions WHERE quantity > 0 GROUP BY bucket
    """).fetchall()
    print(f"  Open positions:")
    for b, n, u in open_rows:
        print(f"    {b or 'none':<22} {n} open  unrealized=${u or 0:.2f}")
    conn.close()
    return {"vector": "bucket_pnl", "closed": summary}


def kill_keep_quarantine() -> None:
    section("⚖️  KILL / KEEP / QUARANTINE — judgment day per [GPT 31]")
    print("""
  hedge_shadow      :  KEEP shadow (taker live PAUSED).
                       Action: feature-extraction only.
                       Re-eval gate: pause unchanged unless edge half-life > 2min.

  weather_bucket    :  QUARANTINE pending manual verifies.
                       Tokyo +51pp signal traced to GMT-vs-station-tz bug (FIXED today).
                       Re-eval gate: 5 manually verified events → re-promote to shadow.

  paper_maker_sim   :  KEEP shadow with status caveats.
                       Proxy = upper bound. Strict = trade-flow validated.
                       Re-eval gate: ≥30 strict fills → judgement.

  sm_weather_v2     :  KEEP watchlist. Live copy gated 14d + criteria.

  fade_any_canary   :  LIVE $1, telemetry-only auto-disable. Continue.
                       Closed trades to date: 0. Watching for first signal.

  event_strategy    :  LIVE workhorse. +$11.70 historic, 31 trades, 61% WR.
                       Continue, do not touch.

  financial_internal:  QUARANTINED. 1 trade, -$3.83. No re-enable yet.
""")


def main() -> None:
    print(f"\n{'═' * 75}")
    print(f"  CROSS-STRATEGY ANALYSIS — Day 3 judgment day per [GPT 31]")
    print(f"  Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'═' * 75}")

    out = {
        "ts": int(time.time()),
        "kind": "analysis_summary",
        "vectors": [],
    }
    out["vectors"].append(report_hedge_shadow())
    out["vectors"].append(report_weather_bucket())
    out["vectors"].append(report_paper_maker())
    out["vectors"].append(report_sm_weather())
    out["vectors"].append(report_bucket_pnl())
    kill_keep_quarantine()

    # Append a snapshot to a rolling jsonl
    out_path = DATA / "analysis_24h.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as fh:
        fh.write(json.dumps(out) + "\n")
    print(f"\nSummary appended to {out_path}")


if __name__ == "__main__":
    main()
