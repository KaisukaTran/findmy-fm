"""
ORM models for FINDMY-FM (lean rebuild) — all tables in one SQLite database.

Tables
------
- pending_orders : approval queue (every order starts here; nothing bypasses it)
- fills          : append-only execution facts (paper trades) used as trade history
- positions      : current per-symbol position (derived/updated state)
- kss_sessions   : KSS Pyramid DCA sessions
- kss_waves      : individual waves within a session

Status values are plain strings to stay migration-friendly and to match
`app.kss.pyramid.PyramidSessionStatus.value` directly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# --- status constants ---------------------------------------------------

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"

# session statuses mirror PyramidSessionStatus.value
SESSION_PENDING = "pending"
SESSION_ACTIVE = "active"
SESSION_STOPPED = "stopped"
SESSION_COMPLETED = "completed"
SESSION_TP_TRIGGERED = "tp_triggered"

# wave statuses
WAVE_PENDING = "pending"
WAVE_SENT = "sent"
WAVE_FILLED = "filled"
WAVE_CANCELLED = "cancelled"


class PendingOrder(Base):
    """An order awaiting manual approval. Approve → execute; reject → discard."""

    __tablename__ = "pending_orders"
    __table_args__ = (
        Index("ix_pending_orders_status", "status"),
        Index("ix_pending_orders_symbol", "symbol"),
        Index("ix_pending_orders_source_ref", "source_ref"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(12), nullable=False, default="LIMIT")
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 0 = market

    # provenance / attribution
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # approval workflow
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=PENDING)
    reviewer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI Guardian (Phase B): a veto blocks AUTO approval only — the order stays
    # pending so a human can still approve it. Reason is the model's rationale.
    # auto_veto_at stamps when the veto was set so the scheduler can expire stale
    # vetoes (TTL) and re-review them — a transient veto must never deadlock a KSS
    # DCA wave whose limit price has since been reached.
    auto_veto: Mapped[bool] = mapped_column(default=False, nullable=False)
    auto_veto_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_veto_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "price": self.price,
            "source": self.source,
            "source_ref": self.source_ref,
            "strategy_name": self.strategy_name,
            "note": self.note,
            "risk_note": self.risk_note,
            "status": self.status,
            "reviewer": self.reviewer,
            "reject_reason": self.reject_reason,
            "auto_veto": self.auto_veto,
            "auto_veto_reason": self.auto_veto_reason,
            "auto_veto_at": self.auto_veto_at.isoformat() if self.auto_veto_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


class Fill(Base):
    """An executed paper fill. Append-only — never edited after insert."""

    __tablename__ = "fills"
    __table_args__ = (
        Index("ix_fills_symbol", "symbol"),
        Index("ix_fills_executed_at", "executed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pending_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("pending_orders.id"), nullable=True
    )

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)  # effective fill price
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    source_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pending_order_id": self.pending_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "fee": self.fee,
            "slippage": self.slippage,
            "realized_pnl": self.realized_pnl,
            "source_ref": self.source_ref,
            "strategy_name": self.strategy_name,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }


class Position(Base):
    """Current open position per symbol (derived state, updated on each fill)."""

    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_symbol", "symbol", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "avg_entry_price": self.avg_entry_price,
            "total_cost": self.total_cost,
            "realized_pnl": self.realized_pnl,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KssSession(Base):
    """KSS Pyramid DCA session (parameters + running state)."""

    __tablename__ = "kss_sessions"
    __table_args__ = (
        Index("ix_kss_sessions_symbol", "symbol"),
        Index("ix_kss_sessions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    distance_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_waves: Mapped[int] = mapped_column(Integer, nullable=False)
    isolated_fund: Mapped[float] = mapped_column(Float, nullable=False)
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False)
    timeout_x_min: Mapped[float] = mapped_column(Float, nullable=False)
    gap_y_min: Mapped[float] = mapped_column(Float, nullable=False)

    # Risk exits (Phase A). 0.0 = fall back to the settings default (sl_pct /
    # trailing_pct); a positive per-session value overrides it. peak_price is the
    # high-water mark used by the trailing stop.
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trailing_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    peak_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=SESSION_PENDING)
    current_wave: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_filled_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Max days the session may wait for take-profit before being force-closed.
    deadline_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_fill_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    waves: Mapped[list[KssWave]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="KssWave.wave_num"
    )


class KssWave(Base):
    """A single wave within a KSS session."""

    __tablename__ = "kss_waves"
    __table_args__ = (
        Index("ix_kss_waves_session_id", "session_id"),
        Index("ix_kss_waves_pending_order_id", "pending_order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("kss_sessions.id"), nullable=False)

    wave_num: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=WAVE_PENDING)
    filled_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pending_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    session: Mapped[KssSession] = relationship(back_populates="waves")


# --- scanner / multi-agent audit ----------------------------------------


class ScanRun(Base):
    """One run of the multi-agent scanner."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(12), nullable=False, default="semi")  # semi / auto
    universe_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    params: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON snapshot of thresholds
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Candidate(Base):
    """Per-symbol result of a scan: consensus, win-rate, and the trade/skip decision."""

    __tablename__ = "candidates"
    __table_args__ = (Index("ix_candidates_scan_id", "scan_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    consensus_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Wilson 95% lower bound of the win-rate (trustworthy small-sample number) and the mean
    # net expected PnL %/trial — the realistic metrics the gate actually trades on.
    win_rate_lb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expectancy: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_days_to_tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String(8), nullable=False, default="skip")  # trade / skip
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "scan_id": self.scan_id, "symbol": self.symbol,
            "consensus_pct": self.consensus_pct, "win_rate": self.win_rate,
            "win_rate_lb": self.win_rate_lb, "expectancy": self.expectancy,
            "trials": self.trials,
            "est_days_to_tp": self.est_days_to_tp, "decision": self.decision,
            "reason": self.reason, "session_id": self.session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AgentVoteRecord(Base):
    """One agent's vote on one symbol in one scan (full audit of agent reasoning)."""

    __tablename__ = "agent_votes"
    __table_args__ = (Index("ix_agent_votes_scan_id", "scan_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scan_runs.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "scan_id": self.scan_id, "symbol": self.symbol,
            "agent_name": self.agent_name, "score": self.score,
            "confidence": self.confidence, "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AuditLog(Base):
    """Append-only log of every AI/automation action (decisions, sessions, approvals)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(48), nullable=False)  # e.g. agent:dip, scanner, auto-trader
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "actor": self.actor, "action": self.action,
            "entity": self.entity, "detail": self.detail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RuntimeConfig(Base):
    """
    Persisted key/value runtime state (survives restarts).

    Holds the full-auto master switch and the circuit-breaker freeze state so
    automation flags are no longer lost when the process restarts (unlike the
    in-memory `settings` singleton). Values are stored as strings; callers in
    `app.runtime` cast as needed.
    """

    __tablename__ = "runtime_config"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PairParams(Base):
    """Best per-symbol KSS parameters found by the Phase C hyperopt search.

    The scanner reads these (falling back to the global scan_* config) when it
    opens a session, so each market trades its own tuned distance/tp/waves.
    """

    __tablename__ = "pair_params"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    distance_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_waves: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # objective value
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "distance_pct": self.distance_pct, "tp_pct": self.tp_pct,
            "max_waves": self.max_waves, "score": self.score, "trials": self.trials,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class MlModel(Base):
    """A trained win-rate model (Phase C). Serialized as JSON in `params_json`
    (pure-Python logistic model — no numpy/sklearn dependency)."""

    __tablename__ = "ml_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # weights/bias/feature spec
    metric: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # val accuracy/AUC
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trained_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "version": self.version, "metric": self.metric,
            "n_samples": self.n_samples,
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
        }
