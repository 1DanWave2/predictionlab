"""SportsMarketMapper — mapping Polymarket sports markets to bookmaker odds.

Output: SportsMatchScope с teams, league, date, time_scope, market_type,
window_bucket и mapping_confidence.

Hard rules (per AI debate финальная spec):
  * time_scope == unknown → reject (no trade)
  * market_type != moneyline → reject
  * mapping_confidence == fuzzy → no live trade (shadow only)
  * mapping_confidence == none → no log entry

Window buckets — для разных порогов entry edge:
  pre_90_30: 30-90 мин до старта (normal threshold 10%)
  pre_30_10: 10-30 мин до старта (stricter 12% + spread <= 3%)
  pre_10_5:  5-10 мин до старта (16% + exact + depth check)
  pre_5_blocked: < 5 мин до старта (no new entries)
  live: после старта (live in-game — shadow-only first 48h)
  closed: после конца event
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_ALIASES_PATH = Path(__file__).parent / "team_aliases.yaml"

# Полные слова и фразы которые сигналят что market не binary moneyline.
# Если найдено хотя бы одно — time_scope становится "unknown" → reject.
_NON_MONEYLINE_TOKENS = (
    " spread", " total", " over", " under", " props", " prop ",
    "first half", "second half", " quarter", "1st quarter",
    "2nd quarter", "3rd quarter", "4th quarter", " period",
    "first period", "second period", "third period",
    " inning ", "first inning", " series ", "advance to",
    "to lift", "to reach", "to qualify", "to make finals",
    "championship winner", " finals", " playoff ", "win series",
    " mvp ", " rookie ", " award ",
    "draw no bet", "double chance", " btts ", "both teams to score",
    "correct score", "first goal", "first score",
    "by player", "player to ", " score 2+", " score 3+", " hat-trick",
    " yards", " strikeouts", " rebounds", " assists", " points",
    "regulation only", "ft only", "90 minutes", "ninety minutes",
)

# Tokens которые ОК (часть full-game moneyline даже если включают OT и т.д.)
# Эти не вызывают reject. Просто заметка для маркера.
_FULLGAME_OK_TOKENS = (
    "incl. ot", "including ot", "incl ot", "including overtime",
    "incl. extra innings", "with overtime", "moneyline",
    " ml ", "winner", "to win",
)


class TimeScope(StrEnum):
    FULL_GAME_INCL_OT = "full_game_incl_ot"
    REGULATION_ONLY = "regulation_only"
    FIRST_HALF = "first_half"
    PERIOD = "period"
    UNKNOWN = "unknown"


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    PROP = "prop"
    SERIES = "series"
    UNKNOWN = "unknown"


class MappingConfidence(StrEnum):
    EXACT = "exact"      # обе команды matched через canonical+league+date
    ALIAS = "alias"      # обе команды matched через alias
    FUZZY = "fuzzy"      # одна или обе через partial match — shadow only
    NONE = "none"        # не удалось распарсить — no log


class WindowBucket(StrEnum):
    PRE_90_30 = "pre_90_30"
    PRE_30_10 = "pre_30_10"
    PRE_10_5 = "pre_10_5"
    PRE_5_BLOCKED = "pre_5_blocked"
    LIVE = "live"
    CLOSED = "closed"


@dataclass
class SportsMatchScope:
    league: str | None
    team_a_id: str | None
    team_b_id: str | None
    team_a_canonical: str | None
    team_b_canonical: str | None
    event_date: str | None  # YYYY-MM-DD UTC
    market_type: MarketType
    time_scope: TimeScope
    window_bucket: WindowBucket
    mapping_confidence: MappingConfidence
    minutes_to_start: float | None
    cluster_key: str | None = None
    raw_title: str = ""
    debug: dict[str, Any] = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        """Можно ли торговать на этом scope (любой size)."""
        if self.market_type != MarketType.MONEYLINE:
            return False
        if self.time_scope == TimeScope.UNKNOWN:
            return False
        if self.mapping_confidence == MappingConfidence.NONE:
            return False
        if self.window_bucket in (WindowBucket.PRE_5_BLOCKED, WindowBucket.CLOSED):
            return False
        return True

    @property
    def live_tradable(self) -> bool:
        """Можно ли торговать live (real money)."""
        if not self.tradable:
            return False
        if self.mapping_confidence != MappingConfidence.EXACT:
            return False
        if self.window_bucket == WindowBucket.LIVE:
            return False
        return True


@lru_cache(maxsize=1)
def _load_aliases() -> dict[str, dict[str, dict[str, Any]]]:
    if not _ALIASES_PATH.exists():
        return {}
    with _ALIASES_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalize(text: str) -> str:
    """Lowercase + strip punctuation/extra whitespace для alias matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _match_team(text: str, league: str | None = None) -> tuple[str | None, str | None, MappingConfidence] | None:
    """Returns (team_id, canonical_name, confidence) or None.

    Если league передан — ищем только в нём (быстрее, точнее).
    Если нет — пробегаем все leagues, exact > alias > fuzzy.
    """
    aliases = _load_aliases()
    norm = _normalize(text)
    if not norm:
        return None

    leagues = [league] if league and league in aliases else list(aliases.keys())

    for lg in leagues:
        teams = aliases.get(lg, {})
        for team_id, data in teams.items():
            canonical = _normalize(data.get("canonical", ""))
            team_aliases = [_normalize(a) for a in data.get("aliases", [])]
            if norm == canonical:
                return (team_id, data["canonical"], MappingConfidence.EXACT)
            if norm in team_aliases:
                return (team_id, data["canonical"], MappingConfidence.ALIAS)

    # Fuzzy — substring match (только если ничего не нашли точного)
    for lg in leagues:
        teams = aliases.get(lg, {})
        for team_id, data in teams.items():
            canonical = _normalize(data.get("canonical", ""))
            if canonical and (canonical in norm or norm in canonical):
                # Минимум 4 символа чтобы не цеплять "la" → "los angeles"
                if len(norm) >= 4 and len(canonical) >= 4:
                    return (team_id, data["canonical"], MappingConfidence.FUZZY)

    return None


def _detect_market_type_and_scope(title: str) -> tuple[MarketType, TimeScope]:
    """По title определяем market_type и time_scope. Без exact match → unknown.

    Возвращаем UNKNOWN при любом сомнении. Лучше пропустить сделку
    чем купить wrong outcome.
    """
    norm = title.lower()

    # Reject: явные не-moneyline признаки
    for tok in _NON_MONEYLINE_TOKENS:
        if tok in norm:
            return (MarketType.UNKNOWN, TimeScope.UNKNOWN)

    # Detect explicit time_scope от title
    time_scope = TimeScope.FULL_GAME_INCL_OT  # default для US sports moneyline
    if "regulation only" in norm or "90 minutes" in norm or "ft only" in norm:
        time_scope = TimeScope.REGULATION_ONLY

    return (MarketType.MONEYLINE, time_scope)


def _split_title_into_teams(title: str) -> tuple[str, str] | None:
    """Парсим Polymarket title на 2 team-токена.

    Поддерживаемые форматы:
      "Lakers vs Celtics"
      "Lakers @ Celtics"
      "Lakers - Celtics"
      "Lakers vs. Celtics"
      "Tournament: Player A vs Player B"  (tennis Polymarket формат)
      "Madrid Open: Alcaraz vs Sinner"
    """
    cleaned = title

    # Tournament prefix detection (tennis): "Madrid Open: Alcaraz vs Sinner"
    # Берём часть после ":" если она содержит team separator
    if ":" in cleaned:
        after_colon = cleaned.split(":", 1)[1].strip()
        for sep in [" vs ", " vs. ", " v "]:
            if sep in after_colon.lower():
                cleaned = after_colon
                break

    # Простые делители
    for sep in [" vs ", " vs. ", " @ ", " - ", " v "]:
        if sep in cleaned.lower():
            idx = cleaned.lower().index(sep)
            left = cleaned[:idx].strip()
            right = cleaned[idx + len(sep):].strip()
            # Очистить trailing markers ("incl. OT", "?", etc.)
            right = re.split(r"[?(]|incl\.|including", right, flags=re.IGNORECASE)[0].strip()
            if left and right:
                return (left, right)

    return None


def _detect_window_bucket(minutes_to_start: float | None) -> WindowBucket:
    if minutes_to_start is None:
        return WindowBucket.CLOSED
    if minutes_to_start < 0:
        return WindowBucket.LIVE
    if minutes_to_start < 5:
        return WindowBucket.PRE_5_BLOCKED
    if minutes_to_start < 10:
        return WindowBucket.PRE_10_5
    if minutes_to_start < 30:
        return WindowBucket.PRE_30_10
    if minutes_to_start <= 90:
        return WindowBucket.PRE_90_30
    return WindowBucket.CLOSED  # > 90 мин — слишком далеко (по GPT 5)


def _build_cluster_key(
    league: str | None,
    team_a_id: str | None,
    team_b_id: str | None,
    event_date: str | None,
) -> str | None:
    """Stable cluster key для correlation guard в risk manager."""
    if not all([league, team_a_id, team_b_id, event_date]):
        return None
    teams = sorted([team_a_id, team_b_id])
    return f"{league}:{event_date}:{teams[0]}:{teams[1]}"


class SportsMarketMapper:
    """Map Polymarket market → SportsMatchScope.

    Usage:
        mapper = SportsMarketMapper()
        scope = mapper.map(market)
        if scope.tradable:
            # log + maybe trade
            ...
    """

    def __init__(self) -> None:
        # Pre-load aliases чтобы fail fast если YAML битый
        _load_aliases()

    def map(
        self,
        title: str,
        category: str | None = None,
        end_date_iso: str | None = None,
        league_hint: str | None = None,
        game_start_iso: str | None = None,
    ) -> SportsMatchScope:
        """Главный метод. Возвращает scope (всегда возвращает, не None).
        Решение tradable/live_tradable делается через scope.tradable.
        """
        debug: dict[str, Any] = {"raw_title": title}

        # Step 1 — market type / time scope
        market_type, time_scope = _detect_market_type_and_scope(title)
        debug["market_type_detected"] = market_type.value
        debug["time_scope_detected"] = time_scope.value

        if market_type != MarketType.MONEYLINE:
            return SportsMatchScope(
                league=None, team_a_id=None, team_b_id=None,
                team_a_canonical=None, team_b_canonical=None,
                event_date=None,
                market_type=market_type,
                time_scope=time_scope,
                window_bucket=WindowBucket.CLOSED,
                mapping_confidence=MappingConfidence.NONE,
                minutes_to_start=None,
                cluster_key=None,
                raw_title=title,
                debug=debug,
            )

        # Step 2 — split title into teams
        teams_split = _split_title_into_teams(title)
        if not teams_split:
            debug["reject"] = "no_team_separator"
            return SportsMatchScope(
                league=None, team_a_id=None, team_b_id=None,
                team_a_canonical=None, team_b_canonical=None,
                event_date=None, market_type=market_type, time_scope=TimeScope.UNKNOWN,
                window_bucket=WindowBucket.CLOSED,
                mapping_confidence=MappingConfidence.NONE,
                minutes_to_start=None, cluster_key=None,
                raw_title=title, debug=debug,
            )

        left_text, right_text = teams_split
        debug["left_text"] = left_text
        debug["right_text"] = right_text

        # Step 3 — match each team
        league = league_hint
        a = _match_team(left_text, league)
        b = _match_team(right_text, league)

        if not a or not b:
            debug["reject"] = "team_unmatched"
            debug["a"] = a
            debug["b"] = b
            return SportsMatchScope(
                league=None, team_a_id=None, team_b_id=None,
                team_a_canonical=None, team_b_canonical=None,
                event_date=None, market_type=market_type, time_scope=time_scope,
                window_bucket=WindowBucket.CLOSED,
                mapping_confidence=MappingConfidence.NONE,
                minutes_to_start=None, cluster_key=None,
                raw_title=title, debug=debug,
            )

        team_a_id, team_a_canonical, conf_a = a
        team_b_id, team_b_canonical, conf_b = b

        # Common league guess: prefix перед "_"
        derived_league = team_a_id.split("_")[0] if team_a_id else None
        if not league:
            league = derived_league

        # Confidence = worst of two
        ranks = {
            MappingConfidence.EXACT: 3,
            MappingConfidence.ALIAS: 2,
            MappingConfidence.FUZZY: 1,
            MappingConfidence.NONE: 0,
        }
        confidence = conf_a if ranks[conf_a] <= ranks[conf_b] else conf_b

        # Step 4 — window bucket per [GPT 21] fix:
        # Используем gameStartTime (когда матч стартует), а не endDate (резолюция market'а).
        # endDate часто на дни/недели позже start of game (e.g. tennis ATP).
        # Fallback на endDate только если gameStartTime отсутствует.
        minutes_to_start = None
        event_date = None
        if game_start_iso:
            try:
                gs = datetime.fromisoformat(str(game_start_iso).replace("Z", "+00:00"))
                if gs.tzinfo is None:
                    gs = gs.replace(tzinfo=UTC)
                minutes_to_start = (gs - datetime.now(UTC)).total_seconds() / 60.0
                event_date = gs.date().isoformat()
            except (ValueError, TypeError):
                debug["game_start_parse_error"] = game_start_iso
        if minutes_to_start is None and end_date_iso:
            try:
                end = datetime.fromisoformat(str(end_date_iso).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=UTC)
                minutes_to_start = (end - datetime.now(UTC)).total_seconds() / 60.0
                event_date = end.date().isoformat()
            except (ValueError, TypeError):
                debug["end_date_parse_error"] = end_date_iso

        window_bucket = _detect_window_bucket(minutes_to_start)
        cluster_key = _build_cluster_key(league, team_a_id, team_b_id, event_date)

        return SportsMatchScope(
            league=league,
            team_a_id=team_a_id,
            team_b_id=team_b_id,
            team_a_canonical=team_a_canonical,
            team_b_canonical=team_b_canonical,
            event_date=event_date,
            market_type=market_type,
            time_scope=time_scope,
            window_bucket=window_bucket,
            mapping_confidence=confidence,
            minutes_to_start=minutes_to_start,
            cluster_key=cluster_key,
            raw_title=title,
            debug=debug,
        )
