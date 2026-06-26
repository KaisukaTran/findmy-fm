"""
OPUS mode tables (additive — never co-mingle with rule-based accounting).

All three are created by `Base.metadata.create_all` (init_db imports this module).
Orders/fills Opus produces are tagged source="opus" elsewhere; these tables track the
discretionary-position lifecycle, the metered (x2) Opus cost, and hourly KPI rollups.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.clock import utcnow
from app.db import Base

# OpusPosition.state lifecycle (see docs §5 — the 3h rule).
OPUS_WATCH = "watch"      # 0–3h after a discretionary buy
OPUS_RIDE = "ride"        # winner at the 3h mark — Opus keeps discretion (may bypass KSS)
OPUS_RESCUE = "rescue"    # loser at the 3h mark — handed off to a standard KSS session
OPUS_CLOSED = "closed"


class OpusPosition(Base):
    """One discretionary trade Opus opened, plus its watch/ride/rescue state."""

    __tablename__ = "opus_positions"
    __table_args__ = (
        Index("ix_opus_positions_state", "state"),
        Index("ix_opus_positions_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    state: Mapped[str] = mapped_column(String(12), nullable=False, default=OPUS_WATCH)
    watch_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set to the 3h-mark verdict time once the watch window is evaluated.
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # If handed off to KSS (rescue), the adopting session id.
    kss_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OpusCostLedger(Base):
    """One row per Opus API call. billed_cost = 2 x raw_cost (requirement #5)."""

    __tablename__ = "opus_cost_ledger"
    __table_args__ = (Index("ix_opus_cost_ledger_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    billed_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 2x raw
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="decision")
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OpusLesson(Base):
    """Distilled lesson from OPUS's own/the engine's trade history (Phase O-LEARN, L2).

    A periodic distiller summarizes recent wins/losses into short lessons; the top
    `opus_lessons_max` are injected into the static system prompt so learning compounds
    across calls instead of being amnesiac. Table created now (O-FIX scaffolding); the
    writer/reader is built in a later phase.
    """

    __tablename__ = "opus_lessons"
    __table_args__ = (Index("ix_opus_lessons_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    lesson_text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class OpusMetricHourly(Base):
    """Hourly KPI rollup (drives the OPUS chart + the 1%/24h gauge)."""

    __tablename__ = "opus_metrics_hourly"
    __table_args__ = (Index("ix_opus_metrics_hour", "hour_ts", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # truncated to the hour
    gross_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    opus_cost_billed: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    invested_capital: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    net_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
