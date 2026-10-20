"""Normalized SQLAlchemy 2.x database models for AppVault."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base class for all AppVault models."""


class App(Base):
    """Deduplicated canonical application or game record."""

    __tablename__ = "apps"
    __table_args__ = (
        Index("ix_apps_normalized_name", "normalized_name", unique=True),
        Index("ix_apps_canonical_name", "canonical_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    platform: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="iOS")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    source_links: Mapped[List["SourceApp"]] = relationship(
        "SourceApp", back_populates="app", cascade="all, delete-orphan"
    )
    aliases: Mapped[List["AppAlias"]] = relationship(
        "AppAlias", back_populates="app", cascade="all, delete-orphan"
    )


class Source(Base):
    """Monitored public listing source page and its extracted account credentials."""

    __tablename__ = "sources"
    __table_args__ = (
        Index("ix_sources_source_key", "source_key", unique=True),
        Index("ix_sources_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)

    # Extracted account credentials from <div class="copy-buttons">
    account_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    account_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    account_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    current_content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)

    # Conditional HTTP headers
    etag: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, default=None)
    last_modified: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)

    # DB-backed claim/lease coordination to prevent concurrent duplicate checks
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    app_links: Mapped[List["SourceApp"]] = relationship(
        "SourceApp", back_populates="source", cascade="all, delete-orphan"
    )
    change_events: Mapped[List["ChangeEvent"]] = relationship(
        "ChangeEvent", back_populates="source", cascade="all, delete-orphan"
    )


class SourceApp(Base):
    """Association between a Source and an App, preserving original title, order, and data origin."""

    __tablename__ = "source_apps"
    __table_args__ = (
        Index("ix_source_apps_source_id", "source_id"),
        Index("ix_source_apps_app_id", "app_id"),
    )

    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    app_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True
    )
    original_title: Mapped[str] = mapped_column(String(255), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_origin: Mapped[str] = mapped_column(String(32), nullable=False, default="seed")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    source: Mapped["Source"] = relationship("Source", back_populates="app_links")
    app: Mapped["App"] = relationship("App", back_populates="source_links")


class ChangeEvent(Base):
    """Recorded meaningful change detected on a monitored source."""

    __tablename__ = "change_events"
    __table_args__ = (
        UniqueConstraint("source_id", "current_hash", name="uq_change_events_source_current_hash"),
        Index("ix_change_events_source_id", "source_id"),
        Index("ix_change_events_detected_at", "detected_at"),
        Index("ix_change_events_notification_status", "notification_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    current_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    account_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    account_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    notification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    notification_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_notification_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, default=None)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)

    source: Mapped["Source"] = relationship("Source", back_populates="change_events")


class DiscordSubscription(Base):
    """Guild channel subscription for automated change notifications (all games or filtered by game)."""

    __tablename__ = "discord_subscriptions"
    __table_args__ = (
        UniqueConstraint("channel_id", name="uq_discord_subscriptions_channel_id"),
        Index("ix_discord_subscriptions_guild_id", "guild_id"),
        Index("ix_discord_subscriptions_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    subscribed_event_types: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="initial_observation,credentials_updated,apps_updated,title_changed,content_updated",
    )
    game_filter: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AppAlias(Base):
    """Alternative title, spelling correction, or common abbreviation for an App."""

    __tablename__ = "app_aliases"
    __table_args__ = (
        UniqueConstraint("app_id", "normalized_alias", name="uq_app_aliases_app_norm"),
        Index("ix_app_aliases_normalized_alias", "normalized_alias"),
        Index("ix_app_aliases_app_id", "app_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)

    app: Mapped["App"] = relationship("App", back_populates="aliases")


class NotificationDelivery(Base):
    """Tracks per-channel delivery for each ChangeEvent to prevent duplicate Discord messages on retry."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("change_event_id", "channel_id", name="uq_notification_delivery_event_channel"),
        Index("ix_notification_deliveries_event_id", "change_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("change_events.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class WorkerState(Base):
    """Shared coordination row for worker pause state, heartbeat, and command sync metadata."""

    __tablename__ = "worker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active_worker_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, default=None)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    last_command_sync_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
