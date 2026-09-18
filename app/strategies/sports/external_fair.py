"""ExternalSportsFairPrice — соединяет SportsMarketMapper и OddsApiClient.

Принимает Polymarket market → находит matching bookmaker event → вычисляет
no-vig consensus → возвращает ExternalFairResult с tradable_edge и всем
metadata для opportunity log.

Per AI debate spec:
  external_fair = no_vig_median_consensus
  raw_edge = external_fair - poly_ask  (для BUY YES: hometeam=YES_outcome)
  haircut = max(0.02, dispersion / 2)
  tradable_edge = raw_edge - haircut

Sport key mapping:
  nba → basketball_nba
  nhl → icehockey_nhl
  nfl → americanfootball_nfl
  mlb → baseball_mlb
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.integrations.odds_api import OddsApiClient, OddsEvent, haircut as compute_haircut
from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket
from app.strategies.sports.mapper import (
    MappingConfidence,
    SportsMarketMapper,
    SportsMatchScope,
    _match_team,
    _normalize,
)


logger = get_logger(__name__)


SPORT_KEY_BY_LEAGUE: dict[str, str] = {
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",
    # Tennis: ATP/WTA cycle через несколько турниров. The Odds API имеет
    # отдельные feed на каждый текущий турнир. Используем aggregate keys —
    # будут работать для активных турниров.
    "atp": "tennis_atp_us_open",  # placeholder; будет переключаться
    "wta": "tennis_wta_us_open",
    "epl": "soccer_epl",
    "ufc": "mma_mixed_martial_arts",
}


@dataclass
class ExternalFairResult:
    """Результат: external_fair + edge + metadata для opportunity log."""

    poly_market_id: str
    scope: SportsMatchScope
    matched_event_id: str | None
    matched_home: str | None
    matched_away: str | None

    external_fair: float | None  # no-vig median prob для YES outcome
    book_dispersion: float | None
    sharp_count: int
    main_count: int
    books_used: list[str]
    needs_higher_threshold: bool

    raw_edge: float | None
    haircut: float | None
    tradable_edge: float | None

    # Latency tracking
    odds_fetched_at: datetime | None
    bookmaker_last_update: datetime | None

    # Reject reason если внешний fair не получен
    reject_reason: str = ""

    debug: dict[str, Any] = field(default_factory=dict)


class ExternalSportsFairPrice:
    """Composite: mapper + odds_client → ExternalFairResult.

    Usage:
        ext = ExternalSportsFairPrice(odds_client)
        result = await ext.get_external_fair(market)
        if result.tradable_edge is not None and result.tradable_edge >= 0.10:
            # potential entry
            ...
    """

    def __init__(self, odds_client: OddsApiClient) -> None:
        self.odds_client = odds_client
        self.mapper = SportsMarketMapper()

    async def get_external_fair(
        self,
        market: NormalizedMarket,
    ) -> ExternalFairResult:
        """Главная entry-функция."""
        question = market.raw.get("question") or market.raw.get("title") or market.slug or ""
        end_date_iso = market.raw.get("endDate") or market.raw.get("end_date") or market.raw.get("endDateIso")
        # Per [GPT 21] mapper fix: предпочитаем gameStartTime для accurate window detection.
        game_start_iso = market.raw.get("gameStartTime") or market.raw.get("game_start_time")

        scope = self.mapper.map(
            title=question,
            category=market.category,
            end_date_iso=end_date_iso,
            game_start_iso=game_start_iso,
        )

        # Empty result template
        result = ExternalFairResult(
            poly_market_id=market.market_id,
            scope=scope,
            matched_event_id=None,
            matched_home=None,
            matched_away=None,
            external_fair=None,
            book_dispersion=None,
            sharp_count=0,
            main_count=0,
            books_used=[],
            needs_higher_threshold=True,
            raw_edge=None,
            haircut=None,
            tradable_edge=None,
            odds_fetched_at=None,
            bookmaker_last_update=None,
            reject_reason="",
            debug={"question": question},
        )

        # Reject if scope не tradable
        if not scope.tradable:
            result.reject_reason = (
                f"scope_not_tradable: market_type={scope.market_type.value} "
                f"time_scope={scope.time_scope.value} "
                f"window={scope.window_bucket.value} "
                f"mapping_confidence={scope.mapping_confidence.value}"
            )
            return result

        if not scope.league or scope.league not in SPORT_KEY_BY_LEAGUE:
            result.reject_reason = f"unsupported_league: {scope.league}"
            return result

        sport_key = SPORT_KEY_BY_LEAGUE[scope.league]
        events = await self.odds_client.fetch_events(sport_key)
        result.odds_fetched_at = datetime.now(UTC)

        if not events:
            result.reject_reason = "no_odds_events_returned"
            return result

        # Find matching event by team names
        matched = self._find_matching_event(events, scope)
        if not matched:
            result.reject_reason = "no_event_match_in_odds"
            result.debug["candidates_count"] = len(events)
            return result

        result.matched_event_id = matched.event_id
        result.matched_home = matched.home_team
        result.matched_away = matched.away_team
        result.bookmaker_last_update = max(
            (b.last_update for b in matched.books if b.last_update),
            default=None,
        )

        consensus = matched.consensus()
        if not consensus:
            result.reject_reason = "no_consensus_from_books"
            return result

        # Decide which side of YES this Polymarket market represents.
        # Polymarket question: "Will [team] win?". Сначала пробуем найти которая
        # команда из scope соответствует "YES outcome" — это та что в title как
        # центральная team_a (left side в "Lakers vs Celtics" parsed from title).
        yes_team_id = scope.team_a_id  # left side of title
        # Map matched.home_team / away_team back to team_id
        home_match = _match_team(matched.home_team, scope.league)
        away_match = _match_team(matched.away_team, scope.league)
        home_id = home_match[0] if home_match else None
        away_id = away_match[0] if away_match else None

        if yes_team_id == home_id:
            external_fair = consensus["home_prob"]
        elif yes_team_id == away_id:
            external_fair = consensus["away_prob"]
        else:
            result.reject_reason = (
                f"yes_team_orientation_unclear: yes={yes_team_id} "
                f"home={home_id} away={away_id}"
            )
            return result

        result.external_fair = external_fair
        result.book_dispersion = consensus["dispersion"]
        result.sharp_count = consensus["sharp_count"]
        result.main_count = consensus["main_count"]
        result.books_used = consensus["books_used"]
        result.needs_higher_threshold = consensus["needs_higher_threshold"]

        if market.best_ask <= 0:
            result.reject_reason = "no_poly_ask"
            return result

        # Edge calculation per AI debate spec
        raw_edge = external_fair - market.best_ask
        haircut = compute_haircut(consensus["dispersion"])
        tradable_edge = raw_edge - haircut

        result.raw_edge = round(raw_edge, 6)
        result.haircut = round(haircut, 6)
        result.tradable_edge = round(tradable_edge, 6)

        return result

    def _find_matching_event(
        self,
        events: list[OddsEvent],
        scope: SportsMatchScope,
    ) -> OddsEvent | None:
        """Match Polymarket scope to bookmaker event by team names + date."""
        if not scope.team_a_id or not scope.team_b_id:
            return None

        # Build target set
        target = {scope.team_a_id, scope.team_b_id}

        for ev in events:
            home_match = _match_team(ev.home_team, scope.league)
            away_match = _match_team(ev.away_team, scope.league)
            if not home_match or not away_match:
                continue
            event_teams = {home_match[0], away_match[0]}
            if event_teams != target:
                continue

            # Date check (scope.event_date is YYYY-MM-DD)
            if scope.event_date:
                if ev.commence_time.date().isoformat() != scope.event_date:
                    continue

            return ev

        return None
