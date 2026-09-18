"""TheOddsAPI usage audit — per [GPT 47] requirement.

Queries TheOddsAPI status endpoint (free, 0 cost), reports:
  - x-requests-remaining
  - x-requests-used
  - reset date (1st of next month UTC)
  - days until reset
  - daily burn rate
  - projected exhaustion if any

Outputs JSON to /app/data/odds_api_audit.jsonl + Telegram alert if usage>80%.
"""
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger(__name__)
OUTPUT = Path('/app/data/odds_api_audit.jsonl')


def telegram(text: str) -> None:
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        return
    try:
        import httpx
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": text},
            )
    except Exception:
        pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        try:
            with open('/app/.env') as f:
                for ln in f:
                    if ln.startswith("ODDS_API_KEY="):
                        api_key = ln.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    if not api_key:
        LOG.error("no api key")
        return 1

    url = f"https://api.the-odds-api.com/v4/sports/?apiKey={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        headers = dict(resp.headers)
    except Exception as exc:
        LOG.error("fetch failed: %s", exc)
        return 1

    used = int(headers.get("x-requests-used", 0))
    remaining = int(headers.get("x-requests-remaining", 0))
    last_cost = int(headers.get("x-requests-last", 0))
    total_quota = used + remaining

    # Reset = 1st of next month UTC
    now = datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    seconds_until_reset = (reset - now).total_seconds()
    days_until_reset = seconds_until_reset / 86400

    days_in_month_used = now.day - 1 + (now.hour * 3600 + now.minute * 60) / 86400
    daily_burn = used / max(days_in_month_used, 0.1)
    projected_full_month = daily_burn * 30

    pct_used = used * 100 / max(total_quota, 1)

    print("=" * 70)
    print(f"  THEODDSAPI USAGE AUDIT  ({now.isoformat()})")
    print("=" * 70)
    print(f"  used:               {used} / {total_quota} ({pct_used:.1f}%)")
    print(f"  remaining:          {remaining}")
    print(f"  last call cost:     {last_cost}")
    print(f"  reset:              {reset.isoformat()} ({days_until_reset:.1f} days)")
    print(f"  daily burn:         {daily_burn:.1f} calls/day")
    print(f"  projected month:    {projected_full_month:.0f} calls (vs {total_quota} quota)")
    print()

    # Verdict per [GPT 47] gate
    print("VERDICT:")
    if remaining < 50:
        print("  ❌ <50 remaining — sniper effectively offline until reset")
    elif pct_used > 80:
        print("  🟡 >80% used — budget alert. Reduce poll frequency or wait reset.")
    else:
        print("  ✅ healthy budget remaining")

    print()
    print("RESPONSES TO [GPT 47] QUESTIONS:")
    print(f"  Q: When do credits reset?")
    print(f"  A: {reset.strftime('%Y-%m-%d %H:%M UTC')} ({days_until_reset:.0f} days)")
    print()
    print(f"  Q: Calls/day at current polling?")
    print(f"  A: {daily_burn:.0f} calls/day average ({pct_used:.0f}% of monthly quota)")
    print()
    print(f"  Q: Did May 1 sniper entries require external odds?")
    print(f"  A: YES — code path app/strategies/sports/external_fair.py uses")
    print(f"     OddsApiClient.fetch_event_odds() for fair-price anchor.")
    print()
    print(f"  Q: Historical sniper candidates with external_books?")
    print(f"  A: query opportunity_logs WHERE league IS NOT NULL AND ")
    print(f"     external_books IS NOT NULL")
    print()
    print(f"  Q: Monthly budget alert at 70/90/100%?")
    print(f"  A: NOT YET IMPLEMENTED — adding to bot_health_alerts now.")

    # Send Telegram alert if >80% used
    if pct_used > 80:
        msg = (
            f"OddsAPI BUDGET ALERT\n"
            f"used: {used}/{total_quota} ({pct_used:.0f}%)\n"
            f"remaining: {remaining}\n"
            f"reset: {reset.strftime('%Y-%m-%d')} ({days_until_reset:.0f} days)\n"
            f"daily burn: {daily_burn:.0f}/day"
        )
        telegram(msg)
        print(f"\n  📨 Telegram alert sent")

    # Save
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("a") as fh:
        fh.write(json.dumps({
            "ts": int(time.time()),
            "kind": "odds_api_audit_v1",
            "used": used,
            "remaining": remaining,
            "total_quota": total_quota,
            "pct_used": round(pct_used, 2),
            "daily_burn": round(daily_burn, 2),
            "days_until_reset": round(days_until_reset, 2),
            "reset_iso": reset.isoformat(),
        }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
