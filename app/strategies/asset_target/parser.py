"""Parser для Polymarket asset price-target market titles.

Examples:
  "Will BTC hit $120K by May 31?"
  "Will Bitcoin hit (HIGH) $200,000 in May?"
  "Will WTI Crude Oil (WTI) hit (HIGH) $110 in May?"
  "Will ETH reach $5,000 by 2026-06-30?"
  "Will SOL drop below $100 by June 30?"

Output: AssetTargetSpec с asset_symbol, threshold, deadline_iso, direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum


class TargetDirection(StrEnum):
    UPPER = "upper"  # "hit X" / "reach X" / "above X"
    LOWER = "lower"  # "drop below X" / "fall to X"
    UNKNOWN = "unknown"


@dataclass
class AssetTargetSpec:
    asset_symbol: str | None       # "BTC", "ETH", "SOL", "WTI", ...
    asset_canonical: str | None    # "Bitcoin", "Ethereum", ...
    threshold_usd: float | None
    deadline_iso: str | None        # YYYY-MM-DD UTC, end-of-day
    direction: TargetDirection
    asset_class: str | None        # "crypto" | "commodity" | "equity" | "unknown"
    raw_title: str = ""
    parse_confidence: str = "none"  # "exact" | "fuzzy" | "none"

    @property
    def tradable(self) -> bool:
        return (
            self.asset_symbol is not None
            and self.threshold_usd is not None
            and self.threshold_usd > 0
            and self.deadline_iso is not None
            and self.direction != TargetDirection.UNKNOWN
            and self.parse_confidence == "exact"
        )

    @property
    def cluster_key(self) -> str | None:
        """asset:deadline:direction_family per [GPT 6] spec."""
        if not all([self.asset_symbol, self.deadline_iso, self.direction != TargetDirection.UNKNOWN]):
            return None
        return f"{self.asset_symbol}:{self.deadline_iso}:{self.direction.value}"


# Asset symbol mapping. Все варианты titles → канонический символ + class.
_ASSETS: list[tuple[list[str], str, str, str]] = [
    # (aliases, symbol, canonical, asset_class)
    (["bitcoin", "btc"], "BTC", "Bitcoin", "crypto"),
    (["ethereum", "eth"], "ETH", "Ethereum", "crypto"),
    (["solana", "sol"], "SOL", "Solana", "crypto"),
    (["dogecoin", "doge"], "DOGE", "Dogecoin", "crypto"),
    (["xrp", "ripple"], "XRP", "XRP", "crypto"),
    (["cardano", "ada"], "ADA", "Cardano", "crypto"),
    (["avalanche", "avax"], "AVAX", "Avalanche", "crypto"),
    (["chainlink", "link"], "LINK", "Chainlink", "crypto"),
    (["polkadot", "dot"], "DOT", "Polkadot", "crypto"),
    (["polygon", "matic"], "MATIC", "Polygon", "crypto"),
    (["litecoin", "ltc"], "LTC", "Litecoin", "crypto"),
    (["wti crude oil", "wti", "crude oil"], "WTI", "WTI Crude Oil", "commodity"),
    (["brent crude", "brent oil", "brent"], "BRENT", "Brent Crude", "commodity"),
    (["gold"], "GOLD", "Gold", "commodity"),
    (["silver"], "SILVER", "Silver", "commodity"),
    (["nasdaq", "qqq"], "NDX", "Nasdaq 100", "equity_index"),
    (["s&p 500", "sp500", "s&p", "spx"], "SPX", "S&P 500", "equity_index"),
]

# Number formats:
#  $120K, $120,000, $120000, 120K, 120k, $5,000, $200,000.50
# Suffix должен быть отделён word boundary иначе "$5,000 by" → b matched as billion.
_NUMBER_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(k|K|m|M|b|B|thousand|million|billion)?\b",
)


def _parse_number(s: str) -> float | None:
    """Parse '$120K' / '$120,000' → 120000.0.

    Требует $ префикс чтобы не цеплять числа из дат типа "2026-06-30".
    Suffix matched только если идёт word boundary (\\b защита от "by" → billion).
    """
    m = _NUMBER_RE.search(s)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    # Suffix только если непосредственно после числа (без пробела) или с пробелом+буква.
    # _NUMBER_RE захватывает с \s*, но нам нужно отвергать suffix если за ним идёт ещё буква.
    suffix_raw = (m.group(2) or "")
    suffix = suffix_raw.lower()
    # Если suffix существует но это "b" или "k" а в strings типа "by"/"key" — already \b cuts.
    # Но дополнительно: не считаем suffix если строка имела ' ' между числом и suffix
    # (unless explicit word like "thousand"/"million"/"billion").
    if suffix in ("k", "m", "b") and " " in m.group(0)[len(raw):]:
        # Например "$100 b" или "$100 by" — у нас \b отрезал bg, но если был \s* match — ignore
        # Конкретно: m.group(0) например "$100 b" — после "100" один пробел перед "b". В normal text
        # "$100 by" → m.group(2) was matched as "b" if we had \b boundary. Reject if any whitespace.
        suffix = ""
    try:
        n = float(raw)
    except ValueError:
        return None
    if suffix == "k" or suffix == "thousand":
        n *= 1_000
    elif suffix == "m" or suffix == "million":
        n *= 1_000_000
    elif suffix == "b" or suffix == "billion":
        n *= 1_000_000_000
    return n


# Deadline patterns:
#  "by May 31"
#  "in May"
#  "by 2026-06-30"
#  "by end of June"
#  "by June 30, 2026"
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    return (next_month_first - timedelta(days=1)).day


def _parse_deadline(text: str, fallback_year: int) -> str | None:
    """Best-effort deadline extraction. Returns YYYY-MM-DD UTC end-of-day."""
    t = text.lower()

    # ISO yyyy-mm-dd
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass

    # "by Month DD, YYYY" or "by Month DD" — \bby для отсечения substring matches
    m = re.search(r"\bby\s+(\w+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", t)
    if m:
        mon = _MONTH_NAMES.get(m.group(1).lower())
        if mon:
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else fallback_year
            try:
                return date(year, mon, day).isoformat()
            except ValueError:
                pass

    # "in Month YYYY" or "in Month" → last day of month.
    # \bin защищает от substring matches типа "Bitco**in** ..." или "win**in**g".
    m = re.search(r"\bin\s+(\w+)(?:\s+(\d{4}))?", t)
    if m:
        mon = _MONTH_NAMES.get(m.group(1).lower())
        if mon:
            year = int(m.group(2)) if m.group(2) else fallback_year
            try:
                return date(year, mon, _last_day_of_month(year, mon)).isoformat()
            except ValueError:
                pass

    # "by end of Month"
    m = re.search(r"\bby\s+end\s+of\s+(\w+)", t)
    if m:
        mon = _MONTH_NAMES.get(m.group(1).lower())
        if mon:
            year = fallback_year
            try:
                return date(year, mon, _last_day_of_month(year, mon)).isoformat()
            except ValueError:
                pass

    # "by year YYYY"
    m = re.search(r"\bby\s+(\d{4})", t)
    if m:
        year = int(m.group(1))
        # End of year
        return date(year, 12, 31).isoformat()

    return None


# Direction keywords
_UPPER_PATTERNS = (
    "hit",
    "reach",
    "above",
    "exceed",
    "go to",
    "rise to",
    "climb to",
    "high",  # "hit (HIGH) $X"
)
_LOWER_PATTERNS = (
    "drop below",
    "fall below",
    "fall to",
    "drop to",
    "decline to",
    "low",  # "hit (LOW) $X"
)


def _detect_direction(title_lower: str) -> TargetDirection:
    has_lower = any(p in title_lower for p in _LOWER_PATTERNS)
    has_upper = any(p in title_lower for p in _UPPER_PATTERNS)
    if has_lower and not has_upper:
        return TargetDirection.LOWER
    if has_upper and not has_lower:
        return TargetDirection.UPPER
    # Default for ambiguous: looks like upper (most "hit $X" queries)
    if "hit" in title_lower or "reach" in title_lower:
        return TargetDirection.UPPER
    return TargetDirection.UNKNOWN


def _detect_asset(title_lower: str) -> tuple[str, str, str] | None:
    """Returns (symbol, canonical, class) or None."""
    for aliases, symbol, canonical, asset_class in _ASSETS:
        for alias in aliases:
            # Match alias as whole word/phrase, не substring shadow
            if re.search(rf"\b{re.escape(alias)}\b", title_lower):
                return (symbol, canonical, asset_class)
    return None


def parse_asset_target(title: str, current_year: int | None = None) -> AssetTargetSpec:
    """Главная entry-функция. Всегда возвращает AssetTargetSpec.
    `tradable` property покажет можно ли использовать.
    """
    if not title:
        return AssetTargetSpec(
            asset_symbol=None, asset_canonical=None, threshold_usd=None,
            deadline_iso=None, direction=TargetDirection.UNKNOWN,
            asset_class=None, raw_title="", parse_confidence="none",
        )

    fallback_year = current_year or datetime.now(UTC).year
    title_lower = title.lower()

    asset_match = _detect_asset(title_lower)
    if not asset_match:
        return AssetTargetSpec(
            asset_symbol=None, asset_canonical=None, threshold_usd=None,
            deadline_iso=None, direction=TargetDirection.UNKNOWN,
            asset_class=None, raw_title=title, parse_confidence="none",
        )

    symbol, canonical, asset_class = asset_match
    direction = _detect_direction(title_lower)
    threshold = _parse_number(title)
    deadline = _parse_deadline(title, fallback_year)

    confidence = "exact" if (
        threshold and deadline and direction != TargetDirection.UNKNOWN
    ) else "fuzzy"

    return AssetTargetSpec(
        asset_symbol=symbol,
        asset_canonical=canonical,
        threshold_usd=threshold,
        deadline_iso=deadline,
        direction=direction,
        asset_class=asset_class,
        raw_title=title,
        parse_confidence=confidence,
    )
