from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketSnapshot(TimestampMixin, Base):
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str] = mapped_column(String(255), index=True)
    question: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="unknown")
    outcome: Mapped[str] = mapped_column(String(32), default="YES")
    best_bid: Mapped[float] = mapped_column(Float, default=0.0)
    best_ask: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    fair_price: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PaperOrder(TimestampMixin, Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="YES")
    side: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="filled")
    mode: Mapped[str] = mapped_column(String(32), default="paper_auto")
    strategy: Mapped[str] = mapped_column(String(64), default="unknown")
    note: Mapped[str] = mapped_column(Text, default="")


class Position(TimestampMixin, Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="YES")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    # bucket: "core_sniper" | "experiment" — для quarantined stats per AI debate spec.
    # Default "experiment" чтобы старые записи не попадали в core stats.
    bucket: Mapped[str] = mapped_column(String(32), default="experiment", server_default="experiment")
    # cluster_key: для correlation guard. Same teams + same date → same cluster.
    # Risk manager не позволяет 2 open в one cluster.
    cluster_key: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)


class OpportunityLog(TimestampMixin, Base):
    """Каждый scan, для каждого signal-кандидата — запись здесь.

    Цель: shadow-replay backtest, paper_optimism_gap measurement,
    mapping error tracking, latency analysis. Per AI debate spec.
    """
    __tablename__ = "opportunity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Market identity
    market_id: Mapped[str] = mapped_column(String(128), index=True)
    slug: Mapped[str] = mapped_column(String(255), default="")
    cluster_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # Mapping result
    league: Mapped[str | None] = mapped_column(String(32), nullable=True)
    team_a_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    team_b_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_type: Mapped[str] = mapped_column(String(32), default="unknown")
    time_scope: Mapped[str] = mapped_column(String(32), default="unknown")
    mapping_confidence: Mapped[str] = mapped_column(String(16), default="none")
    window_bucket: Mapped[str] = mapped_column(String(16), default="closed")

    # Polymarket snapshot at signal time
    poly_ask: Mapped[float] = mapped_column(Float, default=0.0)
    poly_bid: Mapped[float] = mapped_column(Float, default=0.0)
    poly_ask_size: Mapped[float] = mapped_column(Float, default=0.0)  # depth!
    poly_bid_size: Mapped[float] = mapped_column(Float, default=0.0)  # depth!
    poly_spread: Mapped[float] = mapped_column(Float, default=0.0)
    poly_volume: Mapped[float] = mapped_column(Float, default=0.0)
    poly_liquidity: Mapped[float] = mapped_column(Float, default=0.0)

    # External (bookmaker) source
    external_fair: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_books: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    book_dispersion: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Edge calculation
    raw_edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    haircut: Mapped[float | None] = mapped_column(Float, nullable=True)
    tradable_edge: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Internal (FairPriceEngine) snapshot — used as VETO only
    internal_fair: Mapped[float | None] = mapped_column(Float, nullable=True)
    internal_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    internal_momentum: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Decision
    decision: Mapped[str] = mapped_column(String(48), default="REJECTED")
    # ENTERED_LIVE, ENTERED_SHADOW, REJECTED_LOW_EDGE, REJECTED_INTERNAL_VETO,
    # REJECTED_MAPPING_FUZZY, REJECTED_TIME_SCOPE_UNKNOWN, REJECTED_WINDOW,
    # REJECTED_SPREAD, REJECTED_DEPTH, REJECTED_DAILY_STOP, REJECTED_CLUSTER_DUPE
    reject_reason: Mapped[str] = mapped_column(Text, default="")

    # Sizing (theoretical for shadow, actual for live)
    intended_size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    actual_size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    bucket: Mapped[str] = mapped_column(String(32), default="shadow")
    # shadow | core_sniper_live | experiment

    # Latency (used for live in-game readiness analysis)
    odds_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bookmaker_last_update: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poly_book_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    minutes_to_event_start: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Forward returns — заполняются background job через 5/15/60/180 мин/resolution
    fwd_ret_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_ret_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_ret_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_ret_180m: Mapped[float | None] = mapped_column(Float, nullable=True)
    fwd_ret_resolution: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Executable simulation (per GPT spec: paper_optimism_gap)
    sim_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # next-scan ask + slippage
    sim_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # next-scan bid - slippage
    sim_executable_return: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Resolution outcome (filled when market resolves)
    resolution_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)


class NewsAlert(TimestampMixin, Base):
    """Inbound news event from n8n RSS/Telegram pipeline.

    Per AI debate Round 3: news = labeled dataset (alert-only first 2 weeks),
    автотрейд только после 100+ alerts с измеренным edge.
    """
    __tablename__ = "news_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    # "reuters_breaking", "tg_durov_news", "ap_politics", etc.
    headline: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    # AI consensus (filled after async classification call)
    ai_relevant: Mapped[bool | None] = mapped_column(default=None, nullable=True)
    ai_market_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_expected_move_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Affected markets (filled offline by matcher job)
    matched_market_ids: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Classification status
    status: Mapped[str] = mapped_column(String(32), default="ingested")
    # ingested → classified → matched → measured → ignored


class BotState(TimestampMixin, Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class PMFill(TimestampMixin, Base):
    """Polymarket fills logged from data-api для Smart Money MVP per [GPT 18].

    Production fills logger pulls trades каждые 15-30 мин и appends.
    Forward returns calculated lazy via prices-history endpoint при analysis.

    Dedup: tx_hash + asset + side + wallet — uniquely identifies single fill leg.
    """
    __tablename__ = "pm_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fill_ts: Mapped[int] = mapped_column(Integer, index=True)  # unix seconds
    wallet: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))  # BUY/SELL
    condition_id: Mapped[str] = mapped_column(String(80), index=True)
    asset: Mapped[str] = mapped_column(String(80), index=True)  # token_id
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(255), default="")
    slug: Mapped[str] = mapped_column(String(255), default="")
    outcome: Mapped[str] = mapped_column(String(32), default="")
    tx_hash: Mapped[str] = mapped_column(String(80), index=True)
