from __future__ import annotations

import os

import httpx
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.integrations.payloads import (
    DailyReportPayload,
    PaperTradeClosePayload,
    PaperTradeOpenPayload,
    RiskAlertPayload,
    SignalPayload,
)
from app.logger import get_logger


logger = get_logger(__name__)


def get_webhook_secret() -> str:
    settings_secret = get_settings().n8n_webhook_secret
    if settings_secret:
        return settings_secret
    return os.getenv("N8N_WEBHOOK_SECRET") or os.getenv("WEBHOOK_SECRET_TOKEN", "local-dev-webhook-secret")


class N8NClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.secret_token = get_webhook_secret()

    async def send_event(self, payload: BaseModel) -> bool:
        if not self.settings.n8n_webhook_url:
            logger.info(
                "n8n.send_skipped | reason=no_webhook_url event_type=%s",
                getattr(payload, "event_type", "unknown"),
            )
            return False

        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Token": self.secret_token,
            "X-Event-Type": str(getattr(payload, "event_type", "unknown")),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.settings.n8n_webhook_url,
                    json=payload.model_dump(mode="json"),
                    headers=headers,
                )
                response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "n8n.send_failed | event_type=%s error=%s",
                getattr(payload, "event_type", "unknown"),
                exc,
            )
            return False
        logger.info("n8n.sent | event_type=%s", getattr(payload, "event_type", "unknown"))
        return True

    async def send_signal_event(self, payload: SignalPayload) -> bool:
        return await self.send_event(payload)

    async def send_paper_trade_open_event(self, payload: PaperTradeOpenPayload) -> bool:
        return await self.send_event(payload)

    async def send_paper_trade_close_event(self, payload: PaperTradeClosePayload) -> bool:
        return await self.send_event(payload)

    async def send_risk_alert_event(self, payload: RiskAlertPayload) -> bool:
        return await self.send_event(payload)

    async def send_daily_report_event(self, payload: DailyReportPayload) -> bool:
        return await self.send_event(payload)
