"""Kalshi-Polymarket exact-match seed map per [GPT 25] guidance.

Manual mapping >> noisy keyword similarity. 11 high-confidence pairs.
Used by cross_market_radar for arbitrage detection.

Verification status:
  exact: confirmed via API + resolution rule check
  fuzzy: needs PM slug verification before live deploy
"""
from __future__ import annotations


SEED_MAP: list[dict] = [
    # NBA Finals 2026
    {"category": "sports_nba", "pm_slug": "will-the-san-antonio-spurs-win-the-2026-nba-finals",
     "kalshi_ticker": "KXNBA-26-SAS", "match_type": "exact"},
    {"category": "sports_nba", "pm_slug": "will-the-detroit-pistons-win-the-2026-nba-finals",
     "kalshi_ticker": "KXNBA-26-DET", "match_type": "exact"},
    {"category": "sports_nba", "pm_slug": "will-the-oklahoma-city-thunder-win-the-2026-nba-finals",
     "kalshi_ticker": "KXNBA-26-OKC", "match_type": "exact_verify_slug"},
    # NHL Stanley Cup 2026
    {"category": "sports_nhl", "pm_slug": "will-the-carolina-hurricanes-win-the-2026-nhl-stanley-cup",
     "kalshi_ticker": "KXNHL-26-CAR", "match_type": "exact"},
    {"category": "sports_nhl", "pm_slug": "will-the-colorado-avalanche-win-the-2026-nhl-stanley-cup",
     "kalshi_ticker": "KXNHL-26-COL", "match_type": "exact_verify_slug"},
    # FIFA World Cup 2026
    {"category": "sports_soccer", "pm_slug": "will-croatia-win-the-2026-fifa-world-cup",
     "kalshi_ticker": "KXMENWORLDCUP-26-HR", "match_type": "exact"},
    {"category": "sports_soccer", "pm_slug": "will-usa-win-the-2026-fifa-world-cup-467",
     "kalshi_ticker": "KXMENWORLDCUP-26-US", "match_type": "exact"},
    {"category": "sports_soccer", "pm_slug": "will-sweden-win-the-2026-fifa-world-cup",
     "kalshi_ticker": "KXMENWORLDCUP-26-SE", "match_type": "exact"},
    # Fed rate cuts 2026 (both count 25bps=1 cut identically)
    {"category": "macro_fed", "pm_slug": "will-no-fed-rate-cuts-happen-in-2026",
     "kalshi_ticker": "KXRATECUTCOUNT-26DEC31-T0", "match_type": "exact"},
    {"category": "macro_fed", "pm_slug": "will-1-fed-rate-cut-happen-in-2026",
     "kalshi_ticker": "KXRATECUTCOUNT-26DEC31-T1", "match_type": "exact"},
    # CPI (slug verification needed)
    {"category": "macro_cpi", "pm_slug": "will-cpi-rise-more-than-0pt6-percent-in-may-2026",
     "kalshi_ticker": "KXCPI-26MAY-T0.6", "match_type": "fuzzy_verify"},
]


def get_kalshi_for_pm_slug(pm_slug: str) -> dict | None:
    for pair in SEED_MAP:
        if pair["pm_slug"] == pm_slug:
            return pair
    return None
