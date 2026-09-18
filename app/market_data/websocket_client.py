from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import websockets
from pydantic import BaseModel, Field

from app.config import Settings
from app.logger import get_logger


logger = get_logger(__name__)


class MarketEvent(BaseModel):
    event_type: str
    source: str
    market_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime


class MarketWebsocketClient:
    """Streaming abstraction for future real-time updates."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream(self) -> AsyncIterator[MarketEvent]:
        if self.settings.use_mock_data:
            yield MarketEvent(
                event_type="heartbeat",
                source="mock",
                payload={"message": "mock websocket heartbeat"},
                received_at=datetime.now(UTC),
            )
            return

        try:
            async with websockets.connect(self.settings.websocket_url) as websocket:
                # TODO: subscribe to the exact Polymarket channels once finalized.
                logger.info("websocket.connected | url=%s", self.settings.websocket_url)
                async for message in websocket:
                    yield self._parse_message(message)
        except Exception as exc:
            logger.warning("websocket.stream_failed | error=%s", exc)
            yield MarketEvent(
                event_type="error",
                source=self.settings.websocket_url,
                payload={"error": str(exc)},
                received_at=datetime.now(UTC),
            )

    def _parse_message(self, message: str) -> MarketEvent:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = {"raw": message}

        market_id = None
        event_type = "message"
        if isinstance(payload, dict):
            market_id = payload.get("market") or payload.get("market_id")
            event_type = str(payload.get("type") or payload.get("event") or "message")

        return MarketEvent(
            event_type=event_type,
            source=self.settings.websocket_url,
            market_id=str(market_id) if market_id is not None else None,
            payload=payload if isinstance(payload, dict) else {"payload": payload},
            received_at=datetime.now(UTC),
        )
