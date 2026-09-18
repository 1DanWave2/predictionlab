from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket


logger = get_logger(__name__)


SYSTEM_PROMPT = """You classify Polymarket prediction markets by RESOLUTION TYPE.
You do NOT predict probabilities. You only classify how clean the resolution is.

Allowed labels:
- "objective_scheduled_event": clear objective outcome with known timing (election day, scheduled vote)
- "sports_or_match_result": specific sports game or tournament result
- "official_count_or_measurable_outcome": measurable count/value (poll, GDP, vote share)
- "calendar_deadline_outcome": will X happen by date Y, where X is verifiable
- "ambiguous": subjective, manipulable, or unclear resolution criteria

Respond ONLY with JSON:
{"results":[{"market_id":"...","label":"<one of above>","quality":"clear|ambiguous"}]}
No prose outside JSON."""


CLEAN_LABELS = {
    "objective_scheduled_event",
    "sports_or_match_result",
    "official_count_or_measurable_outcome",
    "calendar_deadline_outcome",
}


class AIMarketClassifier:
    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        base_url: str = "https://api.groq.com/openai/v1",
        cache_minutes: float = 60.0,
        timeout_s: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.cache_minutes = cache_minutes
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._calls = 0
        self._errors = 0

    def enabled(self) -> bool:
        return bool(self.api_key)

    def _cache_get(self, market_id: str) -> dict[str, Any] | None:
        hit = self._cache.get(market_id)
        if hit is None:
            return None
        rec, ts = hit
        if (time.time() - ts) / 60.0 > self.cache_minutes:
            self._cache.pop(market_id, None)
            return None
        return rec

    def _cache_put(self, market_id: str, rec: dict[str, Any]) -> None:
        self._cache[market_id] = (rec, time.time())

    def is_clean_market(self, market_id: str) -> bool:
        rec = self._cache_get(market_id)
        if rec is None:
            return False
        return rec.get("label") in CLEAN_LABELS and rec.get("quality") == "clear"

    async def classify_batch(self, markets: list[NormalizedMarket]) -> dict[str, dict[str, Any]]:
        if not markets or not self.enabled():
            return {}
        results: dict[str, dict[str, Any]] = {}
        uncached: list[NormalizedMarket] = []
        for m in markets:
            cached = self._cache_get(m.market_id)
            if cached is not None:
                results[m.market_id] = cached
            else:
                uncached.append(m)
        if not uncached:
            return results

        payload_markets = [
            {
                "market_id": m.market_id,
                "question": m.question[:300],
                "category": m.category,
            }
            for m in uncached
        ]

        self._calls += 1
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps({"markets": payload_markets}, ensure_ascii=False)},
                        ],
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                parsed = self._parse(content)
        except Exception as exc:
            self._errors += 1
            logger.warning("ai_classifier.failed | err=%s", exc)
            return results

        for m in uncached:
            rec = parsed.get(m.market_id)
            if rec is None:
                rec = {"label": "ambiguous", "quality": "ambiguous"}
            results[m.market_id] = rec
            self._cache_put(m.market_id, rec)
        return results

    @staticmethod
    def _parse(content: str) -> dict[str, dict[str, Any]]:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {}
        items = obj.get("results") if isinstance(obj, dict) else None
        if not isinstance(items, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("market_id") or "")
            if not mid:
                continue
            result[mid] = {
                "label": str(it.get("label", "ambiguous")),
                "quality": str(it.get("quality", "ambiguous")),
            }
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "calls": self._calls,
            "errors": self._errors,
            "cache_size": len(self._cache),
        }


_default: AIMarketClassifier | None = None


def get_default() -> AIMarketClassifier | None:
    return _default


def set_default(c: AIMarketClassifier) -> None:
    global _default
    _default = c
