from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.admin import runtime_state
from app.api.dashboard import router as dashboard_router
from app.api.webhooks import router as webhooks_router
from app.config import TradingMode, get_settings
from app.db import initialize_database


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize_database(settings)
        initial_mode = settings.app_mode.value if hasattr(settings.app_mode, "value") else str(settings.app_mode)
        if initial_mode == TradingMode.LIVE_AUTO.value:
            initial_mode = TradingMode.PAPER_AUTO.value
        runtime_state.initialize(initial_mode)
        yield

    api = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Universal Polymarket paper-trading bot MVP API",
        lifespan=lifespan,
    )

    @api.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "mode": runtime_state.mode,
            "paused": runtime_state.paused,
            "live_enabled": False,
        }

    @api.get("/")
    async def root() -> dict:
        return {
            "service": settings.app_name,
            "mode": runtime_state.mode,
            "docs": "/docs",
        }

    api.include_router(admin_router, tags=["admin"])
    api.include_router(dashboard_router, tags=["dashboard"])
    api.include_router(webhooks_router, prefix="/webhook", tags=["webhook"])
    return api


app = create_app()
