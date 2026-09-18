from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import app.db as db_module
from app.api.admin import runtime_state
from app.config import Settings, get_settings
from app.db import initialize_database


def _reset_db_state() -> None:
    if db_module._engine is not None:
        db_module._engine.dispose()
    db_module._engine = None
    db_module._session_factory = None


def _reset_runtime_state() -> None:
    runtime_state.started_at = datetime.now(UTC)
    runtime_state.paused = False
    runtime_state.mode = "paper_auto"
    runtime_state.last_tick_at = None
    runtime_state.last_report_at = None
    runtime_state.last_scan_markets = 0
    runtime_state.last_signals = 0
    runtime_state.last_orders = 0
    runtime_state.last_error = None
    runtime_state.manual_actions = []


@pytest.fixture()
def settings_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Settings]:
    def factory(
        app_mode: str = "paper_auto",
        enable_live_trading: str = "false",
    ) -> Settings:
        database_url = f"sqlite:///{tmp_path / f'{app_mode}.db'}"
        env = {
            "APP_NAME": "Polymarket Bot Test",
            "APP_ENV": "test",
            "APP_MODE": app_mode,
            "LOG_LEVEL": "INFO",
            "ENABLE_LIVE_TRADING": enable_live_trading,
            "USE_MOCK_DATA": "true",
            "DATABASE_URL": database_url,
            "API_HOST": "127.0.0.1",
            "API_PORT": "8000",
            "SCAN_INTERVAL_SECONDS": "1",
            "TRADE_INTERVAL_SECONDS": "1",
            "REPORT_INTERVAL_SECONDS": "1",
            "MAX_OPEN_POSITIONS": "2",
            "MAX_POSITION_SIZE": "10",
            "MAX_ORDER_NOTIONAL": "25",
            "DAILY_LOSS_LIMIT": "10",
            "MARKET_COOLDOWN_SECONDS": "60",
            "DEFAULT_ORDER_SIZE": "5",
            "N8N_WEBHOOK_URL": "",
            "N8N_WEBHOOK_SECRET": "test-secret",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        get_settings.cache_clear()
        _reset_db_state()
        _reset_runtime_state()

        settings = get_settings()
        initialize_database(settings)
        runtime_state.initialize(settings.app_mode.value)
        return settings

    yield factory

    get_settings.cache_clear()
    _reset_db_state()
    _reset_runtime_state()
