from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from sqlalchemy import delete, select

from app.ai.veto import get_default_client as get_ai_veto_client
from app.api.admin import _serialize_position, _serialize_trade, runtime_state
from app.config import get_settings
from app.db import db_session
from app.integrations.n8n import get_webhook_secret
from app.logger import get_logger
from app.models import PaperOrder, Position


logger = get_logger(__name__)
router = APIRouter()


class ManualDecisionPayload(BaseModel):
    market_id: str | None = None
    trade_id: int | None = None
    signal_id: str | None = None
    note: str = ""
    reviewer: str = "unknown"


class TelegramCommandPayload(BaseModel):
    """Telegram command from n8n webhook"""
    command: str  # /status, /pause, /resume, /set_mode, /reset_paper_account, /positions, /trades
    args: dict[str, Any] = {}  # {'mode': 'shadow'} for /set_mode, empty for others
    user_id: str | None = None
    chat_id: str | None = None


class TelegramCallbackPayload(BaseModel):
    """Telegram callback button action from n8n webhook"""
    action: str  # PAUSE, RESUME, STATUS, POSITIONS, TRADES, RESET_PAPER_ACCOUNT
    user_id: str | None = None
    chat_id: str | None = None


def _verify_token(token: str | None) -> None:
    expected = get_webhook_secret()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid webhook token")


@router.post("/manual-approve")
async def manual_approve(
    payload: ManualDecisionPayload,
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _verify_token(x_webhook_token)
    runtime_state.record_manual_action("approve", payload.model_dump(mode="json"))
    return {"received": True, "action": "approve", "status": "stubbed"}


@router.post("/manual-reject")
async def manual_reject(
    payload: ManualDecisionPayload,
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _verify_token(x_webhook_token)
    runtime_state.record_manual_action("reject", payload.model_dump(mode="json"))
    return {"received": True, "action": "reject", "status": "stubbed"}


@router.post("/telegram-command")
async def telegram_command(
    payload: TelegramCommandPayload,
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Handle Telegram commands forwarded from n8n"""
    _verify_token(x_webhook_token)
    settings = get_settings()

    logger.info(
        "telegram.command | command=%s user=%s chat=%s",
        payload.command,
        payload.user_id,
        payload.chat_id,
    )

    result = {"success": False, "command": payload.command, "data": None}

    if payload.command == "/status":
        result["data"] = runtime_state.snapshot()
        result["success"] = True
    elif payload.command == "/pause":
        runtime_state.pause()
        result["data"] = {"status": "paused", "mode": runtime_state.mode}
        result["success"] = True
    elif payload.command == "/resume":
        runtime_state.resume()
        result["data"] = {"status": "running", "mode": runtime_state.mode}
        result["success"] = True
    elif payload.command == "/set_mode":
        mode = payload.args.get("mode")
        if mode:
            try:
                runtime_state.set_mode(mode)
                result["data"] = {"status": "ok", "mode": runtime_state.mode}
                result["success"] = True
            except ValueError as exc:
                result["error"] = str(exc)
        else:
            result["error"] = "mode not provided"
    elif payload.command == "/reset_paper_account":
        if settings.enable_live_trading:
            result["error"] = "live trading is enabled, reset blocked"
        else:
            with db_session() as session:
                session.execute(delete(PaperOrder))
                session.execute(delete(Position))
            runtime_state.last_orders = 0
            runtime_state.last_signals = 0
            runtime_state.last_scan_markets = 0
            result["data"] = {"status": "reset", "mode": runtime_state.mode}
            result["success"] = True
    elif payload.command == "/positions":
        with db_session() as session:
            rows = session.execute(select(Position).order_by(Position.id.asc())).scalars().all()
        result["data"] = [_serialize_position(r) for r in rows]
        result["success"] = True
    elif payload.command == "/trades":
        with db_session() as session:
            rows = session.execute(
                select(PaperOrder).order_by(PaperOrder.id.desc()).limit(20)
            ).scalars().all()
        result["data"] = [_serialize_trade(r) for r in rows]
        result["success"] = True
    elif payload.command == "/ai_stats":
        client = get_ai_veto_client()
        if client is None:
            result["error"] = "ai veto client not initialized"
        else:
            result["data"] = client.stats()
            result["success"] = True
    else:
        result["error"] = f"unknown command: {payload.command}"

    runtime_state.record_manual_action(f"telegram_{payload.command}", payload.model_dump(mode="json"))
    return result


@router.post("/telegram-callback")
async def telegram_callback(
    payload: TelegramCallbackPayload,
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Handle Telegram callback button actions forwarded from n8n"""
    _verify_token(x_webhook_token)

    logger.info(
        "telegram.callback | action=%s user=%s chat=%s",
        payload.action,
        payload.user_id,
        payload.chat_id,
    )

    # Map callback actions to commands
    action_map = {
        "PAUSE": "/pause",
        "RESUME": "/resume",
        "STATUS": "/status",
        "POSITIONS": "/positions",
        "TRADES": "/trades",
        "RESET_PAPER_ACCOUNT": "/reset_paper_account",
        "AI_STATS": "/ai_stats",
    }

    command = action_map.get(payload.action)
    if not command:
        return {"success": False, "action": payload.action, "error": f"unknown action: {payload.action}"}

    # Delegate to telegram_command logic
    telegram_payload = TelegramCommandPayload(
        command=command,
        args={},
        user_id=payload.user_id,
        chat_id=payload.chat_id,
    )
    return await telegram_command(telegram_payload, x_webhook_token)


class NewsAlertPayload(BaseModel):
    """Inbound news from n8n RSS/Telegram aggregator.

    Per AI debate Round 3: alert-only logging для labeled dataset.
    Через 100+ alerts → решаем включать ли auto-trade.

    Example payload (from n8n):
      {
        "source": "reuters_breaking",
        "headline": "BREAKING: Iran agrees to ceasefire...",
        "body": "Full article text...",
        "url": "https://reuters.com/...",
        "published_at": "2026-05-02T10:30:00Z"
      }
    """
    source: str
    headline: str
    body: str = ""
    url: str | None = None
    published_at: str | None = None  # ISO 8601


@router.post("/news-alert")
async def news_alert_ingest(
    payload: NewsAlertPayload,
    x_webhook_token: str | None = Header(default=None, alias="X-Webhook-Token"),
) -> dict[str, Any]:
    """Принять news event от n8n RSS/Telegram pipeline.

    Сохраняем в news_alerts table со status='ingested'. Async classification
    job (TBD) подхватит и добавит AI consensus + market matching.

    Returns: alert_id (для последующего status check).
    """
    _verify_token(x_webhook_token)

    from datetime import UTC, datetime
    from app.models import NewsAlert

    pub_at = None
    if payload.published_at:
        try:
            pub_at = datetime.fromisoformat(payload.published_at.replace("Z", "+00:00"))
            if pub_at.tzinfo is None:
                pub_at = pub_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pub_at = None

    with db_session() as session:
        alert = NewsAlert(
            source=payload.source[:64],
            headline=payload.headline[:1000],
            body=payload.body[:5000],
            url=payload.url[:512] if payload.url else None,
            published_at=pub_at,
            status="ingested",
        )
        session.add(alert)
        session.flush()
        alert_id = alert.id

    logger.info(
        "news_alert.ingested | id=%s source=%s headline=%s",
        alert_id, payload.source, payload.headline[:80],
    )
    return {"success": True, "alert_id": alert_id, "status": "ingested"}


@router.get("/news-alerts/recent")
async def news_alerts_recent(limit: int = 20) -> dict[str, Any]:
    """Recent news alerts для dashboard / debug."""
    from app.models import NewsAlert
    with db_session() as session:
        rows = session.execute(
            select(NewsAlert).order_by(NewsAlert.id.desc()).limit(min(limit, 100))
        ).scalars().all()
        items = [
            {
                "id": a.id,
                "source": a.source,
                "headline": a.headline,
                "published_at": a.published_at.isoformat() if a.published_at else None,
                "status": a.status,
                "ai_confidence": a.ai_confidence,
                "ai_direction": a.ai_direction,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ]
    return {"count": len(items), "items": items}
