"""SQLAlchemy tables.

TripState is stored whole as JSON rather than normalized across tables. The
shape is still moving, the whole document is always read and written together,
and premature normalization would buy nothing (spec section 31).

The JSON type carries a Postgres variant, so moving from the SQLite dev default
to Postgres is a DATABASE_URL change with no code change.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.models.common import utcnow

JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")

TimestampType = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class TravelerProfileRow(Base):
    __tablename__ = "traveler_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile: Mapped[dict[str, Any]] = mapped_column("profile_jsonb", JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TimestampType, default=utcnow, onupdate=utcnow, nullable=False
    )


class TripRow(Base):
    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Mirrors state_jsonb["revision"], kept as a column so the conditional
    # UPDATE that implements optimistic concurrency can key off it.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    state: Mapped[dict[str, Any]] = mapped_column("state_jsonb", JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TimestampType, default=utcnow, onupdate=utcnow, nullable=False
    )


class TripMessageRow(Base):
    """Conversation transcript.

    Kept for audit and continuity only. TripState remains the source of truth -
    replaying this table must never be necessary to know what the trip is
    (spec section 2).
    """

    __tablename__ = "trip_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    trip_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Revision the turn started from, so a reply can be traced to a trip version.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_log: Mapped[Any | None] = mapped_column("run_log_jsonb", JSONType, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_trip_messages_trip_id_id", "trip_id", "id"),)


class ToolCacheRow(Base):
    """Durable half of the tool cache.

    Only holds content that may legally persist: place ids (no expiry) and
    lat/lng (<=30 days). `expires_at` is a delete obligation, enforced by
    `SqliteCache.purge_expired()`, not just a read-time filter.
    """

    __tablename__ = "tool_cache"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_class: Mapped[str] = mapped_column(String(32), nullable=False)

    payload: Mapped[Any] = mapped_column("payload_jsonb", JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TimestampType, nullable=True)

    __table_args__ = (Index("ix_tool_cache_expires_at", "expires_at"),)


class LearningSignalRow(Base):
    """Append-only, like trip_events. One row per observed fact about a traveller.

    No FK to trips: learning must outlive the trip that produced it - deleting
    last year's trip must not delete what it taught us - so the payload's
    `context` carries whatever "Why?" needs to stay renderable. No FK to
    traveler_profiles either; profiles have no delete path today.
    """

    __tablename__ = "learning_signals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trip_id: Mapped[str] = mapped_column(String(64), nullable=False)

    payload: Mapped[dict[str, Any]] = mapped_column("payload_jsonb", JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_learning_signals_profile_id", "profile_id"),)


class OutcomeRow(Base):
    """Append-only record of times this system's numbers met reality.

    No trip id and no coordinates - not an oversight, the point. Predictions
    are derived from trips and die with them; this is the corpus that has to
    survive, and a durable table keyed to where somebody went is not a thing
    to create as a side effect of measuring a routing API. `scope` is
    region-coarse and is the finest location that reaches this row.

    Indexed on the calibration key rather than on any subject, because the
    only question ever asked of it is "how wrong is this provider, at this,
    here?".
    """

    __tablename__ = "outcomes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    dimension: Mapped[str] = mapped_column(String(48), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")

    payload: Mapped[dict[str, Any]] = mapped_column("payload_jsonb", JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_outcomes_key", "provider", "dimension", "mode", "scope"),
    )


class TripEventRow(Base):
    """Append-only audit trail. Never updated, never deleted."""

    __tablename__ = "trip_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    trip_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column("payload_jsonb", JSONType, default=dict)

    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_trip_events_trip_id_id", "trip_id", "id"),)
