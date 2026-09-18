"""Weather Bucket Shadow per [GPT 30] H4+H3 merged + [Claude 35] day-1.

Pulls Polymarket temperature-bucket events, fetches Open-Meteo ensemble forecast
(30 GFS members), maps members to bucket probability distribution, compares to
PM CLOB best ask — logs single-leg directional edge candidates.

NOT a basket arb. NOT live. Pure shadow logger.

Output: /app/data/weather_bucket_shadow.jsonl

Cron: */15 * * * * (light, single API call per event, then a few CLOB books)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"

OUTPUT = Path("/app/data/weather_bucket_shadow.jsonl")

# City → (lat, lon, station_name, IANA_timezone)
# CRITICAL [GPT 31 Q3 verification]: Wunderground resolves "highest temperature on
# DAY X" using LOCAL station day (00:00-23:59 in station's timezone). If we query
# Open-Meteo with timezone=GMT, the daily max window misaligns with the PM resolution
# day → forecast becomes cold-biased (we capture morning+evening of two days, miss
# midday peak of one day). All Tokyo +51pp signals before 2026-05-05 are artifacts
# of this bug. Use station-local timezone for every city.
CITY_COORDS: dict[str, tuple[float, float, str, str]] = {
    "tokyo": (35.55, 139.78, "Haneda", "Asia/Tokyo"),
    "beijing": (40.07, 116.59, "Capital", "Asia/Shanghai"),
    "nyc": (40.78, -73.87, "LaGuardia", "America/New_York"),
    "new york": (40.78, -73.87, "LaGuardia", "America/New_York"),
    "los angeles": (33.94, -118.41, "LAX", "America/Los_Angeles"),
    "san francisco": (37.62, -122.38, "SFO", "America/Los_Angeles"),
    "london": (51.47, -0.45, "Heathrow", "Europe/London"),
    "paris": (49.01, 2.55, "Charles de Gaulle", "Europe/Paris"),
    "madrid": (40.47, -3.56, "Barajas", "Europe/Madrid"),
    "warsaw": (52.17, 20.97, "Chopin", "Europe/Warsaw"),
    "singapore": (1.36, 103.99, "Changi", "Asia/Singapore"),
    "seoul": (37.46, 126.44, "Incheon", "Asia/Seoul"),
    "shanghai": (31.14, 121.81, "Pudong", "Asia/Shanghai"),
    "hong kong": (22.31, 113.92, "HKIA", "Asia/Hong_Kong"),
    "jakarta": (-6.13, 106.66, "Soekarno-Hatta", "Asia/Jakarta"),
    "taipei": (25.07, 121.55, "Songshan", "Asia/Taipei"),
    "wellington": (-41.33, 174.81, "Wellington", "Pacific/Auckland"),
    "busan": (35.18, 128.94, "Gimhae", "Asia/Seoul"),
    "chengdu": (30.58, 103.95, "Shuangliu", "Asia/Shanghai"),
    "dallas": (32.90, -97.04, "DFW", "America/Chicago"),
    "atlanta": (33.64, -84.43, "Hartsfield", "America/New_York"),
    "denver": (39.86, -104.67, "DEN", "America/Denver"),
    "miami": (25.80, -80.29, "MIA", "America/New_York"),
    "amsterdam": (52.31, 4.76, "Schiphol", "Europe/Amsterdam"),
    "moscow": (55.97, 37.41, "Sheremetyevo", "Europe/Moscow"),
    "istanbul": (41.28, 28.74, "Istanbul Apt", "Europe/Istanbul"),
    "lucknow": (26.76, 80.88, "CCSI", "Asia/Kolkata"),
    "buenos aires": (-34.82, -58.54, "Ezeiza", "America/Argentina/Buenos_Aires"),
    "helsinki": (60.31, 24.96, "Vantaa", "Europe/Helsinki"),
    "lagos": (6.58, 3.32, "Murtala", "Africa/Lagos"),
}

MIN_EDGE_PP_LOG = 3.0  # signal threshold per [GPT 30] live_canary_min
MIN_DEPTH_AT_ASK = 5.0  # need at least 5 shares at best ask to trade meaningful

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Bucket parsing
# ──────────────────────────────────────────────────────────────────────────────

_TEMP_PATTERNS = [
    re.compile(r"\bbe\s+(\d+)\s*°?c\s+or\s+below\b", re.I),       # ≤14°C
    re.compile(r"\bbe\s+(\d+)\s*°?c\s+or\s+higher\b", re.I),      # ≥24°C
    re.compile(r"\bbe\s+(\d+)\s*°?c\b", re.I),                    # exactly 18°C
    re.compile(r"\bbelow\s+(\d+)\s*°?c\b", re.I),                 # <14°C
    re.compile(r"\babove\s+(\d+)\s*°?c\b", re.I),                 # >30°C
]

def parse_bucket(question: str) -> dict | None:
    """Return {kind: 'eq'|'le'|'ge', temp: int} or None.

    PM convention (verified Tokyo May 6):
      'be 14°C or below'  → bucket = ≤14 (kind=le, temp=14)
      'be 24°C or higher' → bucket = ≥24 (kind=ge, temp=24)
      'be 21°C'           → bucket = exactly 21°C bin (kind=eq, temp=21, range [20.5, 21.5))
    """
    if not question:
        return None
    q = question.lower()
    if "or below" in q or "below" in q:
        m = re.search(r"(\d+)\s*°?c", q)
        if m:
            return {"kind": "le", "temp": int(m.group(1))}
    if "or higher" in q or "above" in q:
        m = re.search(r"(\d+)\s*°?c", q)
        if m:
            return {"kind": "ge", "temp": int(m.group(1))}
    m = re.search(r"\bbe\s+(\d+)\s*°?c\b", q)
    if m:
        return {"kind": "eq", "temp": int(m.group(1))}
    return None


def detect_city(title: str) -> str | None:
    if not title:
        return None
    t = title.lower()
    for city in CITY_COORDS:
        if city in t:
            return city
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Fair distribution from Open-Meteo ensemble
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_ensemble_forecast(
    client: httpx.AsyncClient,
    lat: float,
    lon: float,
    forecast_date: str,
    timezone: str = "GMT",
) -> list[float] | None:
    """Pull GFS05 30-member ensemble for max-temp on a specific date.

    CRITICAL: timezone must match the station's local time (NOT GMT) so the daily
    max window aligns with PM resolution day. See CITY_COORDS for per-city tz.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "forecast_days": 5,
        "timezone": timezone,
        "models": "gfs05",
    }
    try:
        r = await client.get(OPEN_METEO_ENSEMBLE, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        logger.warning("forecast_fetch_failed | lat=%s lon=%s err=%s", lat, lon, exc)
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if forecast_date not in times:
        return None
    idx = times.index(forecast_date)

    members: list[float] = []
    # Pull up to 30 members
    for n in range(1, 31):
        key = f"temperature_2m_max_member{n:02d}"
        series = daily.get(key)
        if isinstance(series, list) and idx < len(series) and series[idx] is not None:
            members.append(float(series[idx]))
    if len(members) < 5:
        # Fall back to best_match if ensemble too small
        bm = daily.get("temperature_2m_max")
        if isinstance(bm, list) and idx < len(bm) and bm[idx] is not None:
            return [float(bm[idx])] * 5
        return None
    return members


def bucket_probabilities(members: list[float], bucket_specs: list[dict]) -> dict[tuple, float]:
    """Map ensemble to bucket probabilities. Returns {(kind, temp): prob}."""
    if not members:
        return {}
    n = len(members)
    counts: dict[tuple, int] = {}
    for v in members:
        # Round half-up to the nearest integer °C: 19.5 → 20, 19.4 → 19
        bucket_int = int(v + 0.5) if v >= 0 else -int(-v + 0.5)
        # Match against specs
        matched_key = None
        for spec in bucket_specs:
            kind, temp = spec["kind"], spec["temp"]
            if kind == "le" and bucket_int <= temp:
                matched_key = (kind, temp)
                break
            if kind == "ge" and bucket_int >= temp:
                matched_key = (kind, temp)
                break
            if kind == "eq" and bucket_int == temp:
                matched_key = (kind, temp)
                break
        if matched_key:
            counts[matched_key] = counts.get(matched_key, 0) + 1
        # If no match, member falls outside event range — that means PM event missing buckets.
    return {k: v / n for k, v in counts.items()}


# ──────────────────────────────────────────────────────────────────────────────
# Polymarket pulls
# ──────────────────────────────────────────────────────────────────────────────

async def fetch_active_temperature_events(client: httpx.AsyncClient, limit: int = 100) -> list[dict]:
    """Pull active events whose title hints at temperature + city we know."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        r = await client.get(GAMMA_EVENTS_URL, params=params, timeout=15.0)
        r.raise_for_status()
        events = r.json()
    except Exception as exc:
        logger.warning("events_fetch_failed | err=%s", exc)
        return []

    matches: list[dict] = []
    for ev in events:
        title = (ev.get("title") or "").lower()
        if "temperature" not in title:
            continue
        city = detect_city(title)
        if not city:
            continue
        if not ev.get("negRisk"):
            continue
        matches.append(ev)
    return matches


async def fetch_book(client: httpx.AsyncClient, token_id: str) -> dict | None:
    try:
        r = await client.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def best_ask(book: dict | None) -> tuple[float, float] | None:
    if not book:
        return None
    asks = sorted(book.get("asks") or [], key=lambda a: float(a.get("price", 0)))
    if not asks:
        return None
    p = float(asks[0].get("price", 0))
    s = float(asks[0].get("size", 0))
    return (p, s) if p > 0 else None


def best_bid(book: dict | None) -> tuple[float, float] | None:
    if not book:
        return None
    bids = sorted(book.get("bids") or [], key=lambda b: -float(b.get("price", 0)))
    if not bids:
        return None
    p = float(bids[0].get("price", 0))
    s = float(bids[0].get("size", 0))
    return (p, s) if p > 0 else None


def extract_yes_token(market: dict) -> str | None:
    raw = market.get("clobTokenIds")
    if not raw:
        return None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if len(ids) >= 1:
            return str(ids[0])
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-event analysis
# ──────────────────────────────────────────────────────────────────────────────

def parse_resolution_date(event: dict) -> str | None:
    """Return 'YYYY-MM-DD' for the event's resolution date (UTC)."""
    end = event.get("endDate")
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


async def analyze_event(client: httpx.AsyncClient, event: dict) -> dict:
    title = event.get("title") or ""
    eid = event.get("event_id") or event.get("id")
    city = detect_city(title)
    if not city or city not in CITY_COORDS:
        return {"event_id": eid, "title": title[:80], "skipped": "city_unknown"}
    coords = CITY_COORDS[city]
    if len(coords) == 4:
        lat, lon, station, tz = coords
    else:
        # legacy 3-tuple support
        lat, lon, station = coords[:3]
        tz = "GMT"

    # Forecast date should be in STATION-LOCAL day, not UTC day. parse_resolution_date
    # returns the UTC date from endDate, but PM "May 6" markets resolve based on the
    # local station calendar day. For most cases endDate == station-local end-of-day
    # so UTC date matches; we still pass tz to Open-Meteo so the daily max window aligns.
    forecast_date = parse_resolution_date(event)
    if not forecast_date:
        return {"event_id": eid, "title": title[:80], "skipped": "no_endDate"}

    # Time-to-resolution (hours)
    end_iso = event.get("endDate") or ""
    try:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        hours_left = 999.0

    # Pull ensemble in STATION-LOCAL timezone — fixes [GPT 31 Q3] cold-bias bug
    members = await fetch_ensemble_forecast(client, lat, lon, forecast_date, timezone=tz)
    if not members:
        return {
            "event_id": eid, "title": title[:80],
            "city": city, "skipped": "forecast_unavailable",
        }

    # Build bucket specs from event markets
    markets = event.get("markets") or []
    parsed_markets: list[dict] = []
    for m in markets:
        spec = parse_bucket(m.get("question") or "")
        if not spec:
            continue
        token = extract_yes_token(m)
        if not token:
            continue
        parsed_markets.append({
            "market_id": str(m.get("id")),
            "question": (m.get("question") or "")[:80],
            "spec": spec,
            "token_id": token,
            "gamma_yes": _gamma_yes(m),
        })
    if len(parsed_markets) < 3:
        return {
            "event_id": eid, "title": title[:80], "city": city,
            "skipped": f"too_few_parsed_markets={len(parsed_markets)}",
        }

    # Compute bucket probabilities from ensemble
    specs = [pm["spec"] for pm in parsed_markets]
    probs_by_key = bucket_probabilities(members, specs)

    # Fetch CLOB asks for each leg (in parallel, but small N)
    book_tasks = [fetch_book(client, pm["token_id"]) for pm in parsed_markets]
    books = await asyncio.gather(*book_tasks)

    legs: list[dict] = []
    for pm, book in zip(parsed_markets, books):
        ba = best_ask(book)
        bb = best_bid(book)
        if not ba:
            continue
        ask_p, ask_s = ba
        bid_p = bb[0] if bb else 0.0
        spec_key = (pm["spec"]["kind"], pm["spec"]["temp"])
        forecast_p = probs_by_key.get(spec_key, 0.0)
        # Edge: if ask < forecast → BUY YES (mispriced cheap)
        # Edge: if bid > forecast → SELL YES (we don't have YES; but mark SELL signal)
        edge_buy_pp = (forecast_p - ask_p) * 100
        legs.append({
            "market_id": pm["market_id"],
            "question": pm["question"],
            "bucket_kind": pm["spec"]["kind"],
            "bucket_temp": pm["spec"]["temp"],
            "forecast_prob": round(forecast_p, 4),
            "ask_price": ask_p,
            "ask_depth": ask_s,
            "bid_price": bid_p,
            "gamma_yes": pm["gamma_yes"],
            "edge_buy_pp": round(edge_buy_pp, 3),
        })

    # Identify signals: forecast_prob - ask_price ≥ MIN_EDGE_PP_LOG/100, depth sufficient
    signals = [
        l for l in legs
        if l["edge_buy_pp"] >= MIN_EDGE_PP_LOG
        and l["ask_depth"] >= MIN_DEPTH_AT_ASK
        and l["ask_price"] > 0.01  # avoid noise on dust legs
    ]

    forecast_total_prob = sum(probs_by_key.values())
    # FIX-2 [Claude 47]: classify decision so dashboard can group events.
    # Was: every event had decision='?' → 2,376 telemetry-blind events.
    if len(parsed_markets) == 0:
        decision = "OUT_OF_SCOPE"
    elif len(legs) == 0:
        decision = "NO_BOOK"
    elif len(signals) > 0:
        decision = "SHADOW_BUY"
    else:
        decision = "SHADOW_NO_EDGE"
    return {
        "event_id": eid,
        "title": title[:80],
        "city": city,
        "station": station,
        "tz": tz,
        "forecast_date": forecast_date,
        "rounding_rule": "round_half_up_int",
        "status_label": "external_fair_shadow_bug_risk_high_until_5_manual_verifies",  # [GPT 31]
        "hours_to_resolution": round(hours_left, 1),
        "ts": int(time.time()),
        "decision": decision,
        "n_ensemble_members": len(members),
        "ensemble_min": round(min(members), 2),
        "ensemble_max": round(max(members), 2),
        "ensemble_mean": round(sum(members) / len(members), 2),
        "n_markets": len(markets),
        "n_parsed_legs": len(parsed_markets),
        "n_legs_with_book": len(legs),
        "forecast_total_prob": round(forecast_total_prob, 4),
        "n_signals": len(signals),
        "legs": legs,
        "signals": signals,
    }


def _gamma_yes(market: dict) -> float | None:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0]) if prices else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def append_records(records: list[dict]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-events", type=int, default=200)
    args = parser.parse_args()

    started = time.time()
    async with httpx.AsyncClient() as client:
        events = await fetch_active_temperature_events(client, limit=args.limit_events)
        logger.info("weather_shadow.events | matched_temperature_events=%d", len(events))

        if not events:
            logger.info("weather_shadow.no_events")
            return

        records: list[dict] = []
        for ev in events:
            try:
                rec = await analyze_event(client, ev)
                records.append(rec)
            except Exception as exc:
                logger.warning("analyze_failed | event=%s err=%s", ev.get("id"), exc)
                records.append({
                    "event_id": ev.get("id"),
                    "title": (ev.get("title") or "")[:80],
                    "ts": int(time.time()),
                    "error": str(exc)[:120],
                })

    append_records(records)

    n_signals_total = sum(r.get("n_signals", 0) or 0 for r in records)
    n_skipped = sum(1 for r in records if "skipped" in r)
    elapsed = round(time.time() - started, 1)

    logger.info(
        "weather_shadow.done | events=%d skipped=%d signals=%d elapsed=%ss",
        len(records), n_skipped, n_signals_total, elapsed,
    )

    print(f"\n=== Weather Bucket Shadow ({len(records)} events) ===\n")
    for r in records:
        if "skipped" in r or "error" in r:
            continue
        title = r["title"][:55]
        print(f"  {r['event_id']:<8} {r['city']:<12} ens=[{r['ensemble_min']:.1f}..{r['ensemble_max']:.1f}] "
              f"fc_total_p={r['forecast_total_prob']:.3f} "
              f"signals={r['n_signals']}  hrs={r['hours_to_resolution']:.1f}h  | {title}")
        for s in r.get("signals", []):
            print(f"    SIGNAL: {s['bucket_kind']}{s['bucket_temp']}°C  "
                  f"forecast={s['forecast_prob']:.3f}  ask=${s['ask_price']:.4f}  "
                  f"depth={s['ask_depth']:.0f}  edge_BUY=+{s['edge_buy_pp']:.2f}pp")

    print(f"\nTotal signals (≥{MIN_EDGE_PP_LOG}pp edge, depth ≥{MIN_DEPTH_AT_ASK}): {n_signals_total}")


if __name__ == "__main__":
    asyncio.run(main())
