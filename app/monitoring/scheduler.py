"""Background monitoring worker and source check orchestrator."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import List, Optional

from sqlalchemy import select

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.models import Source  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.monitoring.change_detector import apply_fetch_and_detect_changes  # noqa: E402
from app.monitoring.fetcher import SafePageFetcher  # noqa: E402
from app.monitoring.notifications import (  # noqa: E402
    DiscordSenderFunc,
    dispatch_pending_notifications,
)
from app.schemas.api import SourceCheckOutcomeSchema  # noqa: E402
from app.services.catalog import reload_seed_catalog  # noqa: E402
from app.services.sources import (  # noqa: E402
    acquire_source_lease,
    get_or_create_worker_state,
    get_source_by_id_or_key,
    record_worker_heartbeat,
    release_source_lease,
)

logger = logging.getLogger("appvault.worker")


class SourceCheckAlreadyRunningError(RuntimeError):
    """Raised when a manual or scheduled check cannot acquire the source lease."""


async def check_single_source(
    source_id: int,
    *,
    worker_id: str = "manual-check",
    fetcher: Optional[SafePageFetcher] = None,
    notification_sender: Optional[DiscordSenderFunc] = None,
    force: bool = True,
) -> SourceCheckOutcomeSchema:
    """Execute a complete check cycle for a single Source with DB lease coordination."""
    settings = get_settings()
    page_fetcher = fetcher or SafePageFetcher(settings=settings)

    with session_scope() as session:
        source = get_source_by_id_or_key(session, source_id)
        if source is None:
            raise LookupError(f"Source {source_id} not found")

        if not force and not source.enabled:
            return SourceCheckOutcomeSchema(
                source_id=source.id,
                source_key=source.source_key,
                url=source.url,
                status="disabled",
                http_status=source.last_http_status,
                changed=False,
                change_event_id=None,
                extracted_app_count=0,
                data_origin="seed",
                error=source.last_error,
                checked_at=datetime.now(timezone.utc),
            )

        if not acquire_source_lease(session, source.id, worker_id):
            raise SourceCheckAlreadyRunningError(
                f"Source #{source.id} is already being checked by another worker ({source.lease_owner})"
            )

        actual_id = source.id
        target_url = source.url
        etag = source.etag
        last_modified = source.last_modified

    try:
        fetch_result = await page_fetcher.fetch(
            target_url,
            etag=etag,
            last_modified=last_modified,
        )

        with session_scope() as session:
            source = session.get(Source, actual_id)
            if source is None:
                raise LookupError(f"Source {actual_id} disappeared during check")
            outcome = apply_fetch_and_detect_changes(session, source, fetch_result)
            if outcome.changed:
                await dispatch_pending_notifications(
                    session,
                    worker_id=worker_id,
                    sender=notification_sender,
                    settings=settings,
                )
            return outcome
    finally:
        with session_scope() as session:
            release_source_lease(session, actual_id, owner_id=worker_id)


def _is_source_due(source: Source, default_interval: int, now: datetime) -> bool:
    """Return True if an enabled source is due for polling based on its interval."""
    if not source.enabled:
        return False
    if source.last_checked_at is None:
        return True
    last_chk = source.last_checked_at
    if last_chk.tzinfo is None:
        last_chk = last_chk.replace(tzinfo=timezone.utc)
    interval = source.check_interval_seconds or default_interval
    elapsed = (now - last_chk).total_seconds()
    return elapsed >= interval


async def check_all_enabled_sources(
    *,
    worker_id: str = "bulk-check",
    fetcher: Optional[SafePageFetcher] = None,
    notification_sender: Optional[DiscordSenderFunc] = None,
    only_due: bool = False,
) -> List[SourceCheckOutcomeSchema]:
    """Check all enabled sources concurrently up to MONITOR_CONCURRENCY."""
    settings = get_settings()
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        enabled_sources = session.scalars(
            select(Source).where(Source.enabled == True).order_by(Source.id.asc())  # noqa: E712
        ).all()
        target_ids = [
            s.id
            for s in enabled_sources
            if (not only_due or _is_source_due(s, settings.monitor_interval_seconds, now))
        ]

    semaphore = asyncio.Semaphore(settings.monitor_concurrency)
    outcomes: List[SourceCheckOutcomeSchema] = []

    async def _bounded_check(sid: int) -> Optional[SourceCheckOutcomeSchema]:
        async with semaphore:
            try:
                return await check_single_source(
                    sid,
                    worker_id=worker_id,
                    fetcher=fetcher,
                    notification_sender=notification_sender,
                    force=True,
                )
            except SourceCheckAlreadyRunningError:
                logger.info("Skipping source #%d: already leased by another worker", sid)
                return None
            except Exception as exc:
                logger.error("Error checking source #%d: %s", sid, exc)
                return None

    results = await asyncio.gather(*(_bounded_check(sid) for sid in target_ids))
    for res in results:
        if res is not None:
            outcomes.append(res)

    # Also retry any pending notifications whose backoff timer has elapsed
    with session_scope() as session:
        await dispatch_pending_notifications(
            session,
            worker_id=worker_id,
            sender=notification_sender,
            settings=settings,
        )

    return outcomes


class MonitoringWorker:
    """Dedicated asynchronous polling worker with clean SIGTERM/SIGINT shutdown."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        fetcher: Optional[SafePageFetcher] = None,
        notification_sender: Optional[DiscordSenderFunc] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.fetcher = fetcher or SafePageFetcher(settings=self.settings)
        self.notification_sender = notification_sender
        self.worker_id = f"worker-{socket.gethostname()}-{os.getpid()}"
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """Signal the worker loop to exit cleanly."""
        logger.info("Shutdown signal received for worker %s", self.worker_id)
        self._stop_event.set()

    async def run_single_cycle(self) -> List[SourceCheckOutcomeSchema]:
        """Run one heartbeat + polling + notification retry cycle."""
        with session_scope() as session:
            state = record_worker_heartbeat(session, self.worker_id)
            is_paused = state.is_paused

        if is_paused:
            logger.debug("Worker %s is globally paused; skipping source polling.", self.worker_id)
            return []

        return await check_all_enabled_sources(
            worker_id=self.worker_id,
            fetcher=self.fetcher,
            notification_sender=self.notification_sender,
            only_due=True,
        )

    async def run_forever(self, tick_seconds: float = 15.0) -> None:
        """Start background polling loop until SIGTERM/SIGINT or request_stop()."""
        init_db()
        with session_scope() as session:
            reload_seed_catalog(session)
            record_worker_heartbeat(session, self.worker_id)

        logger.info(
            "AppVault MonitoringWorker started (id=%s, interval=%ds, concurrency=%d)",
            self.worker_id,
            self.settings.monitor_interval_seconds,
            self.settings.monitor_concurrency,
        )

        while not self._stop_event.is_set():
            try:
                await self.run_single_cycle()
            except Exception as exc:
                logger.error("Worker cycle error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick_seconds)
            except asyncio.TimeoutError:
                pass

        logger.info("AppVault MonitoringWorker %s stopped cleanly.", self.worker_id)


async def _async_main() -> int:
    settings = get_settings()
    setup_logging(
        settings.log_level,
        secrets_to_redact=[settings.api_admin_token, settings.discord_bot_token],
    )
    worker = MonitoringWorker(settings=settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:
            pass

    await worker.run_forever()
    return 0


def main() -> int:
    return asyncio.run(_async_main())


if __name__ == "__main__":
    raise SystemExit(main())
