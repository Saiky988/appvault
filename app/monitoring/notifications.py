"""Persistent Discord notification dispatcher with DB-backed lease, backoff retries, and game-filtered subscriptions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence, Set

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import redact_sensitive_text
from app.db.models import App, ChangeEvent, DiscordSubscription, NotificationDelivery, Source, SourceApp

logger = logging.getLogger("appvault.notifications")

DiscordSenderFunc = Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]

def _get_source_apps_and_norms(session: Session, source_id: int) -> tuple[List[str], Set[str]]:
    links = session.scalars(
        select(SourceApp)
        .where(SourceApp.source_id == source_id, SourceApp.is_active == True)
        .order_by(SourceApp.display_order.asc())
    ).all()
    titles: List[str] = []
    norms: Set[str] = set()
    for lk in links:
        titles.append(lk.original_title)
        app_obj = session.get(App, lk.app_id)
        if app_obj:
            norms.add(app_obj.normalized_name)
    return titles, norms

def build_change_notification_embed_dict(
    event: ChangeEvent,
    source: Optional[Source],
    game_titles: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    email_val = event.account_email or (source.account_email if source else None) or "Đang cập nhật"
    pass_val = event.account_password or (source.account_password if source else None) or "Đang cập nhật"

    fields: List[Dict[str, Any]] = [
        {"name": "Tài khoản", "value": f"`{email_val}`", "inline": False},
        {"name": "Mật khẩu", "value": f"`{pass_val}`", "inline": False},
    ]

    if game_titles:
        preview = ", ".join(game_titles[:15])
        if len(game_titles) > 15:
            preview += f" (+{len(game_titles) - 15} game)"
        fields.append(
            {
                "name": f"Danh sách Game ({len(game_titles)})",
                "value": preview[:1024],
                "inline": False,
            }
        )

    return {
        "title": f"Cập nhật Tài khoản — ID #{event.source_id}",
        "color": 0x2ECC71,
        "timestamp": event.detected_at.astimezone(timezone.utc).isoformat(),
        "fields": fields,
        "footer": {"text": f"AppVault • ID #{event.source_id}"},
    }

async def default_discord_rest_sender(
    channel_id: str,
    embed_dict: Dict[str, Any],
    *,
    settings: Optional[Settings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> None:
    cfg = settings or get_settings()
    token = (cfg.discord_bot_token or "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured for notification dispatch")

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": cfg.user_agent,
    }
    payload = {
        "embeds": [embed_dict],
        "allowed_mentions": {"parse": []},
    }
    async with httpx.AsyncClient(timeout=cfg.http_timeout_seconds, transport=transport) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 429:
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                pass
            raise RuntimeError(f"Discord rate limited (429); retry after {retry_after}s")
        if resp.status_code >= 400:
            raise RuntimeError(f"Discord API error HTTP {resp.status_code}")

def acquire_event_notification_lease(
    session: Session,
    event: ChangeEvent,
    owner_id: str,
    ttl_seconds: int = 60,
) -> bool:
    now = datetime.now(timezone.utc)
    if event.notification_status in ("sent", "skipped"):
        return False

    if event.next_retry_at is not None:
        nr = event.next_retry_at
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=timezone.utc)
        if nr > now:
            return False

    if (
        event.lease_owner is not None
        and event.lease_owner != owner_id
        and event.lease_expires_at is not None
    ):
        exp = event.lease_expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now:
            return False

    event.lease_owner = owner_id
    event.lease_expires_at = now + timedelta(seconds=ttl_seconds)
    event.notification_status = "processing"
    session.flush()
    return True

def _subscription_matches_event(
    sub: DiscordSubscription,
    event: ChangeEvent,
    source_app_norms: Set[str],
) -> bool:
    types_raw = (sub.subscribed_event_types or "all").lower()
    subscribed_set = {t.strip() for t in types_raw.split(",") if t.strip()}
    if "all" not in subscribed_set and event.event_type.lower() not in subscribed_set:
        if event.event_type.lower() != "credentials_updated":
            return False

    game_filter_raw = (sub.game_filter or "").strip().lower()
    if not game_filter_raw or game_filter_raw == "all":
        return True

    filter_norms = {g.strip() for g in game_filter_raw.split(",") if g.strip()}
    for fn in filter_norms:
        if fn in source_app_norms or any(fn in sn for sn in source_app_norms):
            return True
    return False

async def dispatch_pending_notifications(
    session: Session,
    *,
    worker_id: str = "worker-main",
    sender: Optional[DiscordSenderFunc] = None,
    settings: Optional[Settings] = None,
) -> int:
    cfg = settings or get_settings()
    send_fn = sender or (lambda ch_id, emb: default_discord_rest_sender(ch_id, emb, settings=cfg))

    pending_events = session.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.notification_status.in_(["pending", "processing", "failed"]))
        .order_by(ChangeEvent.detected_at.asc(), ChangeEvent.id.asc())
    ).all()

    active_subscriptions = session.scalars(
        select(DiscordSubscription).where(DiscordSubscription.enabled == True)
    ).all()

    completed_count = 0

    for event in pending_events:
        if event.notification_attempts >= cfg.notification_max_attempts:
            if event.notification_status != "failed":
                event.notification_status = "failed"
                session.flush()
            continue

        if not acquire_event_notification_lease(session, event, worker_id):
            continue

        game_titles, source_app_norms = _get_source_apps_and_norms(session, event.source_id)
        matching_subs = [
            sub
            for sub in active_subscriptions
            if _subscription_matches_event(sub, event, source_app_norms)
        ]

        if not matching_subs:
            event.notification_status = "pending"
            event.lease_owner = None
            event.lease_expires_at = None
            session.flush()
            continue

        source = session.get(Source, event.source_id)
        embed_dict = build_change_notification_embed_dict(event, source, game_titles=game_titles)

        already_delivered_channels = set(
            session.scalars(
                select(NotificationDelivery.channel_id).where(
                    NotificationDelivery.change_event_id == event.id
                )
            ).all()
        )

        delivery_errors: List[str] = []
        for sub in matching_subs:
            if sub.channel_id in already_delivered_channels:
                continue
            try:
                await send_fn(sub.channel_id, embed_dict)
                session.add(
                    NotificationDelivery(
                        change_event_id=event.id,
                        channel_id=sub.channel_id,
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                already_delivered_channels.add(sub.channel_id)
                session.flush()
            except Exception as exc:
                safe_err = redact_sensitive_text(str(exc))
                delivery_errors.append(f"channel={sub.channel_id}: {safe_err}")
                logger.warning(
                    "Failed delivering change_event=%d to channel=%s: %s",
                    event.id,
                    sub.channel_id,
                    safe_err,
                )

        now = datetime.now(timezone.utc)
        event.lease_owner = None
        event.lease_expires_at = None

        if not delivery_errors:
            event.notification_status = "sent"
            event.last_notification_error = None
            event.next_retry_at = None
            completed_count += 1
        else:
            event.notification_attempts += 1
            event.last_notification_error = "; ".join(delivery_errors)[:1000]
            backoff_seconds = min(300, 5 * (2 ** max(0, event.notification_attempts - 1)))
            event.next_retry_at = now + timedelta(seconds=backoff_seconds)
            if event.notification_attempts >= cfg.notification_max_attempts:
                event.notification_status = "failed"
            else:
                event.notification_status = "pending"

        session.flush()

    return completed_count