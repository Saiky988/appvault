"""Tests for HTML parsing, credential exclusion, SSRF prevention, HTTP 304, change detection, and notifications."""
from __future__ import annotations

from typing import Dict, List

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import (
    SSRFValidationError,
    contains_sensitive_credentials,
    redact_sensitive_text,
    validate_url_for_ssrf,
)
from app.db.models import ChangeEvent, NotificationDelivery, Source, SourceApp
from app.monitoring.change_detector import apply_fetch_and_detect_changes
from app.monitoring.fetcher import FetchResult, SafePageFetcher
from app.monitoring.notifications import dispatch_pending_notifications
from app.monitoring.parser import parse_public_listing_html
from app.monitoring.scheduler import (
    MonitoringWorker,
    SourceCheckAlreadyRunningError,
    check_single_source,
)
from app.services.catalog import reload_seed_catalog
from app.services.sources import (
    acquire_source_lease,
    list_source_apps_paginated,
    set_worker_paused,
    upsert_discord_subscription,
)

SYNTHETIC_BLOG_HTML_V1 = """
<!DOCTYPE html>
<html>
<head>
  <title>Source 15 Game List</title>
  <script>var secretToken = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9";</script>
</head>
<body>
  <div class="navbar">Navigation Header</div>
  <article>
    <h1 class="post-title">ID #15 Public Game Catalog</h1>
    <div class="post-timestamp">Updated at 2026-10-20 12:00:00 PM</div>
    <div class="post-body">
      <p>Lưu ý quan trọng: Chỉ đăng nhập App Store, không đăng nhập iCloud.</p>
      <p>Apple ID: leaked_account_user@icloud.com</p>
      <p>Mật khẩu: SuperSecretPassword123!</p>
      <p>Recovery Code: 9988-7766-5544</p>
      <p>Session Cookie: session_id=xyz987secret</p>
      <ul>
        <li>After Inc.</li>
        <li>Balatro</li>
        <li>Dead Cells+</li>
        <li>Pass: ShouldNeverBeExtracted999</li>
        <li>user_login@gmail.com</li>
      </ul>
    </div>
  </article>
  <footer>Copyright 2026</footer>
</body>
</html>
"""

SYNTHETIC_BLOG_HTML_V1_DIFF_TIMESTAMP = SYNTHETIC_BLOG_HTML_V1.replace(
    "Updated at 2026-10-20 12:00:00 PM",
    "Updated at 2026-10-20 11:59:59 PM",
)

SYNTHETIC_BLOG_HTML_V2 = """
<!DOCTYPE html>
<html>
<body>
  <article>
    <h1 class="post-title">ID #15 Public Game Catalog</h1>
    <div class="post-body">
      <ul>
        <li>After Inc.</li>
        <li>Balatro</li>
        <li>Dead Cells+</li>
        <li>Hades II</li>
      </ul>
    </div>
  </article>
</body>
</html>
"""


def test_html_parser_extracts_apps_and_excludes_credentials() -> None:
    """Verify synthetic HTML parsing extracts valid apps while stripping all credential-like lines."""
    parsed = parse_public_listing_html(SYNTHETIC_BLOG_HTML_V1)
    assert parsed.title == "ID #15 Public Game Catalog"
    assert parsed.apps == ["After Inc.", "Balatro", "Dead Cells+"]
    assert parsed.had_redacted_lines is True

    joined_output = " ".join(parsed.apps + parsed.sanitized_body_lines).lower()
    assert "leaked_account_user@icloud.com" not in joined_output
    assert "supersecretpassword123" not in joined_output
    assert "9988-7766-5544" not in joined_output
    assert "session_id=xyz987secret" not in joined_output
    assert "shouldneverbeextracted999" not in joined_output
    assert "user_login@gmail.com" not in joined_output

    parsed_same = parse_public_listing_html(SYNTHETIC_BLOG_HTML_V1_DIFF_TIMESTAMP)
    assert parsed.content_hash == parsed_same.content_hash


def test_malformed_html_and_sensitive_text_helpers() -> None:
    """Verify parser handles malformed HTML gracefully and security filters catch secrets."""
    malformed = "<div><h1>Broken List<ul><li>Terraria<li>Stardew Valley<p>Pass: HideMe123"
    parsed = parse_public_listing_html(malformed)
    assert "Terraria" in parsed.apps
    assert "Stardew Valley" in parsed.apps
    assert all("hideme123" not in a.lower() for a in parsed.apps)

    assert contains_sensitive_credentials("Mật khẩu: MySecret99") is True
    assert contains_sensitive_credentials("Apple ID: test@icloud.com") is True
    assert contains_sensitive_credentials("Balatro") is False
    assert "[REDACTED" in redact_sensitive_text("Account test@icloud.com Pass: Secret123")


def test_ssrf_validation_blocks_private_ips_localhost_and_bad_schemes() -> None:
    """Verify SSRF validator blocks non-HTTP schemes, localhost, private IPs, and metadata endpoints."""
    for blocked_url in (
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://localhost:8000/api/v1/health",
        "http://127.0.0.1/admin",
        "http://10.0.0.1/internal",
        "http://172.16.5.4/secret",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8000/",
        "https://user:password@example.com/page",
        "http://internal.corp/app",
    ):
        with pytest.raises(SSRFValidationError):
            validate_url_for_ssrf(blocked_url, resolve_dns=False)

    with pytest.raises(SSRFValidationError):
        validate_url_for_ssrf(
            "https://evil-rebind.example.com/list",
            resolver=lambda _host, _port: ["192.168.10.25"],
            resolve_dns=True,
        )

    safe_url = validate_url_for_ssrf(
        "https://public-catalog.example.com/post",
        resolver=lambda _host, _port: ["93.184.216.34"],
        resolve_dns=True,
    )
    assert safe_url == "https://public-catalog.example.com/post"


@pytest.mark.asyncio
async def test_fetcher_blocks_unsafe_redirect_and_handles_http_errors() -> None:
    """Verify SafePageFetcher blocks redirects to private IPs and handles 500/404 errors safely."""
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "redirect-to-local" in url_str:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/secret"})
        if "server-error" in url_str:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=SYNTHETIC_BLOG_HTML_V1)

    fetcher = SafePageFetcher(
        transport=httpx.MockTransport(handler),
        dns_resolver=lambda _h, _p: ["93.184.216.34"],
        sleep_func=lambda _s: _noop_async(),
    )

    redirect_res = await fetcher.fetch("https://example.com/redirect-to-local")
    assert redirect_res.ok is False
    assert "SSRF" in (redirect_res.error or "")

    err_res = await fetcher.fetch("https://example.com/server-error")
    assert err_res.ok is False
    assert err_res.status_code == 503

    ok_res = await fetcher.fetch("https://example.com/valid-post")
    assert ok_res.ok is True
    assert ok_res.status_code == 200
    assert "Balatro" in ok_res.content


async def _noop_async() -> None:
    return None


def test_http_304_not_modified_preserves_seed_records(db_session: Session) -> None:
    """Verify HTTP 304 Not Modified never interprets response as empty and preserves all seed records."""
    reload_seed_catalog(db_session)
    db_session.commit()

    source20 = db_session.get(Source, 20)
    assert source20 is not None

    res_304 = FetchResult(
        url=source20.url,
        ok=True,
        not_modified=True,
        status_code=304,
        etag='"etag-20"',
    )
    outcome = apply_fetch_and_detect_changes(db_session, source20, res_304)
    db_session.commit()

    assert outcome.status == "not_modified"
    assert outcome.http_status == 304
    assert outcome.changed is False
    assert outcome.extracted_app_count == 10
    assert outcome.data_origin == "seed"

    paginated = list_source_apps_paginated(db_session, 20, limit=50, offset=0)
    assert paginated is not None
    assert paginated.total == 10
    assert all(item.data_origin == "seed" for item in paginated.items)
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 0


def test_change_detection_and_source_failure_preserves_catalog(db_session: Session) -> None:
    """Verify failed fetches never delete seed apps, seed apps stay intact, and updates create 1 event."""
    reload_seed_catalog(db_session)
    db_session.commit()

    source15 = db_session.get(Source, 15)
    assert source15 is not None

    # 1. Failed HTTP request must preserve all 12 seed apps on Source 15
    failed_fetch = FetchResult(
        url=source15.url,
        ok=False,
        status_code=502,
        error="HTTP 502 Bad Gateway",
    )
    out_fail = apply_fetch_and_detect_changes(db_session, source15, failed_fetch)
    db_session.commit()

    assert out_fail.status == "error"
    assert out_fail.changed is False
    assert source15.last_error == "HTTP 502 Bad Gateway"
    active_count = len(
        db_session.scalars(
            select(SourceApp).where(SourceApp.source_id == 15, SourceApp.is_active == True)  # noqa: E712
        ).all()
    )
    assert active_count == 12
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 0

    # 2. First successful observation upgrades observed items while preserving all 12 seed entries + 1 new (Dead Cells+) = 13
    ok_fetch_v1 = FetchResult(
        url=source15.url,
        ok=True,
        status_code=200,
        content=SYNTHETIC_BLOG_HTML_V1,
        etag='"v1-etag"',
    )
    out_v1 = apply_fetch_and_detect_changes(db_session, source15, ok_fetch_v1)
    db_session.commit()

    assert out_v1.status == "changed"
    assert out_v1.changed is True
    assert out_v1.data_origin == "observed"
    assert out_v1.extracted_app_count == 13
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 1

    # 3. Fetching identical content (even with different timestamp noise) creates NO new ChangeEvent
    ok_fetch_same = FetchResult(
        url=source15.url,
        ok=True,
        status_code=200,
        content=SYNTHETIC_BLOG_HTML_V1_DIFF_TIMESTAMP,
    )
    out_same = apply_fetch_and_detect_changes(db_session, source15, ok_fetch_same)
    db_session.commit()

    assert out_same.status == "unchanged"
    assert out_same.changed is False
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 1

    # 4. Fetching updated app list (Hades II added) creates apps_updated ChangeEvent (14 active apps)
    ok_fetch_v2 = FetchResult(
        url=source15.url,
        ok=True,
        status_code=200,
        content=SYNTHETIC_BLOG_HTML_V2,
    )
    out_v2 = apply_fetch_and_detect_changes(db_session, source15, ok_fetch_v2)
    db_session.commit()

    assert out_v2.status == "changed"
    assert out_v2.changed is True
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 2
    latest_ev = db_session.get(ChangeEvent, out_v2.change_event_id)
    assert latest_ev is not None
    assert latest_ev.event_type == "apps_updated"
    assert "Hades II" in latest_ev.summary

    # 5. Subsequent network failure after live observation still preserves all 14 apps
    out_fail_after = apply_fetch_and_detect_changes(db_session, source15, failed_fetch)
    db_session.commit()
    assert out_fail_after.status == "error"
    assert out_fail_after.extracted_app_count == 14


@pytest.mark.asyncio
async def test_notification_retry_and_duplicate_delivery_prevention(db_session: Session) -> None:
    """Verify failed channel delivery persists retry state without resending to already-delivered channels."""
    reload_seed_catalog(db_session)
    upsert_discord_subscription(db_session, guild_id="100", channel_id="chan_ok")
    upsert_discord_subscription(db_session, guild_id="100", channel_id="chan_flaky")
    source15 = db_session.get(Source, 15)
    assert source15 is not None

    apply_fetch_and_detect_changes(
        db_session,
        source15,
        FetchResult(url=source15.url, ok=True, status_code=200, content=SYNTHETIC_BLOG_HTML_V1),
    )
    db_session.commit()

    sent_log: List[str] = []
    fail_flaky = True

    async def mock_sender(channel_id: str, _embed: Dict[str, object]) -> None:
        if channel_id == "chan_flaky" and fail_flaky:
            raise RuntimeError("Simulated Discord 502 error")
        sent_log.append(channel_id)

    completed_1 = await dispatch_pending_notifications(
        db_session,
        worker_id="test-worker",
        sender=mock_sender,
        settings=get_settings(),
    )
    db_session.commit()

    assert completed_1 == 0
    assert sent_log == ["chan_ok"]
    ev = db_session.scalar(select(ChangeEvent))
    assert ev is not None
    assert ev.notification_status == "pending"
    assert ev.notification_attempts == 1
    assert ev.last_notification_error is not None

    ev.next_retry_at = None
    fail_flaky = False
    db_session.commit()

    completed_2 = await dispatch_pending_notifications(
        db_session,
        worker_id="test-worker",
        sender=mock_sender,
        settings=get_settings(),
    )
    db_session.commit()

    assert completed_2 == 1
    assert sent_log == ["chan_ok", "chan_flaky"]
    assert ev.notification_status == "sent"
    assert db_session.scalar(select(func.count(NotificationDelivery.id))) == 2


@pytest.mark.asyncio
async def test_worker_lease_and_pause_safeguards(db_session: Session) -> None:
    """Verify source leases prevent concurrent duplicate checks and paused workers skip polling."""
    reload_seed_catalog(db_session)
    leased = acquire_source_lease(db_session, 19, owner_id="worker-A", ttl_seconds=120)
    db_session.commit()
    assert leased is True

    with pytest.raises(SourceCheckAlreadyRunningError):
        await check_single_source(19, worker_id="worker-B")

    set_worker_paused(db_session, True)
    db_session.commit()

    worker = MonitoringWorker(settings=get_settings())
    outcomes = await worker.run_single_cycle()
    assert outcomes == []
