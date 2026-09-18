from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

import httpx

from app.logger import get_logger


logger = get_logger(__name__)

_HISTORY_MAX = 500
_HISTORY_WINDOW_SEC = 24 * 3600


SYSTEM_PROMPT = """You are a probability scorer for a Polymarket prediction-market bot.
Each candidate is a proposed trade: BUY/SELL a binary outcome at given price.

For EACH candidate output a SCORE 0-100 that means: probability THIS specific trade WINS at resolution.
- 80-100: very likely a win, high conviction. The market is clearly mispricing.
- 65-79: leaning win, moderate conviction. Some real edge but uncertainty.
- 50-64: roughly fair. No real edge — SKIP.
- 0-49: leaning loss. Don't trade.

Hard SKIP triggers (score must be < 50):
- price < 0.25 or > 0.85 — asymmetric noise dominates
- spread > 0.015 — cost eats edge
- |edge| < 0.025 — too thin to overcome cost
- volume < 1500 — illiquid
- subjective/manipulable/ambiguous question wording
- catalyst-driven (election day, game in progress) where noise overwhelms

Be STRICT. Most candidates should score 50-65. Only flag genuine edge >= 70.
A score of 80+ means: I would bet my own money on this resolving as the bot expects.

Look at:
- Is the question objective and verifiable?
- Does the price seem clearly off vs. what a reasonable observer would say?
- Is the bot's "fair_price" credible or just smoothed noise?
- Time to resolution (very short = noise, very long = uncertainty)
- Spread (wide = expensive)

Decision = "GO" if score >= 65, else "SKIP".

Respond ONLY with JSON:
{"candidates":[{"market_id":"...","decision":"GO"|"SKIP","score":0-100,"confidence":0.0-1.0,"reason":"<=15 words"}]}
No prose outside JSON."""


class AIVetoClient:
    def __init__(
        self,
        api_key: str,
        model: str = "llama-3.3-70b-versatile",
        base_url: str = "https://api.groq.com/openai/v1",
        cache_minutes: float = 10.0,
        timeout_s: float = 15.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.cache_minutes = cache_minutes
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_MAX)
        self._batches = 0
        self._errors = 0

    def enabled(self) -> bool:
        return bool(self.api_key)

    def _cache_get(self, market_id: str) -> dict[str, Any] | None:
        hit = self._cache.get(market_id)
        if hit is None:
            return None
        decision, ts = hit
        if (time.time() - ts) / 60.0 > self.cache_minutes:
            self._cache.pop(market_id, None)
            return None
        return decision

    def _cache_put(self, market_id: str, decision: dict[str, Any]) -> None:
        self._cache[market_id] = (decision, time.time())

    def _record(self, market_id: str, decision: dict[str, Any]) -> None:
        self._history.append(
            {
                "ts": time.time(),
                "market_id": market_id,
                "decision": decision.get("decision", "GO"),
                "reason": decision.get("reason", ""),
                "confidence": decision.get("confidence", 0.0),
            }
        )

    def stats(self, window_seconds: float = _HISTORY_WINDOW_SEC) -> dict[str, Any]:
        now = time.time()
        recent = [h for h in self._history if now - h["ts"] <= window_seconds]
        go = sum(1 for h in recent if h["decision"] == "GO")
        skip = sum(1 for h in recent if h["decision"] == "SKIP")
        skips = [h for h in recent if h["decision"] == "SKIP"][-10:]
        return {
            "enabled": self.enabled(),
            "window_hours": round(window_seconds / 3600, 1),
            "batches": self._batches,
            "errors": self._errors,
            "total": len(recent),
            "go": go,
            "skip": skip,
            "skip_rate": round(skip / len(recent) * 100, 1) if recent else 0.0,
            "last_skips": [
                {
                    "market_id": s["market_id"],
                    "reason": s["reason"],
                    "confidence": round(s["confidence"], 2),
                }
                for s in skips
            ],
        }

    async def evaluate(self, candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not candidates or not self.enabled():
            return {c["market_id"]: {"decision": "GO", "confidence": 1.0, "reason": "ai-disabled"} for c in candidates}

        decisions: dict[str, dict[str, Any]] = {}
        uncached: list[dict[str, Any]] = []
        for cand in candidates:
            cached = self._cache_get(cand["market_id"])
            if cached is not None:
                decisions[cand["market_id"]] = cached
                self._record(cand["market_id"], cached)
            else:
                uncached.append(cand)

        if not uncached:
            return decisions
        self._batches += 1

        payload_candidates = [
            {
                "market_id": c["market_id"],
                "question": c.get("question") or c.get("slug", "")[:80],
                "category": c.get("category"),
                "side": c.get("side"),
                "price": round(float(c.get("price", 0)), 4),
                "fair_price": round(float(c.get("fair_price", 0)), 4),
                "edge": round(float(c.get("edge", 0)), 4),
                "bid": round(float(c.get("bid", 0)), 4),
                "ask": round(float(c.get("ask", 0)), 4),
                "spread": round(float(c.get("spread", 0)), 4),
                "volume": round(float(c.get("volume", 0)), 0),
                "confidence": round(float(c.get("confidence", 0)), 3),
            }
            for c in uncached
        ]

        user_msg = json.dumps({"candidates": payload_candidates}, ensure_ascii=False)

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
                            {"role": "user", "content": user_msg},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_response(content)
        except Exception as exc:
            self._errors += 1
            logger.warning("ai_veto.failed | err=%s — falling back to GO", exc)
            for c in uncached:
                fallback = {"decision": "GO", "confidence": 0.5, "reason": "ai-error"}
                decisions[c["market_id"]] = fallback
                self._record(c["market_id"], fallback)
            return decisions

        for c in uncached:
            dec = parsed.get(c["market_id"]) or {"decision": "GO", "confidence": 0.5, "reason": "ai-missing"}
            decisions[c["market_id"]] = dec
            self._cache_put(c["market_id"], dec)
            self._record(c["market_id"], dec)
        return decisions

    @staticmethod
    def _parse_response(content: str) -> dict[str, dict[str, Any]]:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return {}
        items: list[Any] = []
        if isinstance(obj, list):
            items = obj
        elif isinstance(obj, dict):
            for key in ("candidates", "decisions", "results", "items"):
                if key in obj and isinstance(obj[key], list):
                    items = obj[key]
                    break
            if not items and "market_id" in obj:
                items = [obj]
        result: dict[str, dict[str, Any]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("market_id") or "")
            if not mid:
                continue
            dec = str(it.get("decision") or "GO").upper()
            if dec not in {"GO", "SKIP"}:
                dec = "GO"
            score = float(it.get("score", 50.0))
            result[mid] = {
                "decision": dec,
                "score": max(0.0, min(100.0, score)),
                "confidence": float(it.get("confidence", 0.5)),
                "reason": str(it.get("reason", ""))[:120],
            }
        return result


_default_client: AIVetoClient | None = None


def get_default_client() -> AIVetoClient | None:
    return _default_client


def set_default_client(client: AIVetoClient) -> None:
    global _default_client
    _default_client = client
