from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.ai.fair_price import get_default_client as get_fair_price_client
from app.ai.veto import get_default_client as get_veto_client
from app.api.admin import _serialize_position, _serialize_trade, runtime_state
from app.db import db_session
from app.models import OpportunityLog, PaperOrder, Position


router = APIRouter()

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 4.0
_ANALYTICS_TTL = 15.0


def _cached(key: str, ttl: float, builder):
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    data = builder()
    _cache[key] = (now, data)
    return data


@router.get("/dashboard")
async def dashboard_page() -> Response:
    return Response(content=DASHBOARD_HTML, media_type="text/html")


@router.get("/dashboard/analytics")
async def analytics_page() -> Response:
    return Response(content=ANALYTICS_HTML, media_type="text/html")


@router.get("/dashboard/data")
async def dashboard_data() -> dict[str, Any]:
    return _cached("dashboard", _CACHE_TTL, _build_dashboard_data)


def _build_dashboard_data() -> dict[str, Any]:
    with db_session() as session:
        positions = session.execute(select(Position).order_by(Position.id.desc())).scalars().all()
        trades = session.execute(select(PaperOrder).order_by(PaperOrder.id.desc()).limit(50)).scalars().all()
        all_orders = session.execute(select(PaperOrder).order_by(PaperOrder.id.asc())).scalars().all()

    open_pos = [p for p in positions if p.quantity > 0]
    closed_pos = [p for p in positions if p.quantity == 0]
    wins = [p for p in closed_pos if p.realized_pnl > 0]
    losses = [p for p in closed_pos if p.realized_pnl < 0]

    pos_pnl_map = {p.market_id: p.realized_pnl for p in closed_pos}
    pos_strat_map: dict[str, str] = {}
    for o in all_orders:
        if o.side == "BUY":
            pos_strat_map[o.market_id] = o.strategy

    reasons: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    seen_mid: set[str] = set()
    for t in trades:
        if t.side != "SELL" or t.market_id in seen_mid:
            continue
        seen_mid.add(t.market_id)
        note = t.note or ""
        if "take-profit" in note:
            kind = "TP"
        elif "trailing-tp" in note:
            kind = "trailing-TP"
        elif "pre-resolution" in note:
            kind = "pre-resolution"
        elif "safety-stop" in note:
            kind = "safety-stop"
        elif "hard-stop" in note:
            kind = "hard-stop"
        elif "stop-loss" in note:
            kind = "stop-loss"
        elif "stale" in note:
            kind = "stale"
        elif "orphan" in note:
            kind = "orphan"
        else:
            kind = "other"
        reasons[kind]["count"] = float(reasons[kind]["count"]) + 1
        reasons[kind]["pnl"] = float(reasons[kind]["pnl"]) + float(pos_pnl_map.get(t.market_id, 0.0))

    strat_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "orders": 0})
    for o in all_orders:
        strat_stats[o.strategy]["orders"] = float(strat_stats[o.strategy]["orders"]) + 1
    for p in closed_pos:
        s = pos_strat_map.get(p.market_id, "unknown")
        strat_stats[s]["pnl"] = float(strat_stats[s]["pnl"]) + float(p.realized_pnl)
        if p.realized_pnl > 0:
            strat_stats[s]["wins"] = float(strat_stats[s]["wins"]) + 1
        elif p.realized_pnl < 0:
            strat_stats[s]["losses"] = float(strat_stats[s]["losses"]) + 1

    closed_count = len(closed_pos)
    win_rate = round(len(wins) / closed_count * 100, 1) if closed_count else 0.0
    avg_win = round(sum(p.realized_pnl for p in wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(p.realized_pnl for p in losses) / len(losses), 2) if losses else 0.0
    rr = round(abs(avg_win / avg_loss), 2) if avg_loss < 0 else 0.0

    fp_client = get_fair_price_client()
    veto_client = get_veto_client()

    sniper_lab = _build_sniper_lab()
    asset_lab = _build_asset_target_lab()
    fade_lab = _build_fade_lab()
    radar_lab = _build_radar_lab()
    rewards_lab = _build_rewards_lab()
    pm_fills_lab = _build_pm_fills_lab()
    resolution_risk_lab = _build_resolution_risk_lab()
    arb_lab = _build_arb_lab()
    hedge_shadow_lab = _build_hedge_shadow_lab()
    weather_shadow_lab = _build_weather_shadow_lab()
    bucket_pnl = _build_bucket_pnl()
    strategy_health = _build_strategy_health()
    sm_weather_lab = _build_sm_weather_lab()
    maker_sim_lab = _build_maker_sim_lab()

    return {
        "runtime": runtime_state.snapshot(),
        "sniper_lab": sniper_lab,
        "asset_target_lab": asset_lab,
        "fade_lab": fade_lab,
        "radar_lab": radar_lab,
        "rewards_lab": rewards_lab,
        "pm_fills_lab": pm_fills_lab,
        "resolution_risk_lab": resolution_risk_lab,
        "arb_lab": arb_lab,
        "hedge_shadow_lab": hedge_shadow_lab,
        "weather_shadow_lab": weather_shadow_lab,
        "bucket_pnl": bucket_pnl,
        "sm_weather_lab": sm_weather_lab,
        "maker_sim_lab": maker_sim_lab,
        "summary": {
            "total_positions": len(positions),
            "open": len(open_pos),
            "closed": closed_count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "rr_ratio": rr,
        },
        "total_pnl_summary": _build_total_pnl_summary(closed_pos, open_pos, bucket_pnl),
        "strategy_health": strategy_health,
        "open_positions": [
            {**_serialize_position(p), "strategy": pos_strat_map.get(p.market_id, "unknown")}
            for p in open_pos
        ],
        "recent_trades": [_serialize_trade(t) for t in trades[:30]],
        "exit_reasons": [
            {"kind": k, "count": int(v["count"]), "pnl": round(v["pnl"], 2)}
            for k, v in sorted(reasons.items(), key=lambda x: -x[1]["count"])
        ],
        "by_strategy": [
            {
                "name": s,
                "orders": int(d["orders"]),
                "wins": int(d["wins"]),
                "losses": int(d["losses"]),
                "pnl": round(d["pnl"], 2),
            }
            for s, d in sorted(strat_stats.items())
        ],
        "ai_fair_price": fp_client.stats() if fp_client else None,
        "ai_veto": veto_client.stats() if veto_client else None,
    }


def _build_fade_lab() -> dict[str, Any]:
    """Fade-Any shadow signals + magnitude segmentation (live edge discovery)."""
    import json
    from pathlib import Path
    path = Path("/app/data/fade_signals.jsonl")
    if not path.exists():
        return {"total": 0, "by_magnitude": {}, "recent": []}
    lines = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {"total": 0, "by_magnitude": {}, "recent": []}
    by_mag = {"5-6pp": 0, "6-10pp": 0, "10pp+": 0}
    for s in lines:
        d = abs(s.get("delta_5m", 0))
        if d < 0.06:
            by_mag["5-6pp"] += 1
        elif d < 0.10:
            by_mag["6-10pp"] += 1
        else:
            by_mag["10pp+"] += 1
    by_ec = {"no_match": 0, "agrees": 0, "diverges": 0}
    for s in lines:
        ec = s.get("external_confirmation")
        if ec in by_ec:
            by_ec[ec] += 1
    recent = lines[-15:] if lines else []
    return {
        "total": len(lines),
        "by_magnitude": by_mag,
        "by_external_confirmation": by_ec,
        "sweet_spot_count": by_mag.get("6-10pp", 0),
        "sweet_spot_target": 30,
        "recent": [
            {
                "ts": r.get("ts"),
                "title": (r.get("title") or "")[:60],
                "delta_5m_pp": round(abs(r.get("delta_5m", 0)) * 100, 1),
                "fade_label": r.get("fade_token_label"),
                "expected_edge_pp": round(r.get("expected_edge", 0) * 100, 1),
                "ec": r.get("external_confirmation"),
            }
            for r in recent
        ],
    }


def _build_radar_lab() -> dict[str, Any]:
    """Cross-platform radar: PM↔Manifold gaps."""
    import json
    from pathlib import Path
    path = Path("/app/data/cross_market_gaps.jsonl")
    if not path.exists():
        return {"total": 0, "big_gaps": 0, "recent": []}
    lines = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {"total": 0, "big_gaps": 0, "recent": []}
    big_gaps = [r for r in lines if abs(r.get("gap_pp", 0)) >= 0.06]
    recent = sorted(big_gaps, key=lambda r: abs(r.get("gap_pp", 0)), reverse=True)[:10]
    return {
        "total": len(lines),
        "big_gaps": len(big_gaps),
        "recent_top_gaps": [
            {
                "title": (r.get("pm_title") or "")[:60],
                "pm_prob": round(r.get("pm_yes_prob", 0), 3),
                "mf_prob": round(r.get("mf_yes_prob", 0), 3),
                "gap_pp": round(r.get("gap_pp", 0) * 100, 2),
                "similarity": round(r.get("similarity", 0), 2),
            }
            for r in recent
        ],
    }


def _build_rewards_lab() -> dict[str, Any]:
    """Rewards opportunity score table."""
    import json
    from pathlib import Path
    path = Path("/app/data/rewards_table.jsonl")
    if not path.exists():
        return {"total": 0, "qualified": 0, "top": []}
    lines = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {"total": 0, "qualified": 0, "top": []}
    qualified = [r for r in lines if r.get("opportunity_score", 0) >= 50]
    top = sorted(lines, key=lambda r: r.get("opportunity_score", 0), reverse=True)[:10]
    return {
        "total": len(lines),
        "qualified": len(qualified),
        "top": [
            {
                "score": r.get("opportunity_score", 0),
                "title": (r.get("title") or "")[:60],
                "spread": r.get("rewards_max_spread", 0),
                "mid": r.get("mid", 0),
                "depth": r.get("existing_depth", 0),
            }
            for r in top
        ],
    }


def _build_arb_lab() -> dict[str, Any]:
    """Negative-risk arbitrage opportunities."""
    import json
    from pathlib import Path
    path = Path("/app/data/arb_opportunities.jsonl")
    if not path.exists():
        return {"total": 0, "candidates": 0, "top": []}
    lines = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {"total": 0, "candidates": 0, "top": []}
    if not lines:
        return {"total": 0, "candidates": 0, "top": []}
    latest_ts = max(l["ts"] for l in lines)
    latest = [l for l in lines if l["ts"] == latest_ts]
    candidates = [l for l in latest if l.get("arb_signal")]
    candidates.sort(key=lambda l: -l.get("edge_pp", 0))
    return {
        "total": len(latest),
        "candidates": len(candidates),
        "top": [
            {
                "edge_pp": c.get("edge_pp", 0),
                "signal": c.get("arb_signal", ""),
                "title": (c.get("title") or "")[:70],
                "n_markets": c.get("n_markets", 0),
                "min_liq": c.get("min_liq", 0),
            }
            for c in candidates[:10]
        ],
    }


def _build_total_pnl_summary(closed_pos: list, open_pos: list, bucket_pnl: dict) -> dict[str, Any]:
    """Aggregate total PnL across all strategies/buckets — top-level summary card.

    Returns:
      total_realized:  sum realized over all closed positions
      total_unrealized: sum unrealized over all open positions
      total_pnl:       realized + unrealized
      starting_balance: hardcoded reference
      current_balance:  starting + total_pnl (paper)
      roi_pct:         total_pnl / starting * 100
      by_bucket:       per-bucket breakdown {bucket: {realized, unrealized, total, n_closed, n_open, win_rate}}
      by_status:       counts of LIVE / SHADOW / QUARANTINED strategies
    """
    from datetime import datetime, timezone, timedelta
    starting_balance = 100.0
    total_realized = sum(float(p.realized_pnl or 0) for p in closed_pos)
    total_unrealized = sum(float(p.unrealized_pnl or 0) for p in open_pos)
    total_pnl = total_realized + total_unrealized
    current_balance = starting_balance + total_pnl
    roi_pct = round(total_pnl / starting_balance * 100, 2) if starting_balance else 0

    # Per-bucket aggregation (combine closed + open from existing bucket_pnl builder)
    by_bucket: dict[str, dict] = {}
    for r in (bucket_pnl or {}).get("closed", []):
        b = r["bucket"]
        by_bucket[b] = by_bucket.setdefault(b, {})
        by_bucket[b].update({
            "bucket": b,
            "realized": r["total_pnl"],
            "n_closed": r["trades"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_rate_pct": r["win_rate_pct"],
        })
    for r in (bucket_pnl or {}).get("open", []):
        b = r["bucket"]
        if b not in by_bucket:
            by_bucket[b] = {"bucket": b, "realized": 0, "n_closed": 0, "wins": 0, "losses": 0, "win_rate_pct": 0}
        by_bucket[b]["unrealized"] = r["unrealized"]
        by_bucket[b]["n_open"] = r["open_count"]
    for b, d in by_bucket.items():
        d.setdefault("unrealized", 0)
        d.setdefault("n_open", 0)
        d["total"] = round(d["realized"] + d["unrealized"], 2)

    # Today's PnL — closed positions updated within last 24h
    today_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    today_realized = sum(
        float(p.realized_pnl or 0) for p in closed_pos
        if p.updated_at and p.updated_at.replace(tzinfo=timezone.utc) >= today_cutoff
    )

    # Strategy status registry — single source of truth
    status_registry = {
        "experiment": "LIVE_workhorse",
        "fade_any_canary": "LIVE_canary_telemetry",
        "core_sniper_live": "LIVE_canary",
        "asset_target_live": "LIVE_canary",
        "financial_internal": "QUARANTINED",
        "none": "PAPER_legacy",
    }
    for d in by_bucket.values():
        d["status"] = status_registry.get(d["bucket"], "UNKNOWN")

    sorted_buckets = sorted(by_bucket.values(), key=lambda d: -d["total"])

    return {
        "starting_balance": starting_balance,
        "current_balance": round(current_balance, 2),
        "total_realized": round(total_realized, 2),
        "total_unrealized": round(total_unrealized, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": roi_pct,
        "today_realized": round(today_realized, 2),
        "by_bucket": sorted_buckets,
        "n_active_strategies": sum(1 for d in by_bucket.values() if d["status"].startswith("LIVE")),
        "n_quarantined": sum(1 for d in by_bucket.values() if d["status"] == "QUARANTINED"),
    }


def _build_strategy_health() -> dict[str, Any]:
    """Per-strategy plumbing health per [GPT 39] / refined [GPT 41].

    Status taxonomy (10 levels, per [GPT 41]):
      ACTIVELY_TRADING       — has positions/orders in last 24h
      LIVE_CAPABLE_IDLE      — armed live, no current candidates
      ARMED_TRADING          — orders in last 24h but no positions yet
      RISK_REJECTED          — signals exist, all risk_manager-blocked
      EXECUTION_REJECTED     — passed risk, no order created
      SHADOW_ONLY            — passes gates but never live (asset/sniper SQL path)
      SHADOW_LOW_EDGE        — opportunity found but tradable_edge below live_threshold
      LIVE_DISABLED          — live_enabled flag is False
      UNMAPPED               — markets seen but mapping_confidence=none dominant
      FILTERED_SCOPE         — markets rejected at scope/parse layer
      NO_CANDIDATES          — no signals reach scanner at all
      PIPELINE_BUG           — unexpected silence with no classification

    Each row includes telemetry_source: funnel.jsonl | opportunity_logs_sql | orders | positions
    so the dashboard never lies about which nervous system it's reading.
    """
    import json
    import sqlite3
    from datetime import datetime, timezone, timedelta
    from pathlib import Path

    funnel_path = Path("/app/data/funnel.jsonl")
    db_path = Path("/app/data/paper_bot.db")

    # Strategies we care about (env-enabled live or shadow)
    known = [
        "event_strategy", "sports_strategy", "fade_any",
        "financial_strategy",
        "sniper_strategy", "asset_target_strategy",
    ]

    # ─── Parse funnel.jsonl for last_signal / last_risk_pass / last_order_attempt ───
    last_signal: dict[str, str] = {}
    last_risk_pass: dict[str, str] = {}
    last_risk_reject: dict[str, str] = {}
    last_order_attempt: dict[str, str] = {}
    last_reject_reason: dict[str, str] = {}
    signal_count_24h: dict[str, int] = {s: 0 for s in known}
    risk_pass_count_24h: dict[str, int] = {s: 0 for s in known}
    risk_reject_count_24h: dict[str, int] = {s: 0 for s in known}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    if funnel_path.exists():
        try:
            with funnel_path.open() as fh:
                for line in fh:
                    try:
                        r = json.loads(line.strip())
                    except Exception:
                        continue
                    s = r.get("strategy")
                    if s not in known:
                        continue
                    stage = r.get("stage")
                    ts = r.get("ts")
                    ts_str = ts if isinstance(ts, str) else None
                    if stage == "signal_generated":
                        if not last_signal.get(s) or (ts_str and ts_str > last_signal[s]):
                            last_signal[s] = ts_str or last_signal.get(s, "")
                        if ts_str and ts_str > cutoff:
                            signal_count_24h[s] += 1
                    elif stage == "risk_passed":
                        if not last_risk_pass.get(s) or (ts_str and ts_str > last_risk_pass[s]):
                            last_risk_pass[s] = ts_str or last_risk_pass.get(s, "")
                        # FIX-4 [Claude 47]: count risk_passed only here, NOT also at order_filled.
                        # Prior code double-counted: risk_passed → +1, then order_filled → +1 again.
                        if ts_str and ts_str > cutoff:
                            risk_pass_count_24h[s] += 1
                    elif stage == "order_filled":
                        if not last_order_attempt.get(s) or (ts_str and ts_str > last_order_attempt[s]):
                            last_order_attempt[s] = ts_str or last_order_attempt.get(s, "")
                    elif stage == "risk_rejected":
                        if not last_risk_reject.get(s) or (ts_str and ts_str > last_risk_reject[s]):
                            last_risk_reject[s] = ts_str or last_risk_reject.get(s, "")
                            last_reject_reason[s] = (r.get("reason") or "")[:50]
                        if ts_str and ts_str > cutoff:
                            risk_reject_count_24h[s] += 1
        except Exception:
            pass

    # ─── Parse paper_orders + positions for last activity ───
    last_position_ts: dict[str, str] = {}
    last_order_db_ts: dict[str, str] = {}
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            # paper_orders: most recent per strategy
            for s_name in known:
                row = cur.execute(
                    "SELECT MAX(created_at) FROM paper_orders WHERE strategy = ?",
                    (s_name,),
                ).fetchone()
                if row and row[0]:
                    last_order_db_ts[s_name] = row[0]
            # positions don't have strategy column directly — use bucket
            bucket_to_strategy = {
                "experiment": "event_strategy",  # mostly event, partly sports
                "fade_any_canary": "fade_any",
                "financial_internal": "financial_strategy",
                "core_sniper_live": "sniper_strategy",
                "asset_target_live": "asset_target_strategy",
            }
            for bucket, sname in bucket_to_strategy.items():
                row = cur.execute(
                    "SELECT MAX(updated_at) FROM positions WHERE bucket = ?",
                    (bucket,),
                ).fetchone()
                if row and row[0]:
                    if not last_position_ts.get(sname) or row[0] > last_position_ts[sname]:
                        last_position_ts[sname] = row[0]
            conn.close()
        except Exception:
            pass

    # ─── Sniper / Asset_target also write to opportunity_logs (separate path) ───
    sniper_last_signal: str | None = None
    sniper_last_shadow: str | None = None
    sniper_last_live: str | None = None
    sniper_shadow_low_edge: int = 0
    sniper_unmapped: int = 0
    asset_last_signal: str | None = None
    asset_last_shadow: str | None = None
    asset_last_live: str | None = None
    asset_shadow_low_edge: int = 0
    # Haircut decomposition for asset_target (per [GPT 41])
    asset_recent_raw_edge: float | None = None
    asset_recent_tradable_edge: float | None = None
    asset_recent_threshold: float | None = None
    asset_distance_to_live: float | None = None
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            # Sniper: market_type='moneyline' or league populated and not asset_target
            sniper_rows = cur.execute(
                "SELECT MAX(created_at), MAX(CASE WHEN decision LIKE '%SHADOW%' THEN created_at END), "
                "MAX(CASE WHEN bucket LIKE '%live%' THEN created_at END) "
                "FROM opportunity_logs WHERE market_type IN ('moneyline','spread','total') "
                "OR (league IS NOT NULL AND league != '' AND market_type != 'asset_target')"
            ).fetchone()
            if sniper_rows:
                sniper_last_signal, sniper_last_shadow, sniper_last_live = sniper_rows
            row = cur.execute(
                "SELECT COUNT(*) FROM opportunity_logs WHERE market_type IN ('moneyline','spread','total') "
                "AND decision = 'SHADOW_LOW_EDGE'"
            ).fetchone()
            sniper_shadow_low_edge = (row[0] if row else 0) or 0
            row = cur.execute(
                "SELECT COUNT(*) FROM opportunity_logs WHERE market_type IN ('moneyline','spread','total') "
                "AND mapping_confidence = 'none'"
            ).fetchone()
            sniper_unmapped = (row[0] if row else 0) or 0
            # Asset_target: market_type='asset_target' explicitly
            asset_rows = cur.execute(
                "SELECT MAX(created_at), MAX(CASE WHEN decision LIKE '%SHADOW%' THEN created_at END), "
                "MAX(CASE WHEN bucket LIKE '%live%' THEN created_at END) "
                "FROM opportunity_logs WHERE market_type = 'asset_target'"
            ).fetchone()
            if asset_rows:
                asset_last_signal, asset_last_shadow, asset_last_live = asset_rows
            row = cur.execute(
                "SELECT COUNT(*) FROM opportunity_logs WHERE market_type = 'asset_target' "
                "AND decision = 'SHADOW_LOW_EDGE'"
            ).fetchone()
            asset_shadow_low_edge = (row[0] if row else 0) or 0
            # Haircut decomposition: latest asset_target row with edges
            row = cur.execute(
                "SELECT raw_edge, tradable_edge, reject_reason FROM opportunity_logs "
                "WHERE market_type = 'asset_target' AND raw_edge IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                asset_recent_raw_edge, asset_recent_tradable_edge, _reason = row
                # Parse threshold from reject_reason like 'tradable_edge=0.0480 < 0.1200'
                import re as _re
                m = _re.search(r"<\s*([0-9.]+)", _reason or "")
                if m:
                    try:
                        asset_recent_threshold = float(m.group(1))
                    except Exception:
                        pass
                if asset_recent_tradable_edge is not None and asset_recent_threshold is not None:
                    asset_distance_to_live = round(asset_recent_tradable_edge - asset_recent_threshold, 4)
            conn.close()
        except Exception:
            pass

    # ─── Classify ───
    rows = []
    strategy_meta = {
        "event_strategy":     {"display": "event_strategy",      "live": True,  "type": "internal"},
        "sports_strategy":    {"display": "sports_strategy",     "live": True,  "type": "internal"},
        "fade_any":           {"display": "fade_any_canary",     "live": True,  "type": "internal"},
        "financial_strategy": {"display": "financial_strategy",  "live": False, "type": "internal", "note": "QUARANTINED"},
        "sniper_strategy":    {"display": "sports_sniper",       "live": True,  "type": "external"},
        "asset_target_strategy": {"display": "asset_target",     "live": True,  "type": "external"},
    }

    for s_key, meta in strategy_meta.items():
        sig_ts = last_signal.get(s_key)
        risk_pass_ts = last_risk_pass.get(s_key)
        risk_reject_ts = last_risk_reject.get(s_key)
        order_ts = last_order_db_ts.get(s_key)
        pos_ts = last_position_ts.get(s_key)
        sig24 = signal_count_24h.get(s_key, 0)
        pass24 = risk_pass_count_24h.get(s_key, 0)
        reject24 = risk_reject_count_24h.get(s_key, 0)
        last_reason = last_reject_reason.get(s_key, "")
        telemetry_source = "funnel.jsonl"
        haircut_card: dict | None = None

        # External strategies (sniper / asset) use opportunity_logs path instead of funnel
        if s_key == "sniper_strategy":
            sig_ts = sniper_last_signal
            order_ts = sniper_last_live
            shadow_ts = sniper_last_shadow
            telemetry_source = "opportunity_logs_sql"
        elif s_key == "asset_target_strategy":
            sig_ts = asset_last_signal
            order_ts = asset_last_live
            shadow_ts = asset_last_shadow
            telemetry_source = "opportunity_logs_sql"
            if asset_recent_raw_edge is not None:
                haircut_card = {
                    "raw_edge_pp": round(asset_recent_raw_edge * 100, 2),
                    "tradable_edge_pp": round((asset_recent_tradable_edge or 0) * 100, 2),
                    "haircut_pp": round((asset_recent_raw_edge - (asset_recent_tradable_edge or 0)) * 100, 2),
                    "live_threshold_pp": round((asset_recent_threshold or 0) * 100, 2),
                    "distance_to_live_pp": round((asset_distance_to_live or 0) * 100, 2),
                    "n_shadow_low_edge": asset_shadow_low_edge,
                }
        else:
            shadow_ts = None

        # Status classification per [GPT 41] taxonomy (10 levels)
        # Normalize timestamps for comparison: sqlite uses 'YYYY-MM-DD HH:MM:SS',
        # funnel.jsonl uses ISO with 'T' separator. Strip T and timezone for comparison.
        def _norm(ts: str | None) -> str:
            if not ts:
                return ""
            s = str(ts).replace("T", " ").replace("Z", "")
            # Strip timezone offset like +00:00
            if "+" in s and len(s) >= 25:
                s = s[:s.rfind("+")]
            return s[:19]  # YYYY-MM-DD HH:MM:SS
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        cutoff_norm = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        if pos_ts and _norm(pos_ts) >= cutoff_norm:
            status = "ACTIVELY_TRADING"
        elif order_ts and _norm(order_ts) >= cutoff_norm:
            status = "ARMED_TRADING"
        elif s_key == "asset_target_strategy" and asset_shadow_low_edge > 0:
            status = "SHADOW_LOW_EDGE"
        elif s_key == "sniper_strategy" and sniper_shadow_low_edge > 0:
            status = "SHADOW_LOW_EDGE"
        elif s_key == "sniper_strategy" and sniper_unmapped > sniper_shadow_low_edge * 5:
            status = "UNMAPPED"
        elif s_key in ("sniper_strategy", "asset_target_strategy") and shadow_ts and not order_ts:
            status = "SHADOW_ONLY"
        elif s_key == "financial_strategy" and not meta.get("live"):
            status = "LIVE_DISABLED"
        elif sig24 > 0 and pass24 == 0 and reject24 > 0:
            status = "RISK_REJECTED"
        elif sig24 > 0 and pass24 > 0 and not order_ts:
            status = "EXECUTION_REJECTED"
        elif sig24 > 0:
            status = "FILTERED_SCOPE"
        elif sig_ts and not sig24:
            status = "LIVE_CAPABLE_IDLE"
        elif not sig_ts and sig24 == 0:
            status = "NO_CANDIDATES"
        else:
            status = "PIPELINE_BUG"

        rows.append({
            "strategy": meta["display"],
            "type": meta["type"],
            "live_enabled": meta["live"],
            "note": meta.get("note", ""),
            "status": status,
            "telemetry_source": telemetry_source,
            "signals_24h": sig24,
            "risk_pass_24h": pass24,
            "risk_reject_24h": reject24,
            "last_signal": sig_ts or "",
            "last_risk_pass": risk_pass_ts or "",
            "last_risk_reject": risk_reject_ts or "",
            "last_order": order_ts or "",
            "last_position": pos_ts or "",
            "last_reject_reason": last_reason,
            "haircut_card": haircut_card,
        })

    # Sort: live-trading first, then strategically gated, then dead pipes
    status_order = {
        "ACTIVELY_TRADING": 0, "ARMED_TRADING": 1,
        "LIVE_CAPABLE_IDLE": 2, "SHADOW_LOW_EDGE": 3,
        "SHADOW_ONLY": 4, "UNMAPPED": 5,
        "RISK_REJECTED": 6, "EXECUTION_REJECTED": 7,
        "FILTERED_SCOPE": 8, "LIVE_DISABLED": 9,
        "NO_CANDIDATES": 10, "PIPELINE_BUG": 11,
    }
    rows.sort(key=lambda r: status_order.get(r["status"], 99))
    return {"rows": rows}


def _build_sm_weather_lab() -> dict[str, Any]:
    """Smart Money v2 Weather — candidates + recent follow signals."""
    import json
    from pathlib import Path
    cand_path = Path("/app/data/sm_weather_candidates.jsonl")
    sig_path = Path("/app/data/sm_weather_signals.jsonl")
    out = {"candidates": 0, "signals_7d": 0, "top_candidates": [], "recent_signals": []}
    if cand_path.exists():
        try:
            lines = [json.loads(l) for l in cand_path.open() if l.strip()]
            if lines:
                latest = max(lines, key=lambda x: x.get("ts", 0))
                cands = latest.get("candidates", [])
                out["candidates"] = len(cands)
                out["top_candidates"] = cands[:10]
        except Exception:
            pass
    if sig_path.exists():
        try:
            cutoff = int(__import__("time").time()) - 7 * 86400
            sigs = [json.loads(l) for l in sig_path.open() if l.strip()]
            recent = [s for s in sigs if s.get("ts", 0) >= cutoff]
            out["signals_7d"] = len(recent)
            recent.sort(key=lambda s: -s.get("fill_ts", 0))
            out["recent_signals"] = [
                {
                    "wallet": s.get("wallet", "")[:14] + "...",
                    "side": s.get("side"),
                    "price": s.get("price"),
                    "size": s.get("size"),
                    "outcome": s.get("outcome"),
                    "title": (s.get("title") or "")[:55],
                }
                for s in recent[:15]
            ]
        except Exception:
            pass
    return out


def _build_maker_sim_lab() -> dict[str, Any]:
    """Paper Tail-Maker Sim — quote snapshots + hypothetical fills."""
    import json
    from pathlib import Path
    path = Path("/app/data/paper_maker_sim.jsonl")
    out = {"quote_snapshots": 0, "fills_eval": 0, "filled": 0,
           "median_markout_per_share": 0, "total_pnl_paper": 0, "recent_fills": []}
    if not path.exists():
        return out
    try:
        snaps = []
        fills = []
        for line in path.open():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") == "quote_snapshot":
                snaps.append(rec)
            elif rec.get("kind") == "markout":
                fills.append(rec)
        out["quote_snapshots"] = len(snaps)
        out["fills_eval"] = len(fills)
        filled = [f for f in fills if f.get("would_have_filled")]
        out["filled"] = len(filled)
        markouts = [f["markout_pnl_per_share"] for f in filled if f.get("markout_pnl_per_share") is not None]
        if markouts:
            ms = sorted(markouts)
            out["median_markout_per_share"] = round(ms[len(ms) // 2], 4)
        total = sum(
            (f.get("markout_pnl_per_share") or 0) * (f.get("shares") or 0)
            for f in filled
        )
        out["total_pnl_paper"] = round(total, 2)
        # Most recent fills
        filled.sort(key=lambda f: -f.get("ts", 0))
        out["recent_fills"] = [
            {
                "event_id": f.get("event_id"),
                "market_id": f.get("market_id"),
                "prev_sim_ask": f.get("prev_sim_ask"),
                "cur_best_bid": f.get("cur_best_bid"),
                "elapsed_min": f.get("elapsed_min"),
                "markout_pnl_per_share": f.get("markout_pnl_per_share"),
                "shares": f.get("shares"),
            }
            for f in filled[:10]
        ]
    except Exception as e:
        out["error"] = str(e)[:80]
    return out


def _build_weather_shadow_lab() -> dict[str, Any]:
    """Weather Bucket Shadow per [GPT 30 H4+H3 merged] / [Claude 35] day-1.
    Forecast-based directional signals — single-leg, not basket arb."""
    import json
    from pathlib import Path
    empty = {"events": 0, "signals": 0, "skipped": 0, "top_signals": []}
    path = Path("/app/data/weather_bucket_shadow.jsonl")
    if not path.exists():
        return empty
    lines: list[dict] = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return empty
    if not lines:
        return empty
    # Latest record per event
    by_event: dict[str, dict] = {}
    for r in lines:
        eid = r.get("event_id")
        if not eid:
            continue
        if eid not in by_event or r.get("ts", 0) > by_event[eid].get("ts", 0):
            by_event[eid] = r
    latest = list(by_event.values())
    skipped = [r for r in latest if "skipped" in r]
    valid = [r for r in latest if "skipped" not in r and "error" not in r]
    all_signals: list[dict] = []
    for r in valid:
        for s in r.get("signals", []):
            all_signals.append({
                "event_id": r.get("event_id"),
                "city": r.get("city"),
                "title": (r.get("title") or "")[:50],
                "hours_to_resolution": r.get("hours_to_resolution"),
                "bucket_kind": s.get("bucket_kind"),
                "bucket_temp": s.get("bucket_temp"),
                "forecast_prob": s.get("forecast_prob"),
                "ask_price": s.get("ask_price"),
                "ask_depth": s.get("ask_depth"),
                "edge_buy_pp": s.get("edge_buy_pp"),
            })
    all_signals.sort(key=lambda s: -s.get("edge_buy_pp", 0))
    return {
        "events": len(valid),
        "skipped": len(skipped),
        "signals": len(all_signals),
        "top_signals": all_signals[:15],
    }


def _build_bucket_pnl() -> dict[str, Any]:
    """Per-bucket PnL panel — Danek visibility need.

    Reads positions table grouped by bucket. Closed = quantity=0; Open = quantity>0.
    """
    from sqlalchemy import select, case, func as sa_func
    from app.db import db_session
    from app.models import Position

    out_closed: list[dict] = []
    out_open: list[dict] = []
    try:
        with db_session() as session:
            wins_expr = sa_func.sum(case((Position.realized_pnl > 0, 1), else_=0)).label("wins")
            losses_expr = sa_func.sum(case((Position.realized_pnl < 0, 1), else_=0)).label("losses")
            closed_stmt = select(
                Position.bucket,
                sa_func.count().label("trades"),
                wins_expr,
                losses_expr,
                sa_func.sum(Position.realized_pnl).label("total"),
                sa_func.avg(Position.realized_pnl).label("avg"),
                sa_func.min(Position.realized_pnl).label("worst"),
                sa_func.max(Position.realized_pnl).label("best"),
            ).where(Position.quantity == 0).group_by(Position.bucket)
            for row in session.execute(closed_stmt).all():
                bucket, trades, wins, losses, total, avg, worst, best = row
                wins = int(wins or 0); losses = int(losses or 0)
                wr = round(wins / max(1, wins + losses) * 100, 1)
                out_closed.append({
                    "bucket": bucket or "none",
                    "trades": int(trades),
                    "wins": wins,
                    "losses": losses,
                    "total_pnl": round(float(total or 0), 2),
                    "avg_pnl": round(float(avg or 0), 4),
                    "worst": round(float(worst or 0), 2),
                    "best": round(float(best or 0), 2),
                    "win_rate_pct": wr,
                })
            out_closed.sort(key=lambda r: -r["total_pnl"])

            # Open
            open_stmt = select(
                Position.bucket,
                sa_func.count().label("n"),
                sa_func.sum(Position.unrealized_pnl).label("ur"),
            ).where(Position.quantity > 0).group_by(Position.bucket)
            for row in session.execute(open_stmt).all():
                bucket, n, ur = row
                out_open.append({
                    "bucket": bucket or "none",
                    "open_count": int(n),
                    "unrealized": round(float(ur or 0), 2),
                })
    except Exception as e:
        return {"closed": [], "open": [], "error": str(e)[:80]}

    return {"closed": out_closed, "open": out_open}


def _build_hedge_shadow_lab() -> dict[str, Any]:
    """Hedge_manager shadow simulator results per [GPT 28] / refined [GPT 30].

    Two-fetch persistence + CLEAN_BASKET vs INCOMPLETE_BASKET split.
    """
    import json
    from pathlib import Path
    empty = {
        "total_runs": 0, "events_unique": 0,
        "clean_count": 0, "incomplete_count": 0,
        "stale_count": 0, "error_count": 0,
        "target": 20,
        "clean_top": [], "incomplete_top": [],
    }
    path = Path("/app/data/hedge_shadow.jsonl")
    if not path.exists():
        return empty
    lines: list[dict] = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return empty
    if not lines:
        return empty
    by_event: dict[str, dict] = {}
    for r in lines:
        eid = r.get("event_id")
        if not eid:
            continue
        if eid not in by_event or r.get("ts", 0) > by_event[eid].get("ts", 0):
            by_event[eid] = r
    latest = list(by_event.values())

    def _row(r: dict) -> dict:
        r2 = r.get("round_2") or {}
        r1 = r.get("round_1") or {}
        e1 = r1.get("edge_per_dollar") if r1 else None
        e2 = r2.get("edge_per_dollar") if r2 else None
        return {
            "event_id": r.get("event_id"),
            "title": (r.get("title") or "")[:55],
            "side": r.get("side"),
            "n_legs_ready": r.get("n_legs_ready", 0),
            "n_total": r.get("n_total_markets", 0),
            "n_skipped": r.get("n_skipped", 0),
            "shares": (r2 or r1).get("shares_per_leg"),
            "edge_r1_pp": round((e1 or 0) * 100, 2),
            "edge_r2_pp": round((e2 or 0) * 100, 2),
            "edge_decay_pp": round(((e1 or 0) - (e2 or 0)) * 100, 2),
            "net_profit": (r2 or r1).get("net_profit", 0),
            "cost": (r2 or r1).get("actual_basket_cost", 0),
            "live_eligible": bool(r.get("status_data", {}).get("live_eligible", False)),
            "claimed_edge_pp": r.get("claimed_edge_pp"),
        }

    clean = [_row(r) for r in latest if r.get("status") == "CLEAN_EXECUTABLE"]
    clean.sort(key=lambda r: -r["net_profit"])
    incomplete = [_row(r) for r in latest if r.get("status") == "INCOMPLETE_BASKET"]
    incomplete.sort(key=lambda r: -r["edge_r2_pp"])
    stale = [r for r in latest if r.get("status") == "STALE_EDGE"]
    error_det = [r for r in latest if r.get("status") == "ERROR_DETECTOR"]

    return {
        "total_runs": len(lines),
        "events_unique": len(latest),
        "clean_count": len(clean),
        "incomplete_count": len(incomplete),
        "stale_count": len(stale),
        "error_count": len(error_det),
        "target": 20,
        "clean_top": clean[:15],
        "incomplete_top": incomplete[:10],
    }


def _build_resolution_risk_lab() -> dict[str, Any]:
    """Resolution risk scoring per [GPT 26]: avoid mispricing traps."""
    import json
    from pathlib import Path
    path = Path("/app/data/resolution_risk.jsonl")
    if not path.exists():
        return {"total": 0, "high": 0, "medium": 0, "high_list": []}
    lines = []
    try:
        with path.open() as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return {"total": 0, "high": 0, "medium": 0, "high_list": []}

    # Latest scan only (group by ts, take max ts)
    if not lines:
        return {"total": 0, "high": 0, "medium": 0, "high_list": []}
    latest_ts = max(l["ts"] for l in lines)
    latest = [l for l in lines if l["ts"] == latest_ts]
    high = [l for l in latest if l.get("risk_level") == "HIGH"]
    medium = [l for l in latest if l.get("risk_level") == "MEDIUM"]
    high.sort(key=lambda l: -l.get("score", 0))
    return {
        "total": len(latest),
        "high": len(high),
        "medium": len(medium),
        "high_list": [
            {"score": h["score"], "title": h["title"][:80], "reason": h.get("reason", "")}
            for h in high[:10]
        ],
    }


def _build_pm_fills_lab() -> dict[str, Any]:
    """PM Fills accumulator stats."""
    from sqlalchemy import select, func as sa_func
    from app.db import db_session
    try:
        from app.models import PMFill
    except ImportError:
        return {"total": 0}
    try:
        with db_session() as s:
            n = s.execute(select(sa_func.count(PMFill.id))).scalar_one()
            n_w = s.execute(select(sa_func.count(sa_func.distinct(PMFill.wallet)))).scalar_one()
            n_m = s.execute(select(sa_func.count(sa_func.distinct(PMFill.condition_id)))).scalar_one()
            earliest = s.execute(select(sa_func.min(PMFill.fill_ts))).scalar_one()
            latest = s.execute(select(sa_func.max(PMFill.fill_ts))).scalar_one()
        span_h = round((latest - earliest) / 3600, 1) if earliest and latest else 0
        return {"total": n, "wallets": n_w, "markets": n_m, "span_h": span_h}
    except Exception:
        return {"total": 0}


def _build_asset_target_lab() -> dict[str, Any]:
    """Asset Target Sniper-specific metrics (BTC/ETH/WTI price-targets)."""
    from sqlalchemy import func as sa_func

    with db_session() as session:
        # Decisions distribution для market_type='asset_target'
        rows = session.execute(
            select(
                OpportunityLog.decision,
                OpportunityLog.league,  # asset symbol stored here
                sa_func.count(OpportunityLog.id),
            )
            .where(OpportunityLog.market_type == "asset_target")
            .group_by(OpportunityLog.decision, OpportunityLog.league)
            .order_by(sa_func.count(OpportunityLog.id).desc())
        ).all()
        decisions = [
            {"decision": d, "asset": l, "count": int(c)}
            for d, l, c in rows
        ]

        # Latest 10 unique asset:deadline combinations с deepest data
        latest = session.execute(
            select(
                OpportunityLog.cluster_key,
                OpportunityLog.league,
                OpportunityLog.team_b_id,    # threshold_usd stored here
                OpportunityLog.window_bucket, # deadline_iso stored here
                OpportunityLog.time_scope,    # direction stored here
                OpportunityLog.external_fair, # model_prob
                OpportunityLog.poly_ask,
                OpportunityLog.tradable_edge,
                OpportunityLog.decision,
                sa_func.count(OpportunityLog.id).label("samples"),
            )
            .where(
                OpportunityLog.market_type == "asset_target",
                OpportunityLog.cluster_key.isnot(None),
            )
            .group_by(OpportunityLog.cluster_key)
            .order_by(sa_func.max(OpportunityLog.id).desc())
            .limit(10)
        ).all()

        recent = [
            {
                "cluster": ck,
                "asset": l,
                "threshold": tb,
                "deadline": wb,
                "direction": ts,
                "model_prob": round(float(ef or 0), 4),
                "poly_ask": round(float(pa or 0), 4),
                "tradable_edge": round(float(te or 0), 4) if te is not None else None,
                "decision": d,
                "samples": int(s),
            }
            for ck, l, tb, wb, ts, ef, pa, te, d, s in latest
        ]

        total = int(session.execute(
            select(sa_func.count(OpportunityLog.id))
            .where(OpportunityLog.market_type == "asset_target")
        ).scalar() or 0)

    return {
        "total_logs": total,
        "decisions": decisions,
        "recent_assets": recent,
    }


def _build_sniper_lab() -> dict[str, Any]:
    """Aggregated metrics для opportunity_logs (sniper shadow data)."""
    from sqlalchemy import func as sa_func

    with db_session() as session:
        total = int(session.execute(
            select(sa_func.count(OpportunityLog.id))
        ).scalar() or 0)

        # Decisions распределение
        rows = session.execute(
            select(
                OpportunityLog.decision,
                OpportunityLog.mapping_confidence,
                sa_func.count(OpportunityLog.id),
            )
            .group_by(OpportunityLog.decision, OpportunityLog.mapping_confidence)
        ).all()
        decisions = [
            {"decision": d, "mapping": m, "count": int(c)}
            for d, m, c in rows
        ]

        # Лиги с EXACT mapping
        league_rows = session.execute(
            select(
                OpportunityLog.league,
                sa_func.count(OpportunityLog.id),
            )
            .where(OpportunityLog.mapping_confidence.in_(["exact", "alias"]))
            .group_by(OpportunityLog.league)
        ).all()
        leagues = [
            {"league": l or "?", "count": int(c)}
            for l, c in league_rows
            if l
        ]

        # Forward returns aggregates (только где fwd_ret уже посчитан)
        fwd_rows = session.execute(
            select(
                sa_func.avg(OpportunityLog.fwd_ret_5m),
                sa_func.avg(OpportunityLog.fwd_ret_15m),
                sa_func.avg(OpportunityLog.fwd_ret_60m),
                sa_func.avg(OpportunityLog.fwd_ret_180m),
                sa_func.count(OpportunityLog.fwd_ret_5m),
                sa_func.count(OpportunityLog.fwd_ret_60m),
            )
        ).one()
        avg_5m, avg_15m, avg_60m, avg_180m, n_5m, n_60m = fwd_rows

        # Tradable edge distribution для prospective live signals
        tradable_rows = session.execute(
            select(
                OpportunityLog.window_bucket,
                sa_func.avg(OpportunityLog.tradable_edge),
                sa_func.count(OpportunityLog.id),
            )
            .where(OpportunityLog.tradable_edge.isnot(None))
            .group_by(OpportunityLog.window_bucket)
        ).all()
        windows = [
            {
                "window": w or "?",
                "avg_edge": round(float(e or 0), 4),
                "count": int(c),
            }
            for w, e, c in tradable_rows
        ]

        # Last 5 EXACT matches
        latest = session.execute(
            select(
                OpportunityLog.slug,
                OpportunityLog.league,
                OpportunityLog.team_a_id,
                OpportunityLog.team_b_id,
                OpportunityLog.window_bucket,
                OpportunityLog.minutes_to_event_start,
                OpportunityLog.decision,
            )
            .where(OpportunityLog.mapping_confidence == "exact")
            .order_by(OpportunityLog.id.desc())
            .limit(5)
        ).all()
        recent_exact = [
            {
                "slug": s,
                "league": l,
                "teams": f"{a} vs {b}",
                "window": w,
                "minutes_to_start": round(float(m or 0), 1),
                "decision": d,
            }
            for s, l, a, b, w, m, d in latest
        ]

    return {
        "total_logs": total,
        "decisions": decisions,
        "leagues_with_mapping": leagues,
        "forward_returns": {
            "avg_5m_pct": round(float(avg_5m or 0) * 100, 2),
            "avg_15m_pct": round(float(avg_15m or 0) * 100, 2),
            "avg_60m_pct": round(float(avg_60m or 0) * 100, 2),
            "avg_180m_pct": round(float(avg_180m or 0) * 100, 2),
            "n_5m": int(n_5m or 0),
            "n_60m": int(n_60m or 0),
        },
        "windows": windows,
        "recent_exact_matches": recent_exact,
    }


@router.get("/dashboard/analytics-data")
async def analytics_data() -> dict[str, Any]:
    return _cached("analytics", _ANALYTICS_TTL, _build_analytics_data)


def _build_analytics_data() -> dict[str, Any]:
    with db_session() as session:
        positions = session.execute(select(Position).order_by(Position.id.asc())).scalars().all()
        all_orders = session.execute(select(PaperOrder).order_by(PaperOrder.id.asc())).scalars().all()

    closed = [p for p in positions if p.quantity == 0]

    pos_strategy: dict[str, str] = {}
    pos_category: dict[str, str] = {}
    first_buy_ts: dict[str, datetime] = {}
    last_sell: dict[str, PaperOrder] = {}
    for o in all_orders:
        if o.side == "BUY":
            if o.market_id not in pos_strategy:
                pos_strategy[o.market_id] = o.strategy
                first_buy_ts[o.market_id] = o.created_at
        elif o.side == "SELL":
            last_sell[o.market_id] = o

    rows: list[dict[str, Any]] = []
    for p in closed:
        sell = last_sell.get(p.market_id)
        if sell is None or sell.created_at is None:
            continue
        rows.append(
            {
                "ts": sell.created_at,
                "pnl": float(p.realized_pnl),
                "strategy": pos_strategy.get(p.market_id, "unknown"),
                "note": sell.note or "",
                "avg_price": float(p.avg_price),
                "first_buy_ts": first_buy_ts.get(p.market_id),
                "market_id": p.market_id,
            }
        )
    rows.sort(key=lambda x: x["ts"])

    cum = 0.0
    timeseries: list[dict[str, Any]] = []
    for r in rows:
        cum += r["pnl"]
        timeseries.append(
            {
                "ts": r["ts"].isoformat(),
                "cum_pnl": round(cum, 2),
                "pnl": round(r["pnl"], 2),
                "market_id": r["market_id"],
            }
        )

    pnl_buckets: dict[str, int] = defaultdict(int)
    for r in rows:
        v = r["pnl"]
        if v < -5:
            b = "< -$5"
        elif v < -3:
            b = "-$5..-$3"
        elif v < -1:
            b = "-$3..-$1"
        elif v < 0:
            b = "-$1..$0"
        elif v == 0:
            b = "$0"
        elif v <= 1:
            b = "$0..$1"
        elif v <= 3:
            b = "$1..$3"
        elif v <= 5:
            b = "$3..$5"
        else:
            b = "> $5"
        pnl_buckets[b] += 1

    bucket_order = ["< -$5", "-$5..-$3", "-$3..-$1", "-$1..$0", "$0", "$0..$1", "$1..$3", "$3..$5", "> $5"]
    pnl_buckets_ordered = [{"bucket": b, "count": pnl_buckets.get(b, 0)} for b in bucket_order]

    hour_pnl: dict[int, dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "count": 0.0, "wins": 0.0})
    for r in rows:
        h = r["ts"].hour
        hour_pnl[h]["pnl"] += r["pnl"]
        hour_pnl[h]["count"] += 1
        if r["pnl"] > 0:
            hour_pnl[h]["wins"] += 1
    hour_data = [
        {"hour": h, "pnl": round(hour_pnl.get(h, {"pnl": 0})["pnl"], 2), "count": int(hour_pnl.get(h, {"count": 0})["count"])}
        for h in range(24)
    ]

    strat_data: dict[str, dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "count": 0.0, "wins": 0.0})
    for r in rows:
        s = r["strategy"]
        strat_data[s]["pnl"] += r["pnl"]
        strat_data[s]["count"] += 1
        if r["pnl"] > 0:
            strat_data[s]["wins"] += 1

    holding: list[dict[str, Any]] = []
    for r in rows:
        first = r.get("first_buy_ts")
        if first is None:
            continue
        age_min = (r["ts"] - first).total_seconds() / 60.0
        holding.append({"age_min": round(age_min, 1), "pnl": round(r["pnl"], 2)})

    holding_buckets_def = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 120), (120, 9999)]
    holding_buckets: list[dict[str, Any]] = []
    for lo, hi in holding_buckets_def:
        sub = [h for h in holding if lo <= h["age_min"] < hi]
        wins = [h for h in sub if h["pnl"] > 0]
        losses = [h for h in sub if h["pnl"] < 0]
        label = f"{lo}-{hi}m" if hi < 9999 else f">{lo}m"
        holding_buckets.append(
            {
                "label": label,
                "count": len(sub),
                "wins": len(wins),
                "losses": len(losses),
                "pnl": round(sum(h["pnl"] for h in sub), 2),
            }
        )

    total_pnl = round(sum(r["pnl"] for r in rows), 2)
    win_count = len([r for r in rows if r["pnl"] > 0])
    loss_count = len([r for r in rows if r["pnl"] < 0])

    return {
        "summary": {
            "closed": len(rows),
            "wins": win_count,
            "losses": loss_count,
            "total_pnl": total_pnl,
            "best": round(max((r["pnl"] for r in rows), default=0.0), 2),
            "worst": round(min((r["pnl"] for r in rows), default=0.0), 2),
        },
        "timeseries": timeseries,
        "pnl_buckets": pnl_buckets_ordered,
        "hour_pnl": hour_data,
        "by_strategy": [
            {
                "name": s,
                "pnl": round(d["pnl"], 2),
                "count": int(d["count"]),
                "wins": int(d["wins"]),
            }
            for s, d in sorted(strat_data.items(), key=lambda x: -x[1]["pnl"])
        ],
        "holding_buckets": holding_buckets,
        "scatter": [
            {"age_min": h["age_min"], "pnl": h["pnl"]}
            for h in holding
        ],
    }


COMMON_STYLE = """
:root {
  --bg: #0a0e14;
  --bg-2: #0f1620;
  --panel: rgba(22, 27, 34, 0.8);
  --panel-solid: #161b22;
  --border: #2a3440;
  --border-hover: #3a4a5a;
  --text: #e6edf3;
  --muted: #8b96a3;
  --green: #4ade80;
  --green-dim: rgba(74, 222, 128, 0.15);
  --red: #f87171;
  --red-dim: rgba(248, 113, 113, 0.15);
  --amber: #fbbf24;
  --amber-dim: rgba(251, 191, 36, 0.15);
  --accent: #60a5fa;
  --accent-dim: rgba(96, 165, 250, 0.15);
  --purple: #c084fc;
  --gradient: linear-gradient(135deg, #60a5fa 0%, #c084fc 100%);
}
* { box-sizing: border-box; }
body {
  font-family: 'SF Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--bg);
  background-image:
    radial-gradient(ellipse at top left, rgba(96, 165, 250, 0.05), transparent 50%),
    radial-gradient(ellipse at bottom right, rgba(192, 132, 252, 0.05), transparent 50%);
  color: var(--text);
  margin: 0;
  padding: 24px;
  font-size: 13px;
  line-height: 1.6;
  min-height: 100vh;
}
h1, h2 { font-weight: 600; margin: 0; letter-spacing: -0.01em; }
h1 {
  font-size: 22px;
  background: var(--gradient);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
h2 { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 12px; font-weight: 500; }
.grid { display: grid; gap: 18px; max-width: 1500px; margin: 0 auto; }
.row { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
.row-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.row-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
@media (max-width: 900px) { .row-2, .row-3 { grid-template-columns: 1fr; } }
.panel {
  background: var(--panel);
  backdrop-filter: blur(10px);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px;
  overflow-x: auto;
  transition: border-color 0.2s;
}
.panel:hover { border-color: var(--border-hover); }
.kpi { display: flex; flex-direction: column; gap: 4px; }
.kpi .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 500; }
.kpi .value { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
.kpi.big .value { font-size: 32px; }
.green { color: var(--green); }
.red { color: var(--red); }
.amber { color: var(--amber); }
.muted { color: var(--muted); }
.accent { color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; text-transform: uppercase; font-size: 10px; letter-spacing: 0.06em; }
tr:hover td { background: rgba(96, 165, 250, 0.03); }
.note { color: var(--muted); font-size: 11px; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 11px; font-weight: 500; }
.pill.green { background: var(--green-dim); color: var(--green); }
.pill.red { background: var(--red-dim); color: var(--red); }
.pill.amber { background: var(--amber-dim); color: var(--amber); }
.pill.blue { background: var(--accent-dim); color: var(--accent); }
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 12px;
}
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.dot.on { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
.dot.off { background: var(--red); box-shadow: 0 0 8px var(--red); }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.refresh { color: var(--muted); font-size: 11px; }
.subgrid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.subgrid .kpi .value { font-size: 18px; }
button, .btn {
  background: var(--panel-solid);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 14px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  transition: all 0.15s;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
button:hover, .btn:hover { border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }
.btn.primary { background: var(--gradient); color: white; border: none; }
.btn.primary:hover { color: white; opacity: 0.9; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.scroll { max-height: 360px; overflow-y: auto; }
.scroll::-webkit-scrollbar { width: 6px; }
.scroll::-webkit-scrollbar-track { background: var(--bg-2); }
.scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.nav { display: flex; gap: 8px; }
.nav .btn { padding: 6px 12px; }
.nav .btn.active { background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }
.chart-wrap { position: relative; height: 280px; }
.chart-wrap.tall { height: 360px; }
"""


DASHBOARD_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Polymarket Bot · Live</title>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<style>__STYLE__</style>
</head>
<body>
<div class=\"grid\">
  <div class=\"header-row\">
    <h1>⚡ Polymarket Bot</h1>
    <div class=\"nav\">
      <a class=\"btn active\" href=\"/dashboard\">📊 Live</a>
      <a class=\"btn\" href=\"/dashboard/analytics\">📈 Analytics</a>
    </div>
    <div class=\"refresh\" id=\"refresh\">loading…</div>
  </div>

  <!-- ─── BIG TOTAL PnL HEADER ─── -->
  <div class=\"panel\" style=\"background:linear-gradient(135deg,#1a3a1a,#0a1a2a);border:2px solid #3a7a3a;padding:18px;\">
    <div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:18px;align-items:center;\">
      <div>
        <div class=\"muted\" style=\"font-size:11px;letter-spacing:1px;\">CURRENT BALANCE</div>
        <div style=\"font-size:30px;font-weight:700;\" id=\"hd-balance\">—</div>
        <div class=\"muted\" style=\"font-size:11px;\" id=\"hd-balance-sub\">starting $100.00</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;letter-spacing:1px;\">TOTAL PnL</div>
        <div style=\"font-size:30px;font-weight:700;\" id=\"hd-totalpnl\">—</div>
        <div class=\"muted\" style=\"font-size:11px;\"><span id=\"hd-roi\">—</span> ROI</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;letter-spacing:1px;\">REALIZED</div>
        <div style=\"font-size:22px;font-weight:600;\" id=\"hd-realized\">—</div>
        <div class=\"muted\" style=\"font-size:11px;\"><span id=\"hd-today\">—</span> last 24h</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;letter-spacing:1px;\">UNREALIZED</div>
        <div style=\"font-size:22px;font-weight:600;\" id=\"hd-unrealized\">—</div>
        <div class=\"muted\" style=\"font-size:11px;\">open positions</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;letter-spacing:1px;\">STRATEGIES</div>
        <div style=\"font-size:22px;font-weight:600;\"><span class=\"green\" id=\"hd-active\">—</span> live · <span class=\"red\" id=\"hd-quar\">—</span> quar</div>
        <div class=\"muted\" style=\"font-size:11px;\">9 cron shadow loggers active</div>
      </div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:12px;margin:6px 0;color:#9fcfff;letter-spacing:1px;\">PER-STRATEGY BREAKDOWN</h3>
      <table id=\"hd-buckets\" style=\"font-size:11px;width:100%;\"><thead><tr>
        <th>Bucket</th><th>Status</th><th>Closed</th><th>WR</th><th>Realized</th><th>Open</th><th>Unrealized</th><th>Total</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <!-- ─── STRATEGY HEALTH (per [GPT 39]) ─── -->
  <div class=\"panel\" style=\"background:linear-gradient(135deg,#2a1a3a,#1a0a2a);border:2px solid #6a4a8a;padding:14px;\">
    <h2 style=\"margin:0 0 10px 0;font-size:16px;\">🩺 Strategy Health — pulse before story</h2>
    <p class=\"muted\" style=\"font-size:11px;margin:0 0 12px 0;font-style:italic;\">
      Per [GPT 39]: zero trades is not a status. Every armed surface must show its last-seen timestamps,
      otherwise the dashboard is ornamental.
    </p>
    <table id=\"sh-table\" style=\"font-size:11px;width:100%;\"><thead><tr>
      <th>Strategy</th><th>Status</th><th>Sig 24h</th><th>Pass 24h</th><th>Reject 24h</th>
      <th>Last signal</th><th>Last risk pass</th><th>Last order</th><th>Last position</th><th>Reject reason</th>
    </tr></thead><tbody></tbody></table>
    <p class=\"muted\" style=\"font-size:10px;margin-top:8px;\">
      ACTIVELY_TRADING (green) = trades + positions in 24h • ARMED_TRADING = orders only •
      RISK_REJECTED (red) = signals all blocked by risk_manager • EXECUTION_REJECTED = router accepted but no order •
      SHADOW_ONLY (amber) = passes gates but never live • FILTERED = signals filtered before risk •
      NO_CANDIDATES = nothing reached scanner • PIPELINE_BUG = unexpected silence
    </p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f1a,#1a1a2f);border:1px solid #2d4a2d;\">
    <h2 style=\"margin-top:0;\">📜 History · Reset 2026-04-30</h2>
    <div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;\">
      <div>
        <div class=\"muted\" style=\"font-size:11px;\">BASELINE (Apr 27→29, 38h)</div>
        <div style=\"font-size:18px;color:#7be07b;font-weight:600;\">+15.7% ROI</div>
        <div class=\"muted\" style=\"font-size:12px;\">16W / 11L · win rate 59% · profit factor 1.66</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;\">PEAK BALANCE</div>
        <div style=\"font-size:18px;color:#7be07b;font-weight:600;\">$109.04</div>
        <div class=\"muted\" style=\"font-size:12px;\">29 Apr 20:34 UTC · после first fix</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;\">CURRENT VERSION</div>
        <div style=\"font-size:18px;color:#e0c870;font-weight:600;\">v∞ + anti-falling-knife</div>
        <div class=\"muted\" style=\"font-size:12px;\">momentum guard + hard_stop spread sanity</div>
      </div>
      <div>
        <div class=\"muted\" style=\"font-size:11px;\">SPORTS STRATEGY</div>
        <div style=\"font-size:18px;color:#7be07b;font-weight:600;\">2W / 0L baseline</div>
        <div class=\"muted\" style=\"font-size:12px;\">+$4.38 total · best: TP +43.8%</div>
      </div>
    </div>
  </div>

  <div class=\"row\">
    <div class=\"panel\">
      <h2>Status</h2>
      <div class=\"kpi big\">
        <span class=\"label\">Mode</span>
        <span class=\"value\" id=\"mode\">—</span>
      </div>
      <div style=\"margin-top: 12px;\" id=\"paused-state\"></div>
      <div class=\"actions\" style=\"margin-top: 14px;\">
        <button onclick=\"call('/pause','POST')\">⏸ Pause</button>
        <button onclick=\"call('/resume','POST')\">▶ Resume</button>
      </div>
    </div>
    <div class=\"panel\">
      <h2>Balance</h2>
      <div class=\"kpi big\"><span class=\"label\">Current</span><span class=\"value\" id=\"balance\">—</span></div>
      <div style=\"margin-top: 8px;\" class=\"muted\">start: <span id=\"initial-balance\">—</span></div>
    </div>
    <div class=\"panel\">
      <h2>PnL</h2>
      <div class=\"kpi big\"><span class=\"label\">Total</span><span class=\"value\" id=\"total-pnl\">—</span></div>
      <div class=\"subgrid\" style=\"margin-top: 10px;\">
        <div class=\"kpi\"><span class=\"label\">Realized</span><span class=\"value\" id=\"realized\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">Unrealized</span><span class=\"value\" id=\"unrealized\">—</span></div>
      </div>
      <div style=\"margin-top: 10px;\"><span class=\"muted\">ROI:</span> <span id=\"roi\">—</span></div>
    </div>
    <div class=\"panel\">
      <h2>Performance</h2>
      <div class=\"subgrid\">
        <div class=\"kpi\"><span class=\"label\">Win Rate</span><span class=\"value\" id=\"win-rate\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">R:R</span><span class=\"value\" id=\"rr\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">Avg Win</span><span class=\"value green\" id=\"avg-win\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">Avg Loss</span><span class=\"value red\" id=\"avg-loss\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">Closed</span><span class=\"value\" id=\"closed\">—</span></div>
        <div class=\"kpi\"><span class=\"label\">W / L</span><span class=\"value\" id=\"wl\">—</span></div>
      </div>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2f1a2f,#1a1a2f);border:1px solid #4a2d4a;\">
    <h2 style=\"margin-top:0;\">🎯 Sniper Lab — External Edge Shadow</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total Logs</span><span class=\"value\" id=\"sniper-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">EXACT/ALIAS Mapped</span><span class=\"value\" id=\"sniper-mapped\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">FWD 5m</span><span class=\"value\" id=\"sniper-fwd-5m\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">FWD 60m</span><span class=\"value\" id=\"sniper-fwd-60m\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">FWD 180m</span><span class=\"value\" id=\"sniper-fwd-180m\">—</span></div>
    </div>
    <div style=\"margin-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px;\">
      <div>
        <h3 style=\"font-size:13px;margin:6px 0;\">By League</h3>
        <table id=\"sniper-leagues\" style=\"font-size:12px;\"><thead><tr>
          <th>League</th><th>Mapped</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div>
        <h3 style=\"font-size:13px;margin:6px 0;\">By Window</h3>
        <table id=\"sniper-windows\" style=\"font-size:12px;\"><thead><tr>
          <th>Window</th><th>Avg Edge</th><th>Count</th>
        </tr></thead><tbody></tbody></table>
      </div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Latest EXACT Matches</h3>
      <table id=\"sniper-exact\" style=\"font-size:12px;\"><thead><tr>
        <th>Slug</th><th>Teams</th><th>Window</th><th>Mins to Start</th><th>Decision</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f2f,#1a1a2f);border:1px solid #2d4a4a;\">
    <h2 style=\"margin-top:0;\">💎 Asset Target Lab — Crypto/Commodity Price Targets</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total Logs</span><span class=\"value\" id=\"asset-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Tracked Markets</span><span class=\"value\" id=\"asset-tracked\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">By Decision × Asset</h3>
      <table id=\"asset-decisions\" style=\"font-size:12px;\"><thead><tr>
        <th>Decision</th><th>Asset</th><th>Count</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Latest Asset Targets (model vs Polymarket)</h3>
      <table id=\"asset-recent\" style=\"font-size:11px;\"><thead><tr>
        <th>Asset</th><th>Threshold</th><th>Direction</th><th>Deadline</th><th>Model P</th><th>Poly Ask</th><th>Tradable Edge</th><th>Decision</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2f1a2f,#1a1a2f);border:1px solid #4a2d4a;\">
    <h2 style=\"margin-top:0;\">⚡ Fade-Any Lab — 6-10pp sweet spot edge discovery</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total signals</span><span class=\"value\" id=\"fade-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Sweet spot 6-10pp</span><span class=\"value\" id=\"fade-sweet\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Target for canary</span><span class=\"value\" id=\"fade-target\">30</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Magnitude segmentation</h3>
      <table id=\"fade-mag\" style=\"font-size:12px;\"><thead><tr>
        <th>Magnitude</th><th>Count</th><th>Note</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">External confirmation (Manifold)</h3>
      <table id=\"fade-ec\" style=\"font-size:12px;\"><thead><tr>
        <th>State</th><th>Count</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Recent signals (last 15)</h3>
      <table id=\"fade-recent\" style=\"font-size:11px;\"><thead><tr>
        <th>Title</th><th>Δ5m pp</th><th>Fade</th><th>Edge pp</th><th>Manifold</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f1a,#1a1a2f);border:1px solid #2d4a2d;\">
    <h2 style=\"margin-top:0;\">🛰️ Cross-Platform Radar — PM ↔ Manifold gaps</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total observations</span><span class=\"value\" id=\"radar-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Big gaps (≥6pp)</span><span class=\"value\" id=\"radar-big\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top divergences</h3>
      <table id=\"radar-top\" style=\"font-size:11px;\"><thead><tr>
        <th>Title</th><th>PM YES</th><th>Manifold YES</th><th>Gap pp</th><th>Sim</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2f2a1a,#1a1a2f);border:1px solid #4a4a2d;\">
    <h2 style=\"margin-top:0;\">🎁 Rewards Lab — Maker rebate opportunities</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Markets analyzed</span><span class=\"value\" id=\"rew-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Qualified ≥50</span><span class=\"value\" id=\"rew-qual\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top opportunity scores</h3>
      <table id=\"rew-top\" style=\"font-size:11px;\"><thead><tr>
        <th>Score</th><th>Title</th><th>Spread pp</th><th>Mid</th><th>Existing depth $</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f3a,#1a1a2f);border:1px solid #2d4a5a;\">
    <h2 style=\"margin-top:0;\">💰 Neg-Risk Arbitrage Scanner — multi-outcome event mispricing</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Events scanned</span><span class=\"value\" id=\"arb-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Arb candidates</span><span class=\"value green\" id=\"arb-cand\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top arb opportunities (≥3pp deviation от 1.0)</h3>
      <table id=\"arb-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Edge pp</th><th>Signal</th><th>Title</th><th>#Mkts</th><th>Min liq</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <p class=\"muted\" style=\"font-size:10px;margin-top:10px;\">⚠️ Per [GPT 26/28]: scanner only — see Hedge Shadow lab below for executable validation gate.</p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f1a,#0f1a1a);border:1px solid #2d5a2d;\">
    <h2 style=\"margin-top:0;\">🛡️ Hedge Manager Shadow — two-fetch executable gate per [GPT 30]</h2>
    <p class=\"muted\" style=\"font-size:10px;color:#aaa;margin:4px 0;\"><b>STATUS:</b> taker live PAUSED, feature extraction only — edge half-life observed in minutes</p>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(120px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total runs</span><span class=\"value\" id=\"hs-runs\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Unique events</span><span class=\"value\" id=\"hs-events\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">CLEAN executable</span><span class=\"value green\" id=\"hs-clean\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">INCOMPLETE</span><span class=\"value amber\" id=\"hs-incomplete\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">STALE_EDGE</span><span class=\"value\" id=\"hs-stale\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">ERROR_DETECTOR</span><span class=\"value red\" id=\"hs-errdet\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Target (≥)</span><span class=\"value\" id=\"hs-target\">20</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;color:#7fcf7f;\">✅ CLEAN baskets — fully covered, persistent edge ≥2pp</h3>
      <table id=\"hs-clean-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Side</th><th>Title</th><th>Legs</th><th>K</th><th>edge_r1</th><th>edge_r2</th><th>decay</th><th>Net $</th><th>Live?</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;color:#dfb070;\">⚠️ INCOMPLETE baskets — partial coverage, research-only</h3>
      <table id=\"hs-incomplete-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Side</th><th>Title</th><th>Legs/Tot</th><th>K</th><th>edge_r2</th><th>Cost</th><th>Claimed pp</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <p class=\"muted\" style=\"font-size:10px;margin-top:10px;\">📊 Live $1 canary requires: ≥20 CLEAN + skipped=0 + edge ≥3pp persistent + manual group verify on first 10. INCOMPLETE never live.</p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a1f2e,#0f1822);border:1px solid #2d3a5a;\">
    <h2 style=\"margin-top:0;\">🌡️ Weather Bucket Shadow — forecast-vs-market per [GPT 30 H4+H3]</h2>
    <p class=\"muted\" style=\"font-size:10px;color:#dfb070;margin:4px 0;\"><b>STATUS:</b> external-fair shadow, BUG-RISK HIGH until first 5 manual verifications. Day-1 +51pp Tokyo signal traced to GMT vs station-local timezone mismatch (FIXED).</p>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Events scanned</span><span class=\"value\" id=\"ws-events\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Skipped (no fc)</span><span class=\"value\" id=\"ws-skipped\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Signals (≥3pp)</span><span class=\"value green\" id=\"ws-signals\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top forecast-vs-PM signals (BUY YES if forecast_prob > ask)</h3>
      <table id=\"ws-list\" style=\"font-size:11px;\"><thead><tr>
        <th>City</th><th>Bucket</th><th>Title</th><th>Forecast p</th><th>Ask</th><th>Depth</th><th>edge BUY</th><th>hrs</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <p class=\"muted\" style=\"font-size:10px;margin-top:10px;\">🌍 Source: Open-Meteo GFS05 30-member ensemble. Single-leg directional, NOT basket arb. Pass criteria: ≥30 signals, median edge ≥3pp, Brier &lt; PM mid.</p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2f2a1a,#1f1a0a);border:1px solid #5a4a2d;\">
    <h2 style=\"margin-top:0;\">🐳 Smart Money v2 — Weather Specialists per [Claude 35] Day-2</h2>
    <p class=\"muted\" style=\"font-size:10px;color:#aaa;margin:4px 0;\"><b>STATUS:</b> WATCHLIST, not signal. Live copy gated until: ≥14d data + ≥30 resolved fills + +2pp median + ≥55% hit + beats naive forecast.</p>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Candidates</span><span class=\"value\" id=\"sm-cands\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Signals (7d)</span><span class=\"value green\" id=\"sm-sigs\">—</span></div>
    </div>
    <div style=\"margin-top:12px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top weather-specialist wallets (≥3 trades, $5-$500 avg, active &lt;14d)</h3>
      <table id=\"sm-cand-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Wallet</th><th>Trades</th><th>Events</th><th>Avg $</th><th>Total $</th><th>Last seen</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:12px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Recent (7d) follow signals</h3>
      <table id=\"sm-sig-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Wallet</th><th>Side</th><th>Price</th><th>Size</th><th>Outcome</th><th>Title</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <p class=\"muted\" style=\"font-size:10px;margin-top:10px;\">📊 SM v1 failed broad — v2 narrowed to weather. NO live copy. Resolution outcomes accumulate over 14d.</p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2f2a,#0a1a17);border:1px solid #2d5a4a;\">
    <h2 style=\"margin-top:0;\">🎯 Paper Tail-Maker Sim per [GPT 32 H1]</h2>
    <p class=\"muted\" style=\"font-size:10px;color:#aaa;margin:4px 0;\"><b>STATUS:</b> proxy fills = UPPER BOUND (cur_best_bid &ge; sim_ask, over-counts). Strict fills = trade-flow validated (TRADE_THROUGH only). Trust strict.</p>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(120px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Quote snapshots</span><span class=\"value\" id=\"mk-snaps\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Fills evaluated</span><span class=\"value\" id=\"mk-fevals\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Would-fill</span><span class=\"value green\" id=\"mk-filled\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Median markout/sh</span><span class=\"value\" id=\"mk-markout\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Total paper PnL</span><span class=\"value\" id=\"mk-pnl\">—</span></div>
    </div>
    <div style=\"margin-top:12px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Recent simulated fills (would-have-filled events)</h3>
      <table id=\"mk-fills\" style=\"font-size:11px;\"><thead><tr>
        <th>Event</th><th>Market</th><th>Prev sim ask</th><th>Cur best bid</th><th>Elapsed</th><th>Markout/sh</th><th>Shares</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <p class=\"muted\" style=\"font-size:10px;margin-top:10px;\">⚠️ Paper sim of tail-bucket maker quoting (1 tick above best ask, $1 size). Pass: ≥30 fills, +EV after adverse selection.</p>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2a1a2f,#1a0f1f);border:1px solid #4a2d5a;\">
    <h2 style=\"margin-top:0;\">💰 Per-Bucket PnL — strategy-level performance</h2>
    <div style=\"margin-top:8px;\">
      <h3 style=\"font-size:13px;margin:6px 0;color:#9fcfff;\">Closed positions by bucket (all-time)</h3>
      <table id=\"bp-closed\" style=\"font-size:11px;\"><thead><tr>
        <th>Bucket</th><th>Trades</th><th>W/L</th><th>WR</th><th>Total $</th><th>Avg $</th><th>Worst</th><th>Best</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div style=\"margin-top:12px;\">
      <h3 style=\"font-size:13px;margin:6px 0;color:#fbb;\">Open positions by bucket</h3>
      <table id=\"bp-open\" style=\"font-size:11px;\"><thead><tr>
        <th>Bucket</th><th>Open</th><th>Unrealized $</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#2f1a1a,#1a1a2f);border:1px solid #4a2d2d;\">
    <h2 style=\"margin-top:0;\">⚠️ Resolution Risk Filter — wording-based avoidance</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Markets scored</span><span class=\"value\" id=\"rr-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">HIGH risk (avoid)</span><span class=\"value red\" id=\"rr-high\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">MEDIUM risk</span><span class=\"value amber\" id=\"rr-medium\">—</span></div>
    </div>
    <div style=\"margin-top:14px;\">
      <h3 style=\"font-size:13px;margin:6px 0;\">Top HIGH-risk markets (subjective wording → resolution gap)</h3>
      <table id=\"rr-list\" style=\"font-size:11px;\"><thead><tr>
        <th>Score</th><th>Title</th><th>Reason</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <div class=\"panel\" style=\"background:linear-gradient(90deg,#1a2a2f,#1a1a2f);border:1px solid #2d4a4a;\">
    <h2 style=\"margin-top:0;\">📥 PM Fills Logger — Smart Money data accumulator</h2>
    <div class=\"subgrid\" style=\"grid-template-columns:repeat(auto-fit,minmax(140px,1fr));\">
      <div class=\"kpi\"><span class=\"label\">Total fills</span><span class=\"value\" id=\"pmf-total\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Wallets</span><span class=\"value\" id=\"pmf-wallets\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Markets</span><span class=\"value\" id=\"pmf-markets\">—</span></div>
      <div class=\"kpi\"><span class=\"label\">Span (h)</span><span class=\"value\" id=\"pmf-span\">—</span></div>
    </div>
  </div>

  <div class=\"panel\">
    <h2>📌 Open Positions <span class=\"muted\" id=\"open-count\"></span></h2>
    <table id=\"open-table\"><thead><tr>
      <th>Market</th><th>Strategy</th><th>Qty</th><th>Avg</th><th>Realized</th><th>Unrealized</th><th>Created</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class=\"row\">
    <div class=\"panel\">
      <h2>🚪 Exit Reasons</h2>
      <table id=\"exit-table\"><thead><tr>
        <th>Kind</th><th>Count</th><th>PnL</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class=\"panel\">
      <h2>🎯 By Strategy</h2>
      <table id=\"strat-table\"><thead><tr>
        <th>Strategy</th><th>Orders</th><th>W</th><th>L</th><th>PnL</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class=\"panel\">
      <h2>🤖 AI Activity</h2>
      <div id=\"ai-fp\" class=\"muted\">—</div>
      <h2 style=\"margin-top: 16px;\">🛡 AI Veto</h2>
      <div id=\"ai-veto\" class=\"muted\">—</div>
    </div>
  </div>

  <div class=\"panel\">
    <h2>📜 Recent Trades <span class=\"muted\" id=\"trades-count\"></span></h2>
    <div class=\"scroll\">
    <table id=\"trades-table\"><thead><tr>
      <th>Time</th><th>Side</th><th>Strategy</th><th>Market</th><th>Price</th><th>Size</th><th>Reason / Note</th>
    </tr></thead><tbody></tbody></table>
    </div>
  </div>

</div>

<script>
const fmtMoney = v => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
const fmtPct = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
const cls = v => v == null ? '' : v > 0 ? 'green' : v < 0 ? 'red' : 'muted';

async function call(path, method) {
  try {
    const r = await fetch(path, { method });
    if (r.ok) load();
  } catch(e) { console.error(e); }
}

function row(cells) {
  return '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>';
}

async function load() {
  try {
    const r = await fetch('/dashboard/data');
    const d = await r.json();
    const rt = d.runtime;
    document.getElementById('mode').textContent = rt.mode;
    document.getElementById('balance').textContent = '$' + (rt.current_balance ?? 0).toFixed(2);
    document.getElementById('initial-balance').textContent = '$' + (rt.initial_balance ?? 0).toFixed(2);
    const t = rt.total_pnl;
    const tEl = document.getElementById('total-pnl');
    tEl.textContent = fmtMoney(t); tEl.className = 'value ' + cls(t);
    const r1 = document.getElementById('realized'); r1.textContent = fmtMoney(rt.realized_pnl); r1.className = 'value ' + cls(rt.realized_pnl);
    const r2 = document.getElementById('unrealized'); r2.textContent = fmtMoney(rt.unrealized_pnl); r2.className = 'value ' + cls(rt.unrealized_pnl);
    const roi = document.getElementById('roi'); roi.textContent = fmtPct(rt.roi_pct); roi.className = cls(rt.roi_pct);
    document.getElementById('paused-state').innerHTML = rt.paused
      ? '<span class=\"dot off\"></span><span class=\"red\">PAUSED</span>'
      : '<span class=\"dot on\"></span><span class=\"green\">RUNNING</span>';

    const s = d.summary;
    document.getElementById('win-rate').textContent = s.win_rate_pct + '%';
    document.getElementById('rr').textContent = s.rr_ratio.toFixed(2);
    document.getElementById('avg-win').textContent = fmtMoney(s.avg_win);
    document.getElementById('avg-loss').textContent = fmtMoney(s.avg_loss);
    document.getElementById('closed').textContent = s.closed;
    document.getElementById('wl').textContent = s.wins + ' / ' + s.losses;

    // ─── BIG Total PnL header card ───
    if (d.total_pnl_summary) {
      const tps = d.total_pnl_summary;
      const balEl = document.getElementById('hd-balance');
      balEl.textContent = '$' + tps.current_balance.toFixed(2);
      balEl.className = (tps.current_balance >= tps.starting_balance) ? 'green' : 'red';
      document.getElementById('hd-balance-sub').textContent = 'starting $' + tps.starting_balance.toFixed(2);

      const totEl = document.getElementById('hd-totalpnl');
      totEl.textContent = (tps.total_pnl >= 0 ? '+$' : '-$') + Math.abs(tps.total_pnl).toFixed(2);
      totEl.className = (tps.total_pnl >= 0 ? 'green' : 'red');

      const roiEl = document.getElementById('hd-roi');
      roiEl.textContent = (tps.roi_pct >= 0 ? '+' : '') + tps.roi_pct.toFixed(2) + '%';
      roiEl.className = (tps.roi_pct >= 0 ? 'green' : 'red');

      const realEl = document.getElementById('hd-realized');
      realEl.textContent = (tps.total_realized >= 0 ? '+$' : '-$') + Math.abs(tps.total_realized).toFixed(2);
      realEl.className = (tps.total_realized >= 0 ? 'green' : 'red');

      const todayEl = document.getElementById('hd-today');
      todayEl.textContent = (tps.today_realized >= 0 ? '+$' : '-$') + Math.abs(tps.today_realized).toFixed(2);
      todayEl.className = (tps.today_realized >= 0 ? 'green' : 'red');

      const urlEl = document.getElementById('hd-unrealized');
      urlEl.textContent = (tps.total_unrealized >= 0 ? '+$' : '-$') + Math.abs(tps.total_unrealized).toFixed(2);
      urlEl.className = (tps.total_unrealized >= 0 ? 'green' : 'red');

      document.getElementById('hd-active').textContent = tps.n_active_strategies || 0;
      document.getElementById('hd-quar').textContent = tps.n_quarantined || 0;

      const statusBadge = (st) => {
        if (st === 'LIVE_workhorse') return '<span class=\"pill green\">LIVE</span>';
        if (st === 'LIVE_canary' || st === 'LIVE_canary_telemetry') return '<span class=\"pill blue\">CANARY</span>';
        if (st === 'QUARANTINED') return '<span class=\"pill\" style=\"background:#5a2d2d;color:#ff8888;\">QUARANTINED</span>';
        if (st === 'PAPER_legacy') return '<span class=\"pill\" style=\"background:#3a3a5a;color:#aaa;\">PAPER</span>';
        return '<span class=\"muted\">?</span>';
      };
      document.getElementById('hd-buckets').querySelector('tbody').innerHTML =
        (tps.by_bucket || []).map(b => row([
          '<span style=\"font-weight:bold;\">' + b.bucket + '</span>',
          statusBadge(b.status),
          b.n_closed,
          (b.win_rate_pct >= 50 ? '<span class=\"green\">' : '<span class=\"red\">') + (b.win_rate_pct || 0).toFixed(0) + '%</span>',
          (b.realized >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$') + Math.abs(b.realized).toFixed(2) + '</span>',
          b.n_open,
          (b.unrealized >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$') + Math.abs(b.unrealized).toFixed(2) + '</span>',
          (b.total >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$') + Math.abs(b.total).toFixed(2) + '</span>',
        ])).join('') || '<tr><td colspan=\"8\" class=\"muted\">no buckets yet</td></tr>';
    }

    // ─── STRATEGY HEALTH (per [GPT 41] expanded taxonomy) ───
    if (d.strategy_health) {
      const fmt = ts => ts ? ts.replace('T', ' ').slice(0, 19) : '<span class=\"muted\">never</span>';
      const healthBadge = st => {
        if (st === 'ACTIVELY_TRADING')   return '<span class=\"pill green\">ACTIVE</span>';
        if (st === 'ARMED_TRADING')      return '<span class=\"pill blue\">ARMED</span>';
        if (st === 'LIVE_CAPABLE_IDLE')  return '<span class=\"pill\" style=\"background:#3a4a5a;color:#9fc4ff;\">IDLE_OK</span>';
        if (st === 'SHADOW_LOW_EDGE')    return '<span class=\"pill\" style=\"background:#3a3a5a;color:#dfb070;\">LOW_EDGE</span>';
        if (st === 'SHADOW_ONLY')        return '<span class=\"pill\" style=\"background:#3a3a5a;color:#dfb070;\">SHADOW</span>';
        if (st === 'UNMAPPED')           return '<span class=\"pill\" style=\"background:#4a3a3a;color:#ffaa88;\">UNMAPPED</span>';
        if (st === 'RISK_REJECTED')      return '<span class=\"pill\" style=\"background:#5a2d2d;color:#ff8888;\">RISK_BLOCK</span>';
        if (st === 'EXECUTION_REJECTED') return '<span class=\"pill\" style=\"background:#5a3d2d;color:#ffaa66;\">EXEC_FAIL</span>';
        if (st === 'FILTERED_SCOPE')     return '<span class=\"pill\" style=\"background:#3a3a3a;color:#aaa;\">FILTERED</span>';
        if (st === 'LIVE_DISABLED')      return '<span class=\"pill\" style=\"background:#5a2d5a;color:#ff88ff;\">DISABLED</span>';
        if (st === 'NO_CANDIDATES')      return '<span class=\"pill\" style=\"background:#2a2a2a;color:#888;\">NO_SIG</span>';
        if (st === 'PIPELINE_BUG')       return '<span class=\"pill\" style=\"background:#5a2d2d;color:#ff5555;\">BUG</span>';
        return '<span class=\"muted\">?</span>';
      };
      const sourceTag = src => {
        const colors = {'funnel.jsonl': '#7fcf9f', 'opportunity_logs_sql': '#9fcfff', 'orders': '#dfb070', 'positions': '#cf9fcf'};
        return '<span style=\"font-size:9px;color:' + (colors[src] || '#888') + ';\">' + src + '</span>';
      };
      document.getElementById('sh-table').querySelector('tbody').innerHTML =
        (d.strategy_health.rows || []).map(r => {
          const haircut = r.haircut_card
            ? '<br><span style=\"font-size:9px;color:#dfb070;\">raw=' + r.haircut_card.raw_edge_pp.toFixed(1)
              + 'pp − haircut=' + r.haircut_card.haircut_pp.toFixed(1)
              + 'pp = tradable=' + r.haircut_card.tradable_edge_pp.toFixed(1)
              + 'pp <span style=\"color:#888;\">vs threshold=' + r.haircut_card.live_threshold_pp.toFixed(1)
              + 'pp (' + (r.haircut_card.distance_to_live_pp >= 0 ? '+' : '') + r.haircut_card.distance_to_live_pp.toFixed(1)
              + 'pp from live)</span></span>'
            : '';
          return row([
            '<span style=\"font-weight:bold;\">' + r.strategy + '</span>'
              + (r.note ? ' <span class=\"muted\" style=\"font-size:9px;\">[' + r.note + ']</span>' : '')
              + ' <span class=\"muted\" style=\"font-size:9px;\">(' + r.type + ')</span>'
              + '<br>' + sourceTag(r.telemetry_source)
              + haircut,
            healthBadge(r.status),
            r.signals_24h,
            (r.risk_pass_24h > 0 ? '<span class=\"green\">' : '<span class=\"muted\">') + r.risk_pass_24h + '</span>',
            (r.risk_reject_24h > 0 ? '<span class=\"red\">' : '<span class=\"muted\">') + r.risk_reject_24h + '</span>',
            '<span style=\"font-size:10px;\">' + fmt(r.last_signal) + '</span>',
            '<span style=\"font-size:10px;\">' + fmt(r.last_risk_pass) + '</span>',
            '<span style=\"font-size:10px;\">' + fmt(r.last_order) + '</span>',
            '<span style=\"font-size:10px;\">' + fmt(r.last_position) + '</span>',
            '<span style=\"font-size:10px;color:#aaa;\">' + (r.last_reject_reason || '') + '</span>',
          ]);
        }).join('') || '<tr><td colspan=\"10\" class=\"muted\">no health data yet</td></tr>';
    }

    document.getElementById('open-count').textContent = '(' + d.open_positions.length + ')';
    document.getElementById('open-table').querySelector('tbody').innerHTML =
      d.open_positions.map(p => row([
        p.market_id.slice(0,12),
        '<span class=\"pill blue\">' + (p.strategy || '?') + '</span>',
        p.quantity.toFixed(2),
        '$' + p.avg_price.toFixed(3),
        '<span class=\"' + cls(p.realized_pnl) + '\">' + fmtMoney(p.realized_pnl) + '</span>',
        '<span class=\"' + cls(p.unrealized_pnl) + '\">' + fmtMoney(p.unrealized_pnl) + '</span>',
        (p.created_at || '').slice(0, 19),
      ])).join('') || '<tr><td colspan=\"7\" class=\"muted\">no open positions</td></tr>';

    document.getElementById('exit-table').querySelector('tbody').innerHTML =
      d.exit_reasons.map(r => row([
        r.kind,
        r.count,
        '<span class=\"' + cls(r.pnl) + '\">' + fmtMoney(r.pnl) + '</span>',
      ])).join('') || '<tr><td colspan=\"3\" class=\"muted\">no closes</td></tr>';

    document.getElementById('strat-table').querySelector('tbody').innerHTML =
      d.by_strategy.filter(s => s.orders > 0).map(s => row([
        s.name,
        s.orders,
        '<span class=\"green\">' + s.wins + '</span>',
        '<span class=\"red\">' + s.losses + '</span>',
        '<span class=\"' + cls(s.pnl) + '\">' + fmtMoney(s.pnl) + '</span>',
      ])).join('') || '';

    if (d.asset_target_lab) {
      const al = d.asset_target_lab;
      document.getElementById('asset-total').textContent = al.total_logs || 0;
      document.getElementById('asset-tracked').textContent = (al.recent_assets || []).length;

      document.getElementById('asset-decisions').querySelector('tbody').innerHTML =
        (al.decisions || []).map(d => row([
          '<span style=\"font-size:11px;\">' + d.decision + '</span>',
          d.asset || '—',
          d.count,
        ])).join('') || '<tr><td colspan=\"3\" class=\"muted\">no logs yet</td></tr>';

      document.getElementById('asset-recent').querySelector('tbody').innerHTML =
        (al.recent_assets || []).map(a => {
          const edgeClass = a.tradable_edge !== null && a.tradable_edge >= 0 ? 'green' : 'red';
          const edgeText = a.tradable_edge !== null
            ? (a.tradable_edge >= 0 ? '+' : '') + (a.tradable_edge * 100).toFixed(2) + '%'
            : '—';
          return row([
            '<b>' + (a.asset || '?') + '</b>',
            '$' + (a.threshold || '?'),
            a.direction || '?',
            a.deadline || '?',
            (a.model_prob * 100).toFixed(1) + '%',
            (a.poly_ask * 100).toFixed(1) + '%',
            '<span class=\"' + edgeClass + '\">' + edgeText + '</span>',
            '<span style=\"font-size:10px;\">' + a.decision + '</span>',
          ]);
        }).join('') || '<tr><td colspan=\"8\" class=\"muted\">no exact-mapped assets yet</td></tr>';
    }

    if (d.sniper_lab) {
      const sl = d.sniper_lab;
      const totalMapped = (sl.leagues_with_mapping || []).reduce((sum, l) => sum + l.count, 0);
      document.getElementById('sniper-total').textContent = sl.total_logs || 0;
      document.getElementById('sniper-mapped').textContent = totalMapped;
      const fwd = sl.forward_returns || {};
      const fwdRender = (pct, n) => {
        if (!n || n === 0) return '<span class=\"muted\">no data</span>';
        const cls_ = pct >= 0 ? 'green' : 'red';
        return '<span class=\"' + cls_ + '\">' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '% (n=' + n + ')</span>';
      };
      document.getElementById('sniper-fwd-5m').innerHTML = fwdRender(fwd.avg_5m_pct, fwd.n_5m);
      document.getElementById('sniper-fwd-60m').innerHTML = fwdRender(fwd.avg_60m_pct, fwd.n_60m);
      document.getElementById('sniper-fwd-180m').innerHTML = fwdRender(fwd.avg_180m_pct, fwd.n_60m);

      document.getElementById('sniper-leagues').querySelector('tbody').innerHTML =
        (sl.leagues_with_mapping || []).map(l => row([l.league, l.count])).join('')
        || '<tr><td colspan=\"2\" class=\"muted\">no mappings yet</td></tr>';

      document.getElementById('sniper-windows').querySelector('tbody').innerHTML =
        (sl.windows || []).map(w => row([
          w.window,
          (w.avg_edge >= 0 ? '+' : '') + (w.avg_edge * 100).toFixed(2) + '%',
          w.count,
        ])).join('') || '<tr><td colspan=\"3\" class=\"muted\">none</td></tr>';

      document.getElementById('sniper-exact').querySelector('tbody').innerHTML =
        (sl.recent_exact_matches || []).map(m => row([
          '<span style=\"font-size:11px;\">' + m.slug + '</span>',
          m.teams,
          m.window,
          m.minutes_to_start.toFixed(0) + 'm',
          '<span class=\"muted\">' + m.decision + '</span>',
        ])).join('') || '<tr><td colspan=\"5\" class=\"muted\">no EXACT matches yet — ждём pre_90_30 window</td></tr>';
    }

    if (d.fade_lab) {
      const f = d.fade_lab;
      document.getElementById('fade-total').textContent = f.total || 0;
      document.getElementById('fade-sweet').textContent = (f.sweet_spot_count || 0) + ' / ' + (f.sweet_spot_target || 30);
      const mag = f.by_magnitude || {};
      const magNotes = {'5-6pp':'marginal','6-10pp':'sweet spot','10pp+':'avoid (real news)'};
      document.getElementById('fade-mag').querySelector('tbody').innerHTML =
        Object.keys(mag).map(k => row([k, mag[k], '<span class=\"muted\" style=\"font-size:10px;\">'+(magNotes[k]||'')+'</span>'])).join('')
        || '<tr><td colspan=\"3\" class=\"muted\">no signals yet</td></tr>';
      const ec = f.by_external_confirmation || {};
      document.getElementById('fade-ec').querySelector('tbody').innerHTML =
        Object.keys(ec).map(k => row([k, ec[k]])).join('')
        || '<tr><td colspan=\"2\" class=\"muted\">no enriched signals yet</td></tr>';
      document.getElementById('fade-recent').querySelector('tbody').innerHTML =
        (f.recent || []).slice().reverse().map(r => row([
          '<span style=\"font-size:10px;\">'+r.title+'</span>',
          r.delta_5m_pp.toFixed(1),
          r.fade_label,
          r.expected_edge_pp.toFixed(1),
          '<span class=\"muted\" style=\"font-size:10px;\">'+(r.ec||'-')+'</span>',
        ])).join('') || '<tr><td colspan=\"5\" class=\"muted\">no signals yet</td></tr>';
    }
    if (d.radar_lab) {
      const rl = d.radar_lab;
      document.getElementById('radar-total').textContent = rl.total || 0;
      document.getElementById('radar-big').textContent = rl.big_gaps || 0;
      document.getElementById('radar-top').querySelector('tbody').innerHTML =
        (rl.recent_top_gaps || []).map(r => row([
          '<span style=\"font-size:10px;\">'+r.title+'</span>',
          (r.pm_prob*100).toFixed(1)+'%',
          (r.mf_prob*100).toFixed(1)+'%',
          '<span class=\"'+(r.gap_pp>=0?'green':'red')+'\">'+(r.gap_pp>=0?'+':'')+r.gap_pp.toFixed(1)+'</span>',
          r.similarity,
        ])).join('') || '<tr><td colspan=\"5\" class=\"muted\">no gaps yet</td></tr>';
    }
    if (d.rewards_lab) {
      const rw = d.rewards_lab;
      document.getElementById('rew-total').textContent = rw.total || 0;
      document.getElementById('rew-qual').textContent = rw.qualified || 0;
      document.getElementById('rew-top').querySelector('tbody').innerHTML =
        (rw.top || []).map(r => row([
          r.score.toFixed(1),
          '<span style=\"font-size:10px;\">'+r.title+'</span>',
          r.spread.toFixed(2),
          r.mid.toFixed(3),
          '$'+r.depth.toFixed(0),
        ])).join('') || '<tr><td colspan=\"5\" class=\"muted\">no rewards data yet</td></tr>';
    }
    if (d.arb_lab) {
      const ab = d.arb_lab;
      document.getElementById('arb-total').textContent = ab.total || 0;
      document.getElementById('arb-cand').textContent = ab.candidates || 0;
      document.getElementById('arb-list').querySelector('tbody').innerHTML =
        (ab.top || []).map(r => row([
          '<span class=\"green\">+'+r.edge_pp.toFixed(2)+'</span>',
          '<span style=\"font-size:10px;\">'+r.signal+'</span>',
          '<span style=\"font-size:10px;\">'+r.title+'</span>',
          r.n_markets,
          '$'+r.min_liq.toFixed(0),
        ])).join('') || '<tr><td colspan=\"5\" class=\"muted\">no arb candidates</td></tr>';
    }
    if (d.hedge_shadow_lab) {
      const hs = d.hedge_shadow_lab;
      document.getElementById('hs-runs').textContent = (hs.total_runs || 0).toLocaleString();
      document.getElementById('hs-events').textContent = hs.events_unique || 0;
      const cleanEl = document.getElementById('hs-clean');
      cleanEl.textContent = hs.clean_count || 0;
      cleanEl.className = 'value ' + ((hs.clean_count || 0) >= (hs.target || 20) ? 'green' : 'amber');
      document.getElementById('hs-incomplete').textContent = hs.incomplete_count || 0;
      document.getElementById('hs-stale').textContent = hs.stale_count || 0;
      document.getElementById('hs-errdet').textContent = hs.error_count || 0;
      document.getElementById('hs-target').textContent = hs.target || 20;

      document.getElementById('hs-clean-list').querySelector('tbody').innerHTML =
        (hs.clean_top || []).map(r => row([
          '<span class=\"pill ' + (r.side === 'YES' ? 'green' : 'amber') + '\">' + r.side + '</span>',
          '<span style=\"font-size:10px;\">' + r.title + '</span>',
          r.n_legs_ready + '/' + r.n_total,
          r.shares,
          (r.edge_r1_pp || 0).toFixed(2) + '%',
          '<span class=\"green\">' + (r.edge_r2_pp || 0).toFixed(2) + '%</span>',
          (r.edge_decay_pp >= 0.5 ? '<span class=\"red\">' : '<span>') + (r.edge_decay_pp || 0).toFixed(2) + 'pp</span>',
          '<span class=\"green\">$' + (r.net_profit || 0).toFixed(3) + '</span>',
          r.live_eligible ? '<span class=\"pill green\">YES</span>' : '<span class=\"muted\">no</span>',
        ])).join('') || '<tr><td colspan=\"9\" class=\"muted\">no CLEAN baskets yet — accumulating shadow data</td></tr>';

      document.getElementById('hs-incomplete-list').querySelector('tbody').innerHTML =
        (hs.incomplete_top || []).map(r => row([
          '<span class=\"pill ' + (r.side === 'YES' ? 'green' : 'amber') + '\">' + r.side + '</span>',
          '<span style=\"font-size:10px;\">' + r.title + '</span>',
          r.n_legs_ready + '/' + r.n_total,
          r.shares || '—',
          '<span class=\"amber\">' + (r.edge_r2_pp || 0).toFixed(2) + '%</span>',
          '$' + (r.cost || 0).toFixed(2),
          (r.claimed_edge_pp || 0).toFixed(1) + 'pp',
        ])).join('') || '<tr><td colspan=\"7\" class=\"muted\">no incomplete baskets in current scan</td></tr>';
    }
    if (d.weather_shadow_lab) {
      const ws = d.weather_shadow_lab;
      document.getElementById('ws-events').textContent = ws.events || 0;
      document.getElementById('ws-skipped').textContent = ws.skipped || 0;
      const sigEl = document.getElementById('ws-signals');
      sigEl.textContent = ws.signals || 0;
      sigEl.className = 'value ' + ((ws.signals || 0) > 0 ? 'green' : '');
      document.getElementById('ws-list').querySelector('tbody').innerHTML =
        (ws.top_signals || []).map(s => row([
          '<span style=\"font-size:10px;\">'+(s.city||'')+'</span>',
          '<span class=\"pill\">' + (s.bucket_kind === 'le' ? '≤' : (s.bucket_kind === 'ge' ? '≥' : '=')) + s.bucket_temp + '°C</span>',
          '<span style=\"font-size:10px;\">'+s.title+'</span>',
          (s.forecast_prob*100).toFixed(1)+'%',
          '$'+s.ask_price.toFixed(4),
          s.ask_depth.toFixed(0),
          '<span class=\"green\">+'+s.edge_buy_pp.toFixed(2)+'pp</span>',
          (s.hours_to_resolution||0).toFixed(1)+'h',
        ])).join('') || '<tr><td colspan=\"8\" class=\"muted\">no weather signals yet — accumulating</td></tr>';
    }
    if (d.sm_weather_lab) {
      const sm = d.sm_weather_lab;
      document.getElementById('sm-cands').textContent = sm.candidates || 0;
      document.getElementById('sm-sigs').textContent = sm.signals_7d || 0;
      document.getElementById('sm-cand-list').querySelector('tbody').innerHTML =
        (sm.top_candidates || []).map(c => row([
          '<span style=\"font-family:monospace;font-size:10px;\">'+c.wallet.slice(0,10)+'..</span>',
          c.n_temp_trades,
          c.n_unique_events,
          '$'+(c.avg_notional_usd||0).toFixed(0),
          '$'+(c.total_volume_usd||0).toFixed(0),
          (c.last_active_days_ago||0).toFixed(1)+'d',
        ])).join('') || '<tr><td colspan=\"6\" class=\"muted\">no weather wallets yet</td></tr>';
      document.getElementById('sm-sig-list').querySelector('tbody').innerHTML =
        (sm.recent_signals || []).map(s => row([
          '<span style=\"font-family:monospace;font-size:10px;\">'+s.wallet+'</span>',
          '<span class=\"pill ' + (s.side === 'BUY' ? 'green' : 'amber') + '\">'+s.side+'</span>',
          '$'+(s.price||0).toFixed(4),
          (s.size||0).toFixed(0),
          '<span style=\"font-size:10px;\">'+(s.outcome||'')+'</span>',
          '<span style=\"font-size:10px;\">'+s.title+'</span>',
        ])).join('') || '<tr><td colspan=\"6\" class=\"muted\">no recent signals</td></tr>';
    }
    if (d.maker_sim_lab) {
      const mk = d.maker_sim_lab;
      document.getElementById('mk-snaps').textContent = mk.quote_snapshots || 0;
      document.getElementById('mk-fevals').textContent = mk.fills_eval || 0;
      const fEl = document.getElementById('mk-filled');
      fEl.textContent = mk.filled || 0;
      fEl.className = 'value ' + ((mk.filled || 0) > 0 ? 'green' : '');
      document.getElementById('mk-markout').textContent = '$'+(mk.median_markout_per_share||0).toFixed(4);
      const pnlEl = document.getElementById('mk-pnl');
      const p = mk.total_pnl_paper || 0;
      pnlEl.textContent = (p >= 0 ? '+$' : '-$') + Math.abs(p).toFixed(2);
      pnlEl.className = 'value ' + (p > 0 ? 'green' : (p < 0 ? 'red' : ''));
      document.getElementById('mk-fills').querySelector('tbody').innerHTML =
        (mk.recent_fills || []).map(f => row([
          (f.event_id||'').slice(0,7),
          (f.market_id||'').slice(0,7),
          '$'+(f.prev_sim_ask||0).toFixed(4),
          '$'+(f.cur_best_bid||0).toFixed(4),
          (f.elapsed_min||0).toFixed(1)+'m',
          ((f.markout_pnl_per_share||0) >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$') + Math.abs(f.markout_pnl_per_share||0).toFixed(4) + '</span>',
          (f.shares||0).toFixed(0),
        ])).join('') || '<tr><td colspan=\"7\" class=\"muted\">no simulated fills yet</td></tr>';
    }
    if (d.bucket_pnl) {
      const bp = d.bucket_pnl;
      document.getElementById('bp-closed').querySelector('tbody').innerHTML =
        (bp.closed || []).map(r => row([
          '<span style=\"font-weight:bold;\">'+r.bucket+'</span>',
          r.trades,
          r.wins+'/'+r.losses,
          (r.win_rate_pct >= 50 ? '<span class=\"green\">' : '<span class=\"red\">')+r.win_rate_pct+'%</span>',
          (r.total_pnl >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$')+Math.abs(r.total_pnl).toFixed(2)+'</span>',
          (r.avg_pnl >= 0 ? '+' : '-')+'$'+Math.abs(r.avg_pnl).toFixed(4),
          '$'+r.worst.toFixed(2),
          '$'+r.best.toFixed(2),
        ])).join('') || '<tr><td colspan=\"8\" class=\"muted\">no closed positions yet</td></tr>';
      document.getElementById('bp-open').querySelector('tbody').innerHTML =
        (bp.open || []).map(r => row([
          '<span style=\"font-weight:bold;\">'+r.bucket+'</span>',
          r.open_count,
          (r.unrealized >= 0 ? '<span class=\"green\">+$' : '<span class=\"red\">-$')+Math.abs(r.unrealized).toFixed(2)+'</span>',
        ])).join('') || '<tr><td colspan=\"3\" class=\"muted\">no open positions</td></tr>';
    }
    if (d.resolution_risk_lab) {
      const rr = d.resolution_risk_lab;
      document.getElementById('rr-total').textContent = rr.total || 0;
      document.getElementById('rr-high').textContent = rr.high || 0;
      document.getElementById('rr-medium').textContent = rr.medium || 0;
      document.getElementById('rr-list').querySelector('tbody').innerHTML =
        (rr.high_list || []).map(r => row([
          '<span class=\"red\">'+r.score+'</span>',
          '<span style=\"font-size:10px;\">'+r.title+'</span>',
          '<span class=\"muted\" style=\"font-size:10px;\">'+r.reason+'</span>',
        ])).join('') || '<tr><td colspan=\"3\" class=\"muted\">no high-risk markets currently</td></tr>';
    }
    if (d.pm_fills_lab) {
      const pf = d.pm_fills_lab;
      document.getElementById('pmf-total').textContent = (pf.total || 0).toLocaleString();
      document.getElementById('pmf-wallets').textContent = (pf.wallets || 0).toLocaleString();
      document.getElementById('pmf-markets').textContent = pf.markets || 0;
      document.getElementById('pmf-span').textContent = (pf.span_h || 0).toFixed(1);
    }

    if (d.ai_fair_price) {
      const a = d.ai_fair_price;
      document.getElementById('ai-fp').innerHTML =
        'Enabled: <b>' + a.enabled + '</b><br>Calls: ' + a.calls + ' · Errors: ' + a.errors + ' · Cache: ' + a.cache_size;
    }
    if (d.ai_veto) {
      const a = d.ai_veto;
      document.getElementById('ai-veto').innerHTML =
        'Enabled: <b>' + a.enabled + '</b><br>Total: ' + a.total + ' · GO: ' + a.go + ' · SKIP: ' + a.skip + ' (' + a.skip_rate + '%)';
    }

    document.getElementById('trades-count').textContent = '(' + d.recent_trades.length + ')';
    document.getElementById('trades-table').querySelector('tbody').innerHTML =
      d.recent_trades.map(t => {
        const sideClass = t.side === 'BUY' ? 'pill blue' : 'pill amber';
        const note = (t.note || '').replace(/</g, '&lt;');
        let noteClass = 'muted';
        if (note.includes('take-profit') || note.includes('trailing-tp')) noteClass = 'green';
        else if (note.includes('stop-loss') || note.includes('hard-stop') || note.includes('safety-stop')) noteClass = 'red';
        else if (note.includes('pre-resolution')) noteClass = 'amber';
        return row([
          (t.created_at || '').slice(11, 19),
          '<span class=\"' + sideClass + '\">' + t.side + '</span>',
          t.strategy,
          t.market_id.slice(0, 10),
          '$' + t.price.toFixed(3),
          t.size.toFixed(2),
          '<span class=\"note ' + noteClass + '\" title=\"' + note + '\">' + note + '</span>',
        ]);
      }).join('') || '<tr><td colspan=\"7\" class=\"muted\">no trades</td></tr>';

    document.getElementById('refresh').textContent = '⟳ ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('refresh').textContent = 'error: ' + e.message;
  }
}

load();
setInterval(load, 5000);
</script>
</body>
</html>"""


ANALYTICS_HTML = """<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>Polymarket Bot · Analytics</title>
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<style>__STYLE__</style>
<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js\"></script>
</head>
<body>
<div class=\"grid\">
  <div class=\"header-row\">
    <h1>📈 Analytics</h1>
    <div class=\"nav\">
      <a class=\"btn\" href=\"/dashboard\">📊 Live</a>
      <a class=\"btn active\" href=\"/dashboard/analytics\">📈 Analytics</a>
    </div>
    <div class=\"refresh\" id=\"refresh\">loading…</div>
  </div>

  <div class=\"row\">
    <div class=\"panel\"><div class=\"kpi big\"><span class=\"label\">Closed</span><span class=\"value\" id=\"k-closed\">—</span></div></div>
    <div class=\"panel\"><div class=\"kpi big\"><span class=\"label\">Total PnL</span><span class=\"value\" id=\"k-total\">—</span></div></div>
    <div class=\"panel\"><div class=\"kpi big\"><span class=\"label\">Wins / Losses</span><span class=\"value\" id=\"k-wl\">—</span></div></div>
    <div class=\"panel\"><div class=\"kpi big\"><span class=\"label\">Best / Worst</span><span class=\"value\" id=\"k-bw\">—</span></div></div>
  </div>

  <div class=\"panel\">
    <h2>💰 Cumulative PnL Over Time</h2>
    <div class=\"chart-wrap tall\"><canvas id=\"cum-chart\"></canvas></div>
  </div>

  <div class=\"row-2\">
    <div class=\"panel\">
      <h2>🎯 By Strategy</h2>
      <div class=\"chart-wrap\"><canvas id=\"strat-chart\"></canvas></div>
    </div>
    <div class=\"panel\">
      <h2>📊 PnL Distribution</h2>
      <div class=\"chart-wrap\"><canvas id=\"dist-chart\"></canvas></div>
    </div>
  </div>

  <div class=\"row-2\">
    <div class=\"panel\">
      <h2>🕒 PnL by Hour (UTC)</h2>
      <div class=\"chart-wrap\"><canvas id=\"hour-chart\"></canvas></div>
    </div>
    <div class=\"panel\">
      <h2>⏱ Holding Time</h2>
      <div class=\"chart-wrap\"><canvas id=\"hold-chart\"></canvas></div>
    </div>
  </div>

  <div class=\"panel\">
    <h2>🎯 Holding Time vs PnL (each dot = 1 trade)</h2>
    <div class=\"chart-wrap tall\"><canvas id=\"scatter-chart\"></canvas></div>
  </div>

</div>

<script>
const COLORS = {
  green: '#4ade80', red: '#f87171', amber: '#fbbf24', accent: '#60a5fa', purple: '#c084fc', muted: '#8b96a3'
};
const fmtMoney = v => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);

Chart.defaults.color = '#8b96a3';
Chart.defaults.borderColor = '#2a3440';
Chart.defaults.font.family = 'SF Mono, ui-monospace, Menlo, monospace';
Chart.defaults.font.size = 11;
Chart.defaults.animation = false;

let DATA = { timeseries: [], by_strategy: [], pnl_buckets: [], hour_pnl: [], holding_buckets: [], scatter: [] };

function commonScales(yPrefix='$') {
  return {
    y: { grid: { color: '#1f2937' }, ticks: { callback: v => yPrefix + v } },
    x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } },
  };
}

const cumChart = new Chart(document.getElementById('cum-chart'), {
  type: 'line',
  data: { labels: [], datasets: [{
    label: 'Cumulative PnL', data: [],
    borderColor: COLORS.accent,
    backgroundColor: COLORS.accent + '20',
    fill: true, tension: 0.2, pointRadius: 3, pointHoverRadius: 6,
  }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: ctx => 'Cum: ' + fmtMoney(ctx.parsed.y) + ' · Trade: ' + fmtMoney(DATA.timeseries[ctx.dataIndex]?.pnl)
    }}},
    scales: commonScales(),
  },
});

const stratChart = new Chart(document.getElementById('strat-chart'), {
  type: 'bar',
  data: { labels: [], datasets: [{ label: 'PnL', data: [], backgroundColor: [], borderRadius: 4 }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: ctx => fmtMoney(ctx.parsed.y) + ' · ' + (DATA.by_strategy[ctx.dataIndex]?.count || 0) + ' trades · ' + (DATA.by_strategy[ctx.dataIndex]?.wins || 0) + 'W'
    }}},
    scales: commonScales(),
  },
});

const distChart = new Chart(document.getElementById('dist-chart'), {
  type: 'bar',
  data: { labels: [], datasets: [{ label: 'Count', data: [], backgroundColor: [], borderRadius: 4 }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }},
    scales: { y: { grid: { color: '#1f2937' }, ticks: { precision: 0 }}, x: { grid: { display: false }}},
  },
});

const hourChart = new Chart(document.getElementById('hour-chart'), {
  type: 'bar',
  data: { labels: [], datasets: [{ label: 'PnL', data: [], backgroundColor: [], borderRadius: 3 }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: ctx => fmtMoney(ctx.parsed.y) + ' · ' + (DATA.hour_pnl[ctx.dataIndex]?.count || 0) + ' trades'
    }}},
    scales: commonScales(),
  },
});

const holdChart = new Chart(document.getElementById('hold-chart'), {
  type: 'bar',
  data: { labels: [], datasets: [
    { label: 'Wins', data: [], backgroundColor: COLORS.green, stack: 's' },
    { label: 'Losses', data: [], backgroundColor: COLORS.red, stack: 's' },
  ]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { tooltip: { callbacks: {
      afterLabel: ctx => 'PnL: ' + fmtMoney(DATA.holding_buckets[ctx.dataIndex]?.pnl)
    }}},
    scales: { y: { stacked: true, grid: { color: '#1f2937' }, ticks: { precision: 0 }}, x: { stacked: true, grid: { display: false }}},
  },
});

const scatterChart = new Chart(document.getElementById('scatter-chart'), {
  type: 'scatter',
  data: { datasets: [{ label: 'Trades', data: [], backgroundColor: [], pointRadius: 5, pointHoverRadius: 8 }]},
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: false }, tooltip: { callbacks: {
      label: ctx => 'Age: ' + ctx.parsed.x + 'min · PnL: ' + fmtMoney(ctx.parsed.y)
    }}},
    scales: {
      x: { title: { display: true, text: 'Holding (min)' }, grid: { color: '#1f2937' }},
      y: { title: { display: true, text: 'PnL ($)' }, grid: { color: '#1f2937' }, ticks: { callback: v => '$' + v }},
    },
  },
});

async function load() {
  try {
    const r = await fetch('/dashboard/analytics-data');
    const d = await r.json();
    DATA = d;
    const s = d.summary;
    document.getElementById('k-closed').textContent = s.closed;
    const totalEl = document.getElementById('k-total');
    totalEl.textContent = fmtMoney(s.total_pnl);
    totalEl.className = 'value ' + (s.total_pnl > 0 ? 'green' : s.total_pnl < 0 ? 'red' : 'muted');
    document.getElementById('k-wl').innerHTML =
      '<span class=\"green\">' + s.wins + '</span> / <span class=\"red\">' + s.losses + '</span>';
    document.getElementById('k-bw').innerHTML =
      '<span class=\"green\">' + fmtMoney(s.best) + '</span> / <span class=\"red\">' + fmtMoney(s.worst) + '</span>';

    const cumLabels = d.timeseries.map(t => new Date(t.ts).toLocaleString([], { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }));
    const cumValues = d.timeseries.map(t => t.cum_pnl);
    const finalPnL = cumValues[cumValues.length - 1] || 0;
    const lineColor = finalPnL >= 0 ? COLORS.green : COLORS.red;
    cumChart.data.labels = cumLabels;
    cumChart.data.datasets[0].data = cumValues;
    cumChart.data.datasets[0].borderColor = lineColor;
    cumChart.data.datasets[0].backgroundColor = lineColor + '20';
    cumChart.data.datasets[0].pointBackgroundColor = d.timeseries.map(p => p.pnl > 0 ? COLORS.green : p.pnl < 0 ? COLORS.red : COLORS.muted);
    cumChart.update('none');

    stratChart.data.labels = d.by_strategy.map(s => s.name);
    stratChart.data.datasets[0].data = d.by_strategy.map(s => s.pnl);
    stratChart.data.datasets[0].backgroundColor = d.by_strategy.map(s => s.pnl >= 0 ? COLORS.green : COLORS.red);
    stratChart.update('none');

    distChart.data.labels = d.pnl_buckets.map(b => b.bucket);
    distChart.data.datasets[0].data = d.pnl_buckets.map(b => b.count);
    distChart.data.datasets[0].backgroundColor = d.pnl_buckets.map(b =>
      b.bucket.startsWith('< -') || b.bucket.startsWith('-$') ? COLORS.red :
      b.bucket === '$0' ? COLORS.muted : COLORS.green
    );
    distChart.update('none');

    hourChart.data.labels = d.hour_pnl.map(h => h.hour + 'h');
    hourChart.data.datasets[0].data = d.hour_pnl.map(h => h.pnl);
    hourChart.data.datasets[0].backgroundColor = d.hour_pnl.map(h => h.pnl > 0 ? COLORS.green : h.pnl < 0 ? COLORS.red : COLORS.muted);
    hourChart.update('none');

    holdChart.data.labels = d.holding_buckets.map(h => h.label);
    holdChart.data.datasets[0].data = d.holding_buckets.map(h => h.wins);
    holdChart.data.datasets[1].data = d.holding_buckets.map(h => h.losses);
    holdChart.update('none');

    scatterChart.data.datasets[0].data = d.scatter.map(p => ({ x: p.age_min, y: p.pnl }));
    scatterChart.data.datasets[0].backgroundColor = d.scatter.map(p => p.pnl > 0 ? COLORS.green : p.pnl < 0 ? COLORS.red : COLORS.muted);
    scatterChart.update('none');

    document.getElementById('refresh').textContent = '⟳ ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('refresh').textContent = 'error: ' + e.message;
  }
}

load();
setInterval(load, 15000);
</script>
</body>
</html>"""

DASHBOARD_HTML = DASHBOARD_HTML.replace("__STYLE__", COMMON_STYLE)
ANALYTICS_HTML = ANALYTICS_HTML.replace("__STYLE__", COMMON_STYLE)
