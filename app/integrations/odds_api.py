"""The Odds API client (https://the-odds-api.com).

Free tier: 500 req/month. Чтобы остаться в лимите, кэшируем ответы по
sport_key с TTL configurable (default 300 sec = 5 мин). Для pre-start window
30-90 мин это даёт ~1-2 fetch per event на trade decision.

Per AI debate spec:
  * h2h (moneyline) only — никаких spreads/totals
  * decimal odds format
  * region=us для US sports + uk для EPL/tennis
  * sharp_books: pinnacle, betfair_ex_uk, matchbook (если доступны)
  * main_books: draftkings, fanduel, betmgm, caesars, williamhill_us

No-vig probability calculation для 2-way market:
  p1_raw = 1 / odds1
  p2_raw = 1 / odds2
  overround = p1_raw + p2_raw  (~1.02-1.10)
  p1 = p1_raw / overround   ← no-vig prob
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.logger import get_logger


logger = get_logger(__name__)


SHARP_BOOKS = ("pinnacle", "betfair_ex_uk", "matchbook", "betfair_ex_eu")
MAIN_BOOKS = (
    "draftkings", "fanduel", "betmgm", "caesars",
    "williamhill_us", "pointsbetus", "bovada", "betrivers",
)


@dataclass
class BookmakerOdds:
    """Decoded h2h odds from one bookmaker for one event."""
    book_key: str
    team_a_odds: float  # decimal odds for team_a winning
    team_b_odds: float  # decimal odds for team_b winning
    last_update: datetime | None = None
    is_sharp: bool = False

    @property
    def no_vig_probs(self) -> tuple[float, float]:
        if self.team_a_odds <= 1.0 or self.team_b_odds <= 1.0:
            return (0.0, 0.0)
        p1_raw = 1.0 / self.team_a_odds
        p2_raw = 1.0 / self.team_b_odds
        total = p1_raw + p2_raw
        if total <= 0:
            return (0.0, 0.0)
        return (p1_raw / total, p2_raw / total)


@dataclass
class OddsEvent:
    """Aggregated odds for one event across bookmakers."""
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    books: list[BookmakerOdds] = field(default_factory=list)

    def consensus(self) -> dict[str, Any] | None:
        """Compute consensus no-vig prob for home/away.

        Logic:
          - If >=1 sharp book: use sharp + main books, threshold normal
          - Else: use main only, threshold +0.03 (caller responsibility)
          - dispersion = p75 - p25 of home_probs
          - Returns dict with home_prob, away_prob, dispersion, books_used,
            sharp_count, main_count, missing_books_flag.
        """
        if not self.books:
            return None

        sharp_books = [b for b in self.books if b.is_sharp]
        main_books = [b for b in self.books if not b.is_sharp]

        if sharp_books:
            sample = sharp_books + main_books
        else:
            sample = main_books

        if not sample:
            return None

        home_probs = []
        away_probs = []
        for b in sample:
            ph, pa = b.no_vig_probs
            if ph > 0 and pa > 0:
                home_probs.append(ph)
                away_probs.append(pa)

        if not home_probs:
            return None

        home_median = statistics.median(home_probs)
        away_median = statistics.median(away_probs)

        if len(home_probs) >= 4:
            q = statistics.quantiles(home_probs, n=4)
            dispersion = q[2] - q[0]  # p75 - p25
        elif len(home_probs) >= 2:
            dispersion = max(home_probs) - min(home_probs)
        else:
            dispersion = 0.0

        return {
            "home_prob": round(home_median, 6),
            "away_prob": round(away_median, 6),
            "home_prob_min": round(min(home_probs), 6),
            "home_prob_max": round(max(home_probs), 6),
            "dispersion": round(dispersion, 6),
            "sharp_count": len(sharp_books),
            "main_count": len(main_books),
            "books_used": [b.book_key for b in sample],
            "needs_higher_threshold": len(sharp_books) == 0,
        }


def haircut(dispersion: float) -> float:
    """Conservative haircut применяется к raw_edge."""
    return max(0.02, dispersion / 2.0)


class OddsApiClient:
    """The Odds API HTTP client с in-memory cache.

    Использование:
        client = OddsApiClient(api_key="...")
        events = await client.fetch_events("basketball_nba")
        for event in events:
            consensus = event.consensus()
            ...
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.the-odds-api.com/v4",
        cache_seconds: float = 300.0,
        regions: str = "us,uk",
        timeout_s: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache_seconds = cache_seconds
        self.regions = regions
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[float, list[OddsEvent]]] = {}
        self._last_remaining: int | None = None
        self._last_used: int | None = None

    def enabled(self) -> bool:
        return bool(self.api_key)

    async def fetch_events(self, sport_key: str) -> list[OddsEvent]:
        """Returns list of events for sport_key. Cached cache_seconds.

        Common sport_keys:
          basketball_nba, americanfootball_nfl, baseball_mlb, icehockey_nhl,
          soccer_epl, tennis_atp_us_open
        Full list: https://api.the-odds-api.com/v4/sports
        """
        if not self.enabled():
            return []

        now = time.monotonic()
        cached = self._cache.get(sport_key)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]

        url = f"{self.base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": self.regions,
            "markets": "h2h",
            "oddsFormat": "decimal",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                # Quota tracking
                self._last_remaining = int(resp.headers.get("x-requests-remaining", -1))
                self._last_used = int(resp.headers.get("x-requests-used", -1))
                logger.info(
                    "odds_api.fetch | sport=%s remaining=%s used=%s",
                    sport_key, self._last_remaining, self._last_used,
                )
                events = self._parse_response(resp.json(), sport_key)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "odds_api.http_error | sport=%s status=%s detail=%s",
                sport_key, exc.response.status_code, exc.response.text[:200],
            )
            return cached[1] if cached else []
        except Exception as exc:
            logger.warning("odds_api.fetch_failed | sport=%s error=%s", sport_key, exc)
            return cached[1] if cached else []

        self._cache[sport_key] = (now, events)
        return events

    def quota(self) -> dict[str, int | None]:
        return {"remaining": self._last_remaining, "used": self._last_used}

    @staticmethod
    def _parse_response(payload: list[dict[str, Any]], sport_key: str) -> list[OddsEvent]:
        events: list[OddsEvent] = []
        for item in payload:
            try:
                event_id = str(item.get("id", ""))
                home_team = str(item.get("home_team", ""))
                away_team = str(item.get("away_team", ""))
                commence_raw = item.get("commence_time", "")
                commence_time = datetime.fromisoformat(str(commence_raw).replace("Z", "+00:00"))
                if commence_time.tzinfo is None:
                    commence_time = commence_time.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue

            books: list[BookmakerOdds] = []
            for bm in item.get("bookmakers", []):
                book_key = str(bm.get("key", ""))
                last_update_raw = bm.get("last_update", "")
                last_update = None
                try:
                    last_update = datetime.fromisoformat(str(last_update_raw).replace("Z", "+00:00"))
                    if last_update.tzinfo is None:
                        last_update = last_update.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    pass

                # Find h2h market
                h2h_market = next(
                    (m for m in bm.get("markets", []) if m.get("key") == "h2h"),
                    None,
                )
                if not h2h_market:
                    continue

                outcomes = h2h_market.get("outcomes", [])
                if len(outcomes) != 2:
                    # Skip 3-way (soccer with draw) and other
                    continue

                # Map outcome.name to team_a (home) / team_b (away)
                home_odds = next(
                    (o.get("price") for o in outcomes if o.get("name") == home_team),
                    None,
                )
                away_odds = next(
                    (o.get("price") for o in outcomes if o.get("name") == away_team),
                    None,
                )
                if home_odds is None or away_odds is None:
                    continue

                books.append(
                    BookmakerOdds(
                        book_key=book_key,
                        team_a_odds=float(home_odds),
                        team_b_odds=float(away_odds),
                        last_update=last_update,
                        is_sharp=book_key in SHARP_BOOKS,
                    )
                )

            events.append(
                OddsEvent(
                    event_id=event_id,
                    sport_key=sport_key,
                    home_team=home_team,
                    away_team=away_team,
                    commence_time=commence_time,
                    books=books,
                )
            )

        return events
