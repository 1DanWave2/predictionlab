from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.config import Settings
from app.db import db_session
from app.integrations.n8n import N8NClient
from app.integrations.payloads import DailyReportPayload
from app.models import PaperOrder, Position


class ReporterTask:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.n8n = N8NClient(settings)
        self._last_sent_at: datetime | None = None

    async def run_once(
        self,
        paused: bool = False,
        send_webhook: bool = False,
        bot_mode: str = "paper_auto",
    ) -> dict:
        with db_session() as session:
            orders = session.execute(select(PaperOrder)).scalars().all()
            positions = session.execute(select(Position)).scalars().all()

        total_realized_pnl = round(sum(position.realized_pnl for position in positions), 6)
        total_unrealized_pnl = round(sum(position.unrealized_pnl for position in positions), 6)
        open_positions = [position for position in positions if position.quantity > 0]
        closed_positions = [position for position in positions if position.quantity <= 0]
        gross_exposure = round(sum(position.quantity * position.avg_price for position in open_positions), 6)
        winning_positions = sum(1 for position in positions if (position.realized_pnl + position.unrealized_pnl) > 0)
        losing_positions = sum(1 for position in positions if (position.realized_pnl + position.unrealized_pnl) < 0)

        payload = DailyReportPayload(
            bot_mode=bot_mode,
            report_date=datetime.now(UTC).date().isoformat(),
            paused=paused,
            total_trades=len(orders),
            open_positions=len(open_positions),
            closed_positions=len(closed_positions),
            total_realized_pnl=total_realized_pnl,
            total_unrealized_pnl=total_unrealized_pnl,
            gross_exposure=gross_exposure,
            winning_positions=winning_positions,
            losing_positions=losing_positions,
        )

        should_send = send_webhook and self._should_emit_report()
        if should_send:
            await self.n8n.send_daily_report_event(payload)
            self._last_sent_at = datetime.now(UTC)

        return {
            "orders": len(orders),
            "positions": len(positions),
            "open_positions": len(open_positions),
            "mode": bot_mode,
            "report_payload": payload.model_dump(mode="json"),
            "report_sent": should_send,
        }

    def _should_emit_report(self) -> bool:
        if self._last_sent_at is None:
            return True
        elapsed = (datetime.now(UTC) - self._last_sent_at).total_seconds()
        return elapsed >= self.settings.report_interval_seconds
