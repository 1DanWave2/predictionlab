from __future__ import annotations

import argparse
import asyncio

import uvicorn

from app.api.admin import runtime_state
from app.api.main import create_app
from app.config import TradingMode, get_settings
from app.db import initialize_database
from app.execution.execution_router import ExecutionRouter
from app.integrations.n8n import N8NClient
from app.integrations.payloads import (
    PaperTradeClosePayload,
    PaperTradeOpenPayload,
    RiskAlertPayload,
    SignalPayload,
)
from app.logger import configure_logging, get_logger
from app.tasks.forward_returns import ForwardReturnsTask
from app.tasks.reporter import ReporterTask
from app.tasks.scanner import ScannerTask
from app.tasks.trader import TraderTask


logger = get_logger(__name__)


class BotRuntime:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.settings.app_mode = TradingMode.PAPER_AUTO
        runtime_state.initialize(self.settings.app_mode.value)
        self.scanner = ScannerTask(self.settings)
        self.router = ExecutionRouter(self.settings)
        self.trader = TraderTask(self.settings, self.router)
        self.reporter = ReporterTask(self.settings)
        self.n8n = N8NClient(self.settings)
        self._last_signals: dict[str, str] = {}
        self._last_report_date: str = ""

    async def run_once(self) -> None:
        self.settings.app_mode = TradingMode(runtime_state.mode)

        if runtime_state.paused:
            report_result = await self.reporter.run_once(
                paused=True,
                send_webhook=False,
                bot_mode=runtime_state.mode,
            )
            runtime_state.record_report()
            logger.info("runtime.paused | report_positions=%s", report_result["positions"])
            return

        scan_result = await self.scanner.run_once()
        await self._emit_signal_events(scan_result["opportunities"])

        trade_result = await self.trader.run_once(scan_result["opportunities"])
        await self._emit_trade_events(scan_result["opportunities"], trade_result["results"])

        report_result = await self.reporter.run_once(
            paused=False,
            send_webhook=True,
            bot_mode=runtime_state.mode,
        )
        runtime_state.record_tick(scan_result, trade_result)
        runtime_state.record_report()
        logger.info(
            "tick completed | mode=%s markets=%s opportunities=%s trades=%s positions=%s report_sent=%s",
            runtime_state.mode,
            scan_result["markets_seen"],
            len(scan_result["opportunities"]),
            trade_result["orders_created"],
            report_result["positions"],
            report_result["report_sent"],
        )

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                runtime_state.record_error(str(exc))
                logger.exception("runtime.tick_failed | error=%s", exc)
            await asyncio.sleep(self.settings.scan_interval_seconds)

    async def _emit_signal_events(self, opportunities: list[dict]) -> None:
        for opportunity in opportunities:
            mid = opportunity["market_id"]
            sig_key = f"{mid}:{opportunity['side']}"
            if sig_key in self._last_signals:
                continue
            self._last_signals[sig_key] = opportunity["side"]
            payload = SignalPayload(
                bot_mode=runtime_state.mode,
                market_id=mid,
                slug=opportunity["slug"],
                category=opportunity["category"],
                strategy=opportunity["strategy"],
                side=opportunity["side"],
                price=float(opportunity["price"]),
                fair_price=float(opportunity["fair_price"]),
                edge=float(opportunity["edge"]),
                confidence=float(opportunity["confidence"]),
                bid=float(opportunity["bid"]),
                ask=float(opportunity["ask"]),
                spread=float(opportunity["spread"]),
                volume=float(opportunity["volume"]),
                reason=str(opportunity["reason"]),
            )
            await self.n8n.send_signal_event(payload)

    async def _emit_trade_events(self, opportunities: list[dict], results: list[dict]) -> None:
        for opportunity, result in zip(opportunities, results):
            status = str(result.get("status", ""))
            if status in {"risk_rejected", "blocked"}:
                alert_key = f"risk:{opportunity.get('market_id')}:{opportunity.get('side')}"
                if alert_key not in self._last_signals:
                    self._last_signals[alert_key] = status
                    payload = RiskAlertPayload(
                        bot_mode=runtime_state.mode,
                        severity="warning",
                        reason=str(result.get("reason", status)),
                        market_id=opportunity.get("market_id"),
                        side=opportunity.get("side"),
                        strategy=opportunity.get("strategy"),
                        order=result.get("order", {}),
                    )
                    await self.n8n.send_risk_alert_event(payload)
                continue

            if status != "filled":
                continue

            position = result.get("position", {})
            side = str(result.get("side", "")).upper()
            if side == "BUY":
                payload = PaperTradeOpenPayload(
                    bot_mode=runtime_state.mode,
                    order_id=int(result["order_id"]),
                    market_id=str(result["market_id"]),
                    side=side,
                    strategy=str(opportunity["strategy"]),
                    price=float(result["price"]),
                    size=float(result["size"]),
                    quantity=float(position.get("quantity", 0.0)),
                    avg_price=float(position.get("avg_price", 0.0)),
                    realized_pnl=float(position.get("realized_pnl", 0.0)),
                    unrealized_pnl=float(position.get("unrealized_pnl", 0.0)),
                    note=str(opportunity["reason"]),
                )
                await self.n8n.send_paper_trade_open_event(payload)
            elif side == "SELL":
                payload = PaperTradeClosePayload(
                    bot_mode=runtime_state.mode,
                    order_id=int(result["order_id"]),
                    market_id=str(result["market_id"]),
                    side=side,
                    strategy=str(opportunity["strategy"]),
                    price=float(result["price"]),
                    size=float(result["size"]),
                    closed_quantity=float(result.get("closed_quantity", 0.0)),
                    realized_pnl_delta=float(result.get("realized_pnl_delta", 0.0)),
                    realized_pnl_total=float(position.get("realized_pnl", 0.0)),
                    unrealized_pnl=float(position.get("unrealized_pnl", 0.0)),
                    note=str(opportunity["reason"]),
                )
                await self.n8n.send_paper_trade_close_event(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket paper-trading bot MVP")
    parser.add_argument("--once", action="store_true", help="Run a single bot tick and exit.")
    parser.add_argument("--api-only", action="store_true", help="Run only the FastAPI server.")
    return parser.parse_args()


async def _serve_api() -> None:
    settings = get_settings()
    app = create_app()
    config = uvicorn.Config(
        app=app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


async def async_main(run_once: bool = False, api_only: bool = False) -> None:
    settings = get_settings()
    configure_logging(settings)
    initialize_database(settings)

    if settings.enable_live_trading or settings.app_mode == TradingMode.LIVE_AUTO:
        raise RuntimeError("Live trading remains blocked. Only shadow and paper_auto are allowed.")

    # Startup guard per [GPT 47] — refuse to start if registry has drift.
    from app.strategies.registry import validate_registry, STRATEGY_REGISTRY
    registry_errors = validate_registry()
    if registry_errors:
        logger.error("strategy_registry.validation_failed | errors:")
        for err in registry_errors:
            logger.error("  - %s", err)
        raise RuntimeError(
            f"Strategy registry has {len(registry_errors)} validation errors. "
            f"Refusing to start. Fix app/strategies/registry.py."
        )
    logger.info(
        "strategy_registry.ok | %s strategies registered, all metadata valid",
        len(STRATEGY_REGISTRY),
    )

    runtime = BotRuntime()
    logger.info(
        "starting runtime | requested_mode=%s effective_mode=%s db=%s api=%s:%s",
        settings.app_mode,
        runtime_state.mode,
        settings.database_url,
        settings.api_host,
        settings.api_port,
    )

    if run_once:
        await runtime.run_once()
        return

    if api_only:
        await _serve_api()
        return

    bot_task = asyncio.create_task(runtime.run_forever(), name="bot-runtime")
    api_task = asyncio.create_task(_serve_api(), name="api-server")
    fwd_returns_task = asyncio.create_task(
        ForwardReturnsTask(settings).loop_forever(),
        name="forward-returns",
    )
    done, pending = await asyncio.wait(
        {bot_task, api_task, fwd_returns_task},
        return_when=asyncio.FIRST_EXCEPTION,
    )
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(run_once=args.once, api_only=args.api_only))


if __name__ == "__main__":
    main()
