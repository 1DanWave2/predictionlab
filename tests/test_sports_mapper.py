"""Tests for SportsMarketMapper.

Per GPT debate spec: mapper bugs дороже strategy bugs. Эти tests должны
покрыть hard rules:
  * time_scope unknown = reject
  * non-moneyline = reject
  * fuzzy alias = no live trade
  * same teams + same date = same cluster_key
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.strategies.sports.mapper import (
    MappingConfidence,
    MarketType,
    SportsMarketMapper,
    TimeScope,
    WindowBucket,
)


def _future_iso(minutes_ahead: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes_ahead)).isoformat()


def test_nba_lakers_celtics_full_game_alias():
    """NBA Lakers vs Celtics за 60 мин до старта — full alias match, pre_90_30."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics",
        category="sports",
        end_date_iso=_future_iso(60),
    )
    assert scope.market_type == MarketType.MONEYLINE
    assert scope.time_scope == TimeScope.FULL_GAME_INCL_OT
    assert scope.team_a_id == "nba_lakers"
    assert scope.team_b_id == "nba_celtics"
    assert scope.mapping_confidence == MappingConfidence.ALIAS
    assert scope.window_bucket == WindowBucket.PRE_90_30
    assert scope.tradable is True
    # alias confidence — shadow OK, но live_tradable False
    assert scope.live_tradable is False


def test_nba_canonical_full_names_exact_match():
    """Полные имена → exact confidence → live_tradable."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Los Angeles Lakers @ Boston Celtics",
        end_date_iso=_future_iso(45),
    )
    assert scope.mapping_confidence == MappingConfidence.EXACT
    assert scope.tradable is True
    assert scope.live_tradable is True
    assert scope.window_bucket == WindowBucket.PRE_90_30


def test_player_prop_rejected():
    """Player prop = no moneyline → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Will LeBron James score 30+ points vs Celtics?",
        end_date_iso=_future_iso(60),
    )
    assert scope.market_type == MarketType.UNKNOWN
    assert scope.time_scope == TimeScope.UNKNOWN
    assert scope.tradable is False
    assert scope.live_tradable is False


def test_series_winner_rejected():
    """Series/tournament winner ≠ single-game ML → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Will Lakers advance to NBA Finals?",
        end_date_iso=_future_iso(60 * 24 * 7),
    )
    # "to advance" в _NON_MONEYLINE_TOKENS → reject
    assert scope.market_type == MarketType.UNKNOWN
    assert scope.tradable is False


def test_spread_market_rejected():
    """Spread market → not moneyline → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics -3.5 spread",
        end_date_iso=_future_iso(60),
    )
    assert scope.market_type == MarketType.UNKNOWN
    assert scope.tradable is False


def test_window_bucket_pre_30_10():
    """20 мин до старта — pre_30_10."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics",
        end_date_iso=_future_iso(20),
    )
    assert scope.window_bucket == WindowBucket.PRE_30_10
    assert scope.tradable is True


def test_window_bucket_pre_5_blocked():
    """3 мин до старта — pre_5_blocked → not tradable."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics",
        end_date_iso=_future_iso(3),
    )
    assert scope.window_bucket == WindowBucket.PRE_5_BLOCKED
    assert scope.tradable is False


def test_window_bucket_live():
    """Negative minutes_to_start (event already started) — LIVE bucket."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics",
        end_date_iso=_future_iso(-30),
    )
    assert scope.window_bucket == WindowBucket.LIVE


def test_window_bucket_too_far():
    """120 мин до старта → CLOSED (out of trading window)."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics",
        end_date_iso=_future_iso(120),
    )
    assert scope.window_bucket == WindowBucket.CLOSED
    assert scope.tradable is False


def test_cluster_key_stable_for_same_match():
    """Same match (различные title формы) → same cluster_key."""
    mapper = SportsMarketMapper()
    iso = _future_iso(60)
    scope1 = mapper.map(title="Lakers vs Celtics", end_date_iso=iso)
    scope2 = mapper.map(title="LA Lakers @ Boston", end_date_iso=iso)
    scope3 = mapper.map(title="Los Angeles Lakers vs Boston Celtics", end_date_iso=iso)
    assert scope1.cluster_key is not None
    assert scope1.cluster_key == scope2.cluster_key
    assert scope1.cluster_key == scope3.cluster_key


def test_cluster_key_different_for_different_dates():
    """Same teams но different даты → different clusters."""
    mapper = SportsMarketMapper()
    s1 = mapper.map("Lakers vs Celtics", end_date_iso=_future_iso(60))
    s2 = mapper.map("Lakers vs Celtics", end_date_iso=_future_iso(60 * 24 + 60))
    if s1.event_date != s2.event_date:
        assert s1.cluster_key != s2.cluster_key


def test_unknown_team_rejected():
    """Команда не в alias dict → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Random Team A vs Random Team B",
        end_date_iso=_future_iso(60),
    )
    assert scope.mapping_confidence == MappingConfidence.NONE
    assert scope.tradable is False


def test_first_half_rejected():
    """First half market — different time_scope → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Lakers vs Celtics first half winner",
        end_date_iso=_future_iso(60),
    )
    assert scope.time_scope == TimeScope.UNKNOWN
    assert scope.tradable is False


def test_no_separator_rejected():
    """Title без team separator → reject."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="NBA Championship Winner",
        end_date_iso=_future_iso(60),
    )
    assert scope.mapping_confidence == MappingConfidence.NONE
    assert scope.tradable is False


def test_nfl_chiefs_eagles_alias():
    """NFL alias matching."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Chiefs vs Eagles",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "nfl_chiefs"
    assert scope.team_b_id == "nfl_eagles"
    assert scope.tradable is True


def test_no_end_date_means_closed():
    """Нет end_date → window=CLOSED → not tradable."""
    mapper = SportsMarketMapper()
    scope = mapper.map(title="Lakers vs Celtics", end_date_iso=None)
    assert scope.window_bucket == WindowBucket.CLOSED
    assert scope.tradable is False


def test_tennis_tournament_prefix_alcaraz_sinner():
    """Tennis формат 'Tournament: Player A vs Player B' должен парситься."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Madrid Open: Alcaraz vs Sinner",
        end_date_iso=_future_iso(45),
    )
    assert scope.team_a_id == "atp_alcaraz"
    assert scope.team_b_id == "atp_sinner"
    assert scope.league == "atp"
    assert scope.tradable is True


def test_tennis_real_polymarket_format():
    """Реальный формат с сегодняшнего лога: 'Cagliari: Roman Andres Burruchaga vs Marcos Giron'."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Cagliari: Roman Andres Burruchaga vs Marcos Giron",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "atp_burruchaga"
    assert scope.team_b_id == "atp_giron"
    assert scope.league == "atp"


def test_wta_player_match():
    """WTA matching."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="La Bisbal: Sara Sorribes Tormo vs Elena Pridankina",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "wta_sorribes_tormo"
    assert scope.team_b_id == "wta_pridankina"
    assert scope.league == "wta"


def test_epl_soccer_match():
    """EPL teams matching."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Arsenal vs Chelsea",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "epl_arsenal"
    assert scope.team_b_id == "epl_chelsea"
    assert scope.league == "epl"


def test_tennis_cluster_key():
    """Tennis cluster_key должен быть стабилен между формами."""
    mapper = SportsMarketMapper()
    iso = _future_iso(60)
    s1 = mapper.map("Madrid: Alcaraz vs Sinner", end_date_iso=iso)
    s2 = mapper.map("Madrid: Carlos Alcaraz vs Jannik Sinner", end_date_iso=iso)
    assert s1.cluster_key == s2.cluster_key
    assert s1.cluster_key is not None


def test_ufc_fight_night_format():
    """UFC формат с '(Welterweight, Main Card)' suffix должен парситься."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="UFC Fight Night: Prates vs. Maddalena (Welterweight, Main Card)",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "ufc_prates"
    assert scope.team_b_id == "ufc_maddalena"
    assert scope.league == "ufc"
    assert scope.tradable is True


def test_ufc_pay_per_view_format():
    """UFC PPV format: 'UFC 305: Adesanya vs. Du Plessis'."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="UFC 305: Adesanya vs. Du Plessis",
        end_date_iso=_future_iso(45),
    )
    assert scope.team_a_id == "ufc_adesanya"
    assert scope.team_b_id == "ufc_dupless"
    assert scope.league == "ufc"


def test_nhl_capitals_islanders():
    """NHL добавили — Capitals vs Islanders должно работать."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Capitals vs Islanders",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "nhl_capitals"
    assert scope.team_b_id == "nhl_islanders"
    assert scope.league == "nhl"


def test_mlb_diamondbacks_cubs():
    """MLB добавили — Diamondbacks vs Cubs (это сегодня в логах был)."""
    mapper = SportsMarketMapper()
    scope = mapper.map(
        title="Arizona Diamondbacks vs. Chicago Cubs",
        end_date_iso=_future_iso(60),
    )
    assert scope.team_a_id == "mlb_diamondbacks"
    assert scope.team_b_id == "mlb_cubs"
    assert scope.league == "mlb"
