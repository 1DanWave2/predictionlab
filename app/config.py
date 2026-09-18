from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    SHADOW = "shadow"
    PAPER_AUTO = "paper_auto"
    LIVE_AUTO = "live_auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Polymarket Paper Bot"
    app_env: str = "dev"
    app_mode: TradingMode = TradingMode.PAPER_AUTO
    log_level: str = "INFO"

    enable_live_trading: bool = False
    use_mock_data: bool = True

    database_url: str = "sqlite:///./paper_bot.db"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    websocket_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/"

    scan_interval_seconds: float = Field(default=5.0, ge=0.1)
    trade_interval_seconds: float = Field(default=5.0, ge=0.1)
    report_interval_seconds: float = Field(default=3600.0, ge=1.0)

    max_open_positions: int = Field(default=5, ge=1)
    max_position_size: float = Field(default=200.0, ge=1.0)
    max_order_notional: float = Field(default=10.0, ge=1.0)
    min_order_notional: float = Field(default=2.0, ge=0.1)
    high_conf_notional: float = Field(default=10.0, ge=1.0)
    high_conf_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    daily_loss_limit: float = Field(default=10.0, ge=1.0)
    market_cooldown_seconds: int = Field(default=120, ge=0)
    stoploss_cooldown_seconds: int = Field(default=7200, ge=60)
    max_trades_per_market: int = Field(default=1, ge=1)
    default_order_size: float = Field(default=4.0, ge=0.1)
    initial_paper_balance: float = Field(default=100.0, ge=0.0)
    take_profit_pct: float = Field(default=0.30, ge=0.01)
    stop_loss_pct: float = Field(default=0.03, ge=0.01)
    min_sl_age_minutes: float = Field(default=15.0, ge=0.0)
    stale_exit_hours: float = Field(default=48.0, ge=0.1)
    hard_stop_min_age_minutes: float = Field(default=3.0, ge=0.0)
    hard_stop_max_spread: float = Field(default=0.15, ge=0.0, le=1.0)

    n8n_webhook_url: str = ""
    n8n_webhook_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    ai_veto_enabled: bool = False
    groq_api_key: str = ""
    ai_veto_model: str = "llama-3.3-70b-versatile"
    ai_veto_base_url: str = "https://api.groq.com/openai/v1"
    ai_veto_cache_minutes: float = Field(default=10.0, ge=0.0)
    ai_veto_timeout_s: float = Field(default=15.0, ge=1.0)

    ai_fair_price_enabled: bool = False
    ai_fair_price_model: str = "llama-3.3-70b-versatile"
    ai_fair_price_cache_minutes: float = Field(default=30.0, ge=0.0)
    ai_fair_price_timeout_s: float = Field(default=20.0, ge=1.0)
    ai_fair_price_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    ai_fair_price_min_edge: float = Field(default=0.10, ge=0.0, le=1.0)

    consensus_drift_enabled: bool = False
    consensus_drift_only: bool = False
    classifier_cache_minutes: float = Field(default=60.0, ge=0.0)

    # Sports Sniper external-edge strategy (per AI debate Day-0 spec).
    sniper_enabled: bool = False  # master toggle
    sniper_live_enabled: bool = False  # live tiny trades (canary)
    odds_api_key: str = ""
    odds_api_base_url: str = "https://api.the-odds-api.com/v4"
    odds_api_cache_seconds: float = Field(default=300.0, ge=10.0)
    odds_api_regions: str = "us,uk"
    sniper_live_max_size_usd: float = Field(default=5.0, ge=1.0)
    sniper_live_max_open: int = Field(default=1, ge=1, le=5)
    sniper_live_daily_stop_usd: float = Field(default=4.0, ge=1.0)
    sniper_live_weekly_stop_usd: float = Field(default=8.0, ge=1.0)
    sniper_sport_keys: str = "basketball_nba,icehockey_nhl,americanfootball_nfl,baseball_mlb"

    # Asset Target Sniper (price-target binaries: BTC/ETH/WTI/etc).
    asset_target_enabled: bool = False
    asset_target_live_enabled: bool = False
    asset_target_live_size_usd: float = Field(default=5.0, ge=1.0)
    asset_target_live_max_open: int = Field(default=2, ge=1, le=5)
    asset_target_max_per_asset: int = Field(default=2, ge=1, le=5)

    # Financial Strategy canary (per AI debate [GPT 9]):
    # category='financial' coverage с legacy internal logic, отдельный bucket.
    financial_strategy_enabled: bool = True
    financial_strategy_max_size_usd: float = Field(default=7.5, ge=1.0)
    financial_strategy_daily_stop: float = Field(default=4.0, ge=1.0)

    @model_validator(mode="after")
    def validate_live_mode(self) -> "Settings":
        if self.app_mode == TradingMode.LIVE_AUTO and not self.enable_live_trading:
            raise ValueError(
                "LIVE_AUTO is blocked. Set APP_MODE=shadow or paper_auto. "
                "ENABLE_LIVE_TRADING remains false by default for safety."
            )
        return self

    @property
    def is_shadow(self) -> bool:
        return self.app_mode == TradingMode.SHADOW

    @property
    def is_paper_auto(self) -> bool:
        return self.app_mode == TradingMode.PAPER_AUTO

    @property
    def is_live_auto(self) -> bool:
        return self.app_mode == TradingMode.LIVE_AUTO


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
