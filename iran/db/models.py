"""SQLAlchemy 2 ORM models for the Iran VPS service (Track B, Step 3).

Tables
------
- users              — registered web-UI users
- jobs               — download job records (mirrors JobStatus lifecycle)
- refresh_tokens     — JWT refresh token store
- audit_log          — append-only admin/action audit trail
- settings           — key/value runtime settings
- registrations      — pending user-approval inbox

All primary keys use UUID; JSONB columns fall back to JSON on SQLite for
portability in tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base shared by all Iran DB models."""


# ---------------------------------------------------------------------------
# Helper: UUID primary-key column
# ---------------------------------------------------------------------------


def _uuid_pk() -> Mapped[str]:
    """Return a UUID PK column (stored as TEXT for cross-dialect compatibility)."""
    return mapped_column(Text, primary_key=True, default=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


class User(Base):
    """A registered web-UI user.

    Roles: ``'user'`` | ``'admin'``
    Statuses: ``'pending_approval'`` | ``'active'`` | ``'blocked'`` | ``'deleted'``
    """

    __tablename__ = "users"

    id: Mapped[str] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending_approval"
    )
    rubika_guid: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    jobs: Mapped[list[Job]] = relationship("Job", back_populates="user")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken", back_populates="user"
    )
    audit_entries: Mapped[list[AuditLog]] = relationship(
        "AuditLog", foreign_keys="AuditLog.actor_id", back_populates="actor"
    )
    registration: Mapped[Registration | None] = relationship(
        "Registration", foreign_keys="Registration.user_id", back_populates="user"
    )


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


class Job(Base):
    """A download job record.

    ``status`` mirrors the ``JobStatus`` enum values from ``kharej.contracts``:
    ``pending`` | ``accepted`` | ``running`` | ``completed`` | ``failed`` |
    ``cancelled``

    ``s2_keys`` stores a JSON array of ``S2ObjectRef``-shaped dicts (as
    returned by ``JobCompleted.parts``).

    ``metadata_json`` stores the arbitrary ``JobCompleted.metadata`` dict.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    quality: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False, default="single")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    speed: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # list[S2ObjectRef-as-dict]; stored as JSON
    s2_keys: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    total_tracks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    done_tracks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_tracks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_track: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JobCompleted.metadata dict; stored as JSON
    metadata_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="jobs")


# ---------------------------------------------------------------------------
# refresh_tokens
# ---------------------------------------------------------------------------


class RefreshToken(Base):
    """JWT refresh token record.

    ``token`` stores the *SHA-256 hex digest* of the raw random bytes — the
    raw token is never persisted.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")


# ---------------------------------------------------------------------------
# audit_log
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """Append-only audit trail of admin and user actions.

    ``actor_id`` is nullable to allow system-generated entries (e.g. automated
    timeouts).
    ``payload`` is a free-form JSON snapshot of the message or params.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = _uuid_pk()
    actor_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    ip_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationships
    actor: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[actor_id],
        back_populates="audit_entries",
    )


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class Setting(Base):
    """Key/value runtime settings store."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


# ---------------------------------------------------------------------------
# registrations
# ---------------------------------------------------------------------------


class Registration(Base):
    """Pending user-approval inbox entry.

    Created when a new user registers; an admin reviews and updates
    ``reviewed_by`` / ``reviewed_at`` to approve or reject.
    """

    __tablename__ = "registrations"

    id: Mapped[str] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id"), unique=True, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(
        Text, ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped[User] = relationship(
        "User", foreign_keys=[user_id], back_populates="registration"
    )
    reviewer: Mapped[User | None] = relationship(
        "User", foreign_keys=[reviewed_by]
    )
