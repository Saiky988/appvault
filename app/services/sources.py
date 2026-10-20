"""Source management, worker coordination state, leases, change event queries, and stats."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    App,
    AppAlias,
    ChangeEvent,
    DiscordSubscription,
    Source,
    SourceApp,
    WorkerState,
)
from app.schemas.api import (
    ChangeEventSchema,
    PaginatedResponse,
    SourceAppItemSchema,
    SourceSchema,
    StatsResponseSchema,
    WorkerStateResponseSchema,
)
from app.services.catalog import compute_source_health_status, normalize_app_name


def get_or_create_worker_state(session: Session) -> WorkerState:
    """Return singleton WorkerState row (id=1)."""
    state = session.get(WorkerState, 1)
    if state is None:
        state = WorkerState(
            id=1,
            is_paused=False,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(state)
        session.flush()
    return state


def set_worker_paused(session: Session, paused: bool) -> WorkerStateResponseSchema:
    """Set global background monitoring pause flag in the database."""
    state = get_or_create_worker_state(session)
    state.is_paused = bool(paused)
    state.updated_at = datetime.now(timezone.utc)
    session.flush()
    return WorkerStateResponseSchema(
        is_paused=state.is_paused,
        active_worker_id=state.active_worker_id,
        last_heartbeat_at=state.last_heartbeat_at,
        updated_at=state.updated_at,
    )


def record_worker_heartbeat(session: Session, worker_id: str) -> WorkerState:
    """Update worker heartbeat timestamp and active worker ID."""
    state = get_or_create_worker_state(session)
    now = datetime.now(timezone.utc)
    state.active_worker_id = worker_id
    state.last_heartbeat_at = now
    state.updated_at = now
    session.flush()
    return state


def acquire_source_lease(
    session: Session,
    source_id: int,
    owner_id: str,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Attempt to acquire a database-backed lease on a Source to prevent concurrent duplicate checks."""
    settings = get_settings()
    ttl = ttl_seconds or settings.lease_ttl_seconds
    now = datetime.now(timezone.utc)

    source = session.get(Source, source_id)
    if source is None:
        return False

    if (
        source.lease_owner is not None
        and source.lease_owner != owner_id
        and source.lease_expires_at is not None
    ):
        exp = source.lease_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            return False

    source.lease_owner = owner_id
    source.lease_expires_at = now + timedelta(seconds=ttl)
    session.flush()
    return True


def release_source_lease(session: Session, source_id: int, owner_id: Optional[str] = None) -> None:
    """Release check lease on a Source."""
    source = session.get(Source, source_id)
    if source is None:
        return
    if owner_id is None or source.lease_owner == owner_id:
        source.lease_owner = None
        source.lease_expires_at = None
        session.flush()


def get_active_source_links(session: Session, source_id: int) -> List[SourceApp]:
    """Return active SourceApp links ordered by display_order, with fallback if seed links were deactivated."""
    links = session.scalars(
        select(SourceApp)
        .where(
            SourceApp.source_id == source_id,
            SourceApp.is_active == True,  # noqa: E712
        )
        .order_by(SourceApp.display_order.asc(), SourceApp.app_id.asc())
    ).all()
    if not links:
        links = session.scalars(
            select(SourceApp)
            .where(SourceApp.source_id == source_id)
            .order_by(SourceApp.display_order.asc(), SourceApp.app_id.asc())
        ).all()
    return list(links)


def build_source_schema(session: Session, source: Source) -> SourceSchema:
    """Convert a Source ORM instance into a SourceSchema with app counts and credentials."""
    settings = get_settings()
    links = get_active_source_links(session, source.id)
    seed_count = sum(1 for link in links if link.data_origin == "seed")
    observed_count = sum(1 for link in links if link.data_origin == "observed")
    effective_interval = source.check_interval_seconds or settings.monitor_interval_seconds

    return SourceSchema(
        id=source.id,
        source_key=source.source_key,
        url=source.url,
        enabled=source.enabled,
        health_status=compute_source_health_status(source),
        check_interval_seconds=effective_interval,
        has_custom_interval=source.check_interval_seconds is not None,
        app_count=len(links),
        seed_app_count=seed_count,
        observed_app_count=observed_count,
        account_email=source.account_email,
        account_password=source.account_password,
        account_updated_at=source.account_updated_at,
        last_checked_at=source.last_checked_at,
        last_success_at=source.last_success_at,
        last_http_status=source.last_http_status,
        last_error=source.last_error,
        current_content_hash=source.current_content_hash,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def get_source_by_id_or_key(session: Session, identifier: int | str) -> Optional[Source]:
    """Fetch a Source by integer primary key or source_key."""
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        src = session.get(Source, int(identifier))
        if src is not None:
            return src
    return session.scalar(select(Source).where(Source.source_key == str(identifier)))


def list_sources_paginated(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
) -> PaginatedResponse[SourceSchema]:
    """Return paginated list of configured Sources."""
    total = int(session.scalar(select(func.count(Source.id))) or 0)
    sources = session.scalars(
        select(Source).order_by(Source.id.asc()).limit(limit).offset(offset)
    ).all()
    items = [build_source_schema(session, s) for s in sources]
    return PaginatedResponse[SourceSchema](
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def list_source_apps_paginated(
    session: Session,
    source_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
) -> Optional[PaginatedResponse[SourceAppItemSchema]]:
    """Return paginated apps belonging to a specific Source, ordered by display_order."""
    source = get_source_by_id_or_key(session, source_id)
    if source is None:
        return None

    all_links = get_active_source_links(session, source.id)
    total = len(all_links)
    sliced = all_links[offset : offset + limit]

    items: List[SourceAppItemSchema] = []
    for link in sliced:
        app = session.get(App, link.app_id)
        if app is None:
            continue
        items.append(
            SourceAppItemSchema(
                app_id=app.id,
                canonical_name=app.canonical_name,
                normalized_name=app.normalized_name,
                original_title=link.original_title,
                display_order=link.display_order,
                data_origin=link.data_origin,
                is_active=link.is_active,
                first_seen_at=link.first_seen_at,
                last_seen_at=link.last_seen_at,
            )
        )

    return PaginatedResponse[SourceAppItemSchema](
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def list_updates_paginated(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    source_id: Optional[int] = None,
    source_ids: Optional[List[int]] = None,
) -> PaginatedResponse[ChangeEventSchema]:
    """Return paginated list of detected ChangeEvents ordered newest-first."""
    stmt = select(ChangeEvent)
    if source_id is not None:
        stmt = stmt.where(ChangeEvent.source_id == source_id)
    elif source_ids is not None:
        if not source_ids:
            return PaginatedResponse[ChangeEventSchema](total=0, limit=limit, offset=offset, items=[])
        stmt = stmt.where(ChangeEvent.source_id.in_(source_ids))

    all_matching = session.scalars(
        stmt.order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
    ).all()
    total = len(all_matching)
    page_events = all_matching[offset : offset + limit]

    items: List[ChangeEventSchema] = []
    for ev in page_events:
        src = session.get(Source, ev.source_id)
        items.append(
            ChangeEventSchema(
                id=ev.id,
                source_id=ev.source_id,
                source_key=src.source_key if src else None,
                source_url=src.url if src else None,
                event_type=ev.event_type,
                previous_hash=ev.previous_hash,
                current_hash=ev.current_hash,
                summary=ev.summary,
                account_email=ev.account_email or (src.account_email if src else None),
                account_password=ev.account_password or (src.account_password if src else None),
                detected_at=ev.detected_at,
                notification_status=ev.notification_status,
                notification_attempts=ev.notification_attempts,
            )
        )

    return PaginatedResponse[ChangeEventSchema](
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def get_update_by_id(session: Session, event_id: int) -> Optional[ChangeEventSchema]:
    """Fetch a single ChangeEvent by ID."""
    ev = session.get(ChangeEvent, event_id)
    if ev is None:
        return None
    src = session.get(Source, ev.source_id)
    return ChangeEventSchema(
        id=ev.id,
        source_id=ev.source_id,
        source_key=src.source_key if src else None,
        source_url=src.url if src else None,
        event_type=ev.event_type,
        previous_hash=ev.previous_hash,
        current_hash=ev.current_hash,
        summary=ev.summary,
        account_email=ev.account_email or (src.account_email if src else None),
        account_password=ev.account_password or (src.account_password if src else None),
        detected_at=ev.detected_at,
        notification_status=ev.notification_status,
        notification_attempts=ev.notification_attempts,
    )


def compute_system_stats(session: Session) -> StatsResponseSchema:
    """Aggregate catalog, source, change event, and worker metrics."""
    total_apps = int(session.scalar(select(func.count(App.id))) or 0)
    sources = session.scalars(select(Source)).all()
    total_sources = len(sources)
    enabled_sources = sum(1 for s in sources if s.enabled)
    healthy_sources = sum(1 for s in sources if compute_source_health_status(s) == "healthy")
    seed_only_sources = sum(1 for s in sources if compute_source_health_status(s) == "seed_only")
    error_sources = sum(1 for s in sources if compute_source_health_status(s) == "error")

    last_success_times = [s.last_success_at for s in sources if s.last_success_at is not None]
    last_successful_check_at = max(last_success_times) if last_success_times else None

    total_links = len(
        session.scalars(select(SourceApp).where(SourceApp.is_active == True)).all()  # noqa: E712
    )
    total_aliases = int(session.scalar(select(func.count(AppAlias.id))) or 0)
    all_events = session.scalars(select(ChangeEvent)).all()
    total_events = len(all_events)
    pending_notifications = sum(
        1 for e in all_events if e.notification_status in ("pending", "processing")
    )
    active_subs = len(
        session.scalars(
            select(DiscordSubscription).where(DiscordSubscription.enabled == True)  # noqa: E712
        ).all()
    )
    worker_state = get_or_create_worker_state(session)

    return StatsResponseSchema(
        total_apps=total_apps,
        total_sources=total_sources,
        enabled_sources=enabled_sources,
        healthy_sources=healthy_sources,
        seed_only_sources=seed_only_sources,
        error_sources=error_sources,
        total_source_app_links=total_links,
        total_aliases=total_aliases,
        total_change_events=total_events,
        pending_notifications=pending_notifications,
        active_subscriptions=active_subs,
        worker_paused=worker_state.is_paused,
        last_successful_check_at=last_successful_check_at,
    )


def upsert_discord_subscription(
    session: Session,
    *,
    guild_id: str,
    channel_id: str,
    event_types: str = "initial_observation,credentials_updated,apps_updated,title_changed,content_updated",
    game_filter: Optional[str] = None,
) -> Tuple[DiscordSubscription, bool]:
    """Create or update a Discord channel subscription (for all games or a specific game)."""
    existing = session.scalar(
        select(DiscordSubscription).where(DiscordSubscription.channel_id == str(channel_id))
    )
    norm_game = normalize_app_name(game_filter) if game_filter else None

    if existing is None:
        sub = DiscordSubscription(
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            enabled=True,
            subscribed_event_types=event_types,
            game_filter=norm_game,
            created_at=datetime.now(timezone.utc),
        )
        session.add(sub)
        session.flush()
        return sub, True

    existing.guild_id = str(guild_id)
    existing.enabled = True
    existing.subscribed_event_types = event_types
    if norm_game is None:
        existing.game_filter = None
    else:
        current_filters = [
            f.strip()
            for f in (existing.game_filter or "").split(",")
            if f.strip()
        ]
        if norm_game not in current_filters:
            current_filters.append(norm_game)
        existing.game_filter = ",".join(current_filters)

    session.flush()
    return existing, False


def disable_discord_subscription(session: Session, channel_id: str) -> bool:
    """Disable an existing Discord channel subscription."""
    existing = session.scalar(
        select(DiscordSubscription).where(DiscordSubscription.channel_id == str(channel_id))
    )
    if existing is None or not existing.enabled:
        return False
    existing.enabled = False
    session.flush()
    return True
