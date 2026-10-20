"""Initial AppVault schema.

Revision ID: 20261020_0001
Revises: None
Create Date: 2026-10-20 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20261020_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "apps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_apps_normalized_name", "apps", ["normalized_name"], unique=True)
    op.create_index("ix_apps_canonical_name", "apps", ["canonical_name"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("current_content_hash", sa.String(length=64), nullable=True),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("last_modified", sa.String(length=255), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sources_source_key", "sources", ["source_key"], unique=True)
    op.create_index("ix_sources_enabled", "sources", ["enabled"], unique=False)

    op.create_table(
        "source_apps",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("original_title", sa.String(length=255), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("data_origin", sa.String(length=32), nullable=False, server_default="seed"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_apps_source_id", "source_apps", ["source_id"], unique=False)
    op.create_index("ix_source_apps_app_id", "source_apps", ["app_id"], unique=False)

    op.create_table(
        "change_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("current_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notification_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("notification_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_notification_error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source_id", "current_hash", name="uq_change_events_source_current_hash"),
    )
    op.create_index("ix_change_events_source_id", "change_events", ["source_id"], unique=False)
    op.create_index("ix_change_events_detected_at", "change_events", ["detected_at"], unique=False)
    op.create_index("ix_change_events_notification_status", "change_events", ["notification_status"], unique=False)

    op.create_table(
        "discord_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("subscribed_event_types", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_id", name="uq_discord_subscriptions_channel_id"),
    )
    op.create_index("ix_discord_subscriptions_guild_id", "discord_subscriptions", ["guild_id"], unique=False)
    op.create_index("ix_discord_subscriptions_enabled", "discord_subscriptions", ["enabled"], unique=False)

    op.create_table(
        "app_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("app_id", sa.Integer(), sa.ForeignKey("apps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("normalized_alias", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("app_id", "normalized_alias", name="uq_app_aliases_app_norm"),
    )
    op.create_index("ix_app_aliases_normalized_alias", "app_aliases", ["normalized_alias"], unique=False)
    op.create_index("ix_app_aliases_app_id", "app_aliases", ["app_id"], unique=False)

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("change_event_id", sa.Integer(), sa.ForeignKey("change_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("change_event_id", "channel_id", name="uq_notification_delivery_event_channel"),
    )
    op.create_index("ix_notification_deliveries_event_id", "notification_deliveries", ["change_event_id"], unique=False)

    op.create_table(
        "worker_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("is_paused", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("active_worker_id", sa.String(length=128), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_command_sync_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_state")
    op.drop_table("notification_deliveries")
    op.drop_table("app_aliases")
    op.drop_table("discord_subscriptions")
    op.drop_table("change_events")
    op.drop_table("source_apps")
    op.drop_table("sources")
    op.drop_table("apps")
