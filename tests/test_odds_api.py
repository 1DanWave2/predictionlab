"""Tests for odds_api math: no-vig + haircut + consensus."""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.odds_api import (
    BookmakerOdds,
    OddsEvent,
    haircut,
)


def _make_book(key: str, oa: float, ob: float, sharp: bool = False) -> BookmakerOdds:
    return BookmakerOdds(
        book_key=key, team_a_odds=oa, team_b_odds=ob,
        last_update=datetime.now(UTC), is_sharp=sharp,
    )


def test_no_vig_simple_50_50():
    """Equal odds 2.0/2.0 (с overround=1.0) → 50/50."""
    b = _make_book("test", 2.0, 2.0)
    p1, p2 = b.no_vig_probs
    assert abs(p1 - 0.5) < 1e-6
    assert abs(p2 - 0.5) < 1e-6


def test_no_vig_strips_overround():
    """odds 1.91/1.91 (book имеет vig) → no-vig 50/50."""
    b = _make_book("test", 1.91, 1.91)
    p1, p2 = b.no_vig_probs
    assert abs(p1 - 0.5) < 1e-6
    assert abs(p2 - 0.5) < 1e-6
    # Raw probs sum > 1 (overround)
    assert (1 / 1.91) + (1 / 1.91) > 1.0


def test_no_vig_favorite_underdog():
    """Favorite 1.5 / underdog 3.0 → favorite ~ 66.6%."""
    b = _make_book("test", 1.5, 3.0)
    p1, p2 = b.no_vig_probs
    # Raw: p1 = 0.667, p2 = 0.333 → no-vig sum to 1 already
    assert abs(p1 - 2 / 3) < 1e-3
    assert abs(p2 - 1 / 3) < 1e-3


def test_no_vig_zero_for_invalid_odds():
    b = _make_book("test", 1.0, 1.0)  # impossible odds
    p1, p2 = b.no_vig_probs
    assert p1 == 0.0
    assert p2 == 0.0


def test_haircut_floor():
    """Haircut floor at 0.02 (2%) даже при low dispersion."""
    assert haircut(0.0) == 0.02
    assert haircut(0.01) == 0.02
    assert haircut(0.039) == 0.02


def test_haircut_dispersion_dominant():
    """При high dispersion → dispersion / 2."""
    assert haircut(0.10) == 0.05
    assert haircut(0.20) == 0.10


def test_consensus_with_sharp_book():
    """Если есть sharp book — он + main books, sharp_count >= 1."""
    event = OddsEvent(
        event_id="test", sport_key="basketball_nba",
        home_team="Lakers", away_team="Celtics",
        commence_time=datetime.now(UTC),
        books=[
            _make_book("pinnacle", 1.9, 1.9, sharp=True),
            _make_book("draftkings", 1.85, 2.0),
            _make_book("fanduel", 1.92, 1.95),
        ],
    )
    consensus = event.consensus()
    assert consensus is not None
    assert consensus["sharp_count"] == 1
    assert consensus["main_count"] == 2
    assert consensus["needs_higher_threshold"] is False
    assert 0.4 < consensus["home_prob"] < 0.6


def test_consensus_main_books_only_needs_higher_threshold():
    """Без sharp books — needs_higher_threshold=True."""
    event = OddsEvent(
        event_id="test", sport_key="basketball_nba",
        home_team="Lakers", away_team="Celtics",
        commence_time=datetime.now(UTC),
        books=[
            _make_book("draftkings", 1.85, 2.0),
            _make_book("fanduel", 1.92, 1.95),
        ],
    )
    consensus = event.consensus()
    assert consensus is not None
    assert consensus["sharp_count"] == 0
    assert consensus["needs_higher_threshold"] is True


def test_consensus_dispersion_calculation():
    """Dispersion = p75 - p25 при 4+ books."""
    event = OddsEvent(
        event_id="test", sport_key="basketball_nba",
        home_team="A", away_team="B",
        commence_time=datetime.now(UTC),
        books=[
            _make_book("pinnacle", 2.0, 2.0, sharp=True),     # 50%
            _make_book("draftkings", 1.5, 3.0),               # 66.7% home
            _make_book("fanduel", 2.5, 1.6),                  # 39%-ish home (depends on no-vig)
            _make_book("betmgm", 1.8, 2.2),                   # ~55% home
        ],
    )
    consensus = event.consensus()
    assert consensus is not None
    assert consensus["dispersion"] > 0  # Должен быть какой-то spread between books


def test_consensus_empty_books_returns_none():
    event = OddsEvent(
        event_id="test", sport_key="basketball_nba",
        home_team="A", away_team="B",
        commence_time=datetime.now(UTC),
        books=[],
    )
    assert event.consensus() is None
