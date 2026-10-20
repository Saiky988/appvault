"""Persistent Discord notification dispatcher with DB-backed lease, backoff retries, and per-channel deduplication."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import redact_sensitive_text
from app.db.models import ChangeEvent, DiscordSubscription, NotificationDelivery, Source

logger = logging.getLogger("appvault.notifications")

# Optional callback signature: async def send_fn(channel_id: str, embed_dict: Dict[str, Any]) -> None
DiscordSenderFunc = Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]


def build_change_notification_embed_dict(event: ChangeEvent, source: Optional[Source]) -> Dict[str, Any]:
    """Construct a safe, secret-scrubbed, icon-free Discord embed payload for a ChangeEvent."""
    safe_summary = redact_sensitive_text(event.summary or "Source update detected.")
    source_url = source.url if source else None
    source_label = f"Source #{event.source_id}" + (f" (ID {source.source_key})" if source else "")

    return {
        "title": f"AppVault Update — {source_label}",
        "description": safe_summary[:2000],
        "url": source_url,
        "color": 0x2ECC71 if event.event_type in ("apps_updated", "initial_observation") else 0x3498DB,
        "timestamp": event.detected_at.astimezone(timezone.utc).isoformat(),
        "fields": [
            {"name": "Event Type", "value": f"`{event.event_type}`", "inline": True},
            {"name": "Source ID", "value": f"`{event.source_id}`", "inline": True},
            {
                "name": "Content Hash",
                "value": f"`{event.current_hash[:12]}...`",
                "inline": True,
            },
        ],
        "footer": {"text": "AppVault Monitor | Credentials & sensitive data excluded"},
    }


async def default_discord_rest_sender(
    channel_id: str,
    embed_dict: Dict[str, Any],
    *,
    settings: Optional[Settings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> None:
    """Send an embed to a Discord channel via Discord REST API v10 with safe allowed_mentions."""
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
    """Claim a pending ChangeEvent for notification delivery so multiple workers do not collide."""
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


async def dispatch_pending_notifications(
    session: Session,
    *,
    worker_id: str = "worker-main",
    sender: Optional[DiscordSenderFunc] = None,
    settings: Optional[Settings] = None,
) -> int:
    """Dispatch pending ChangeEvents to enabled DiscordSubscriptions with retry backoff and deduplication.

    Returns the number of ChangeEvents marked as 'sent' or 'skipped'.
    """
    cfg = settings or get_settings()
    send_fn = sender or (lambda ch_id, emb: default_discord_rest_sender(ch_id, emb, settings=cfg))

    pending_events = session.scalars(
        select(ChangeEvent)
        .where(ChangeEvent.notification_status.in_(["pending", "processing", "failed"]))
        .order_by(ChangeEvent.detected_at.asc(), ChangeEvent.id.asc())
    ).all()

    active_subscriptions = session.scalars(
        select(DiscordSubscription).where(DiscordSubscription.enabled == True)  # noqa: E712
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

        matching_subs: List[DiscordSubscription] = []
        for sub in active_subscriptions:
            types_raw = (sub.subscribed_event_types or "all").lower()
            subscribed_set = {t.strip() for t in types_raw.split(",") if t.strip()}
            if "all" in subscribed_set or event.event_type.lower() in subscribed_set:
                matching_subs.append(sub)

        if not matching_subs:
            event.notification_status = "pending"
            event.lease_owner = None
            event.lease_expires_at = None
            session.flush()
            continue

        source = session.get(Source, event.source_id)
        embed_dict = build_change_notification_embed_dict(event, source)

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
