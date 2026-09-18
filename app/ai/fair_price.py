from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.logger import get_logger
from app.market_data.normalizer import NormalizedMarket


logger = get_logger(__name__)


SYSTEM_PROMPT = """You estimate the probability that prediction-market questions resolve YES.

Use your training knowledge to assess each question.
Output:
- fair_price: your probability estimate that YES resolves true (0.0 to 1.0)
- confidence: how much you trust your own estimate (0.0 to 1.0)

Confidence guide:
- 0.0-0.3: I lack the info or the question is too speculative — DEFAULT for unfamiliar topics
- 0.3-0.6: I have some grounding but uncertain
- 0.6-0.85: confident based on widely-known facts/trends
- 0.85+: near-certain

Be honest. If you don't know specifics, output low confidence.
Do not invent facts. Do not guess about future news/events you can't reason about.

Common-sense priors (use when applicable):
- Sports: home team historically wins ~55-60% in major leagues
- Re-election questions: incumbents typically have advantage
- Markets near 0.0 or 1.0 are usually correctly priced — give low edge
- Markets in 0.30-0.70 range are where mispricings live

Respond ONLY with JSON:
{"estimates":[{"market_id":"...","fair_price":0.0-1.0,"confidence":0.0-1.0,"reason":"<=20 words"}]}
No prose outside JSON."""


class AIFairPriceClient:
    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        base_url: str = "https://api.groq.com/openai/v1",
        cache_minutes: float = 30.0,
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
        est, ts = hit
        if (time.time() - ts) / 60.0 > self.cache_minutes:
            self._cache.pop(market_id, None)
            return None
        return est

    def _cache_put(self, market_id: str, est: dict[str, Any]) -> None:
        self._cache[market_id] = (est, time.time())

    async def estimate_batch(self, markets: list[NormalizedMarket]) -> dict[str, dict[str, Any]]:
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
                "current_price": round(m.mid_price, 4),
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
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": json.dumps({"markets": payload_markets}, ensure_ascii=False),
                            },
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse(content)
        except Exception as exc:
            self._errors += 1
            logger.warning("ai_fair_price.failed | err=%s", exc)
            return results

        for m in uncached:
            est = parsed.get(m.market_id)
            if est is None:
                continue
            results[m.market_id] = est
            self._cache_put(m.market_id, est)
        return results

    @staticmethod
    def _parse(content: str) -> dict[str, dict[str, Any]]:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {}
        items = obj.get("estimates") if isinstance(obj, dict) else None
        if not isinstance(items, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("market_id") or "")
            if not mid:
                continue
            try:
                fp = float(it.get("fair_price"))
                conf = float(it.get("confidence", 0.5))
            except (TypeError, ValueError):
                continue
            result[mid] = {
                "fair_price": max(0.01, min(0.99, fp)),
                "confidence": max(0.0, min(1.0, conf)),
                "reason": str(it.get("reason", ""))[:120],
            }
        return result

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "calls": self._calls,
            "errors": self._errors,
            "cache_size": len(self._cache),
        }


_default_client: AIFairPriceClient | None = None


def get_default_client() -> AIFairPriceClient | None:
    return _default_client


def set_default_client(client: AIFairPriceClient) -> None:
    global _default_client
    _default_client = client
