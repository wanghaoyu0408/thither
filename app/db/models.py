"""SQLAlchemy tables.

TripState is stored whole as JSON rather than normalized across tables. The
shape is still moving, the whole document is always read and written together,
and premature normalization would buy nothing (spec section 31).

The JSON type carries a Postgres variant, so moving from the SQLite dev default
to Postgres is a DATABASE_URL change with no code change.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
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
