"""Change detection, account credential extraction, and source catalog update pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import List, Optional, Set

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import redact_sensitive_text
from app.db.models import App, ChangeEvent, Source, SourceApp
from app.monitoring.fetcher import FetchResult
from app.monitoring.parser import ParsedArticle, parse_public_listing_html
from app.schemas.api import SourceCheckOutcomeSchema
from app.services.catalog import (
    SEED_SOURCES,
    ensure_app_and_aliases,
    normalize_app_name,
)


def _get_seed_normalized_names_for_source(source_id: int) -> Set[str]:
    """Return the set of normalized app names belonging to a source's seed specification."""
    for spec in SEED_SOURCES:
        if int(spec["id"]) == int(source_id):
            return {
                normalize_app_name(t, apply_spelling_fixes=True)
                for t in spec.get("apps", ())
                if normalize_app_name(t, apply_spelling_fixes=True)
            }
    return set()


def _determine_source_origin(links: List[SourceApp]) -> str:
    """Return 'observed' if any active link is from observed page content, else 'seed'."""
    if not links:
        return "seed"
    if any(lk.data_origin == "observed" for lk in links):
        return "observed"
    return "seed"


def apply_fetch_and_detect_changes(
    session: Session,
    source: Source,
    fetch_result: FetchResult,
) -> SourceCheckOutcomeSchema:
    """Process a FetchResult for a Source, updating account credentials and preserving seed apps."""
    now = datetime.now(timezone.utc)
    source.last_checked_at = now
    source.updated_at = now

    all_links = session.scalars(
        select(SourceApp)
        .where(SourceApp.source_id == source.id)
        .order_by(SourceApp.display_order.asc(), SourceApp.app_id.asc())
    ).all()

    seed_norms = _get_seed_normalized_names_for_source(source.id)
    for lk in all_links:
        if lk.data_origin == "seed" and not lk.is_active:
            lk.is_active = True
        elif not lk.is_active and seed_norms:
            app_obj = session.get(App, lk.app_id)
            if app_obj and app_obj.normalized_name in seed_norms:
                lk.is_active = True

    active_links = [lk for lk in all_links if lk.is_active]
    current_origin = _determine_source_origin(active_links)

    # 1. Handle failed HTTP fetch: never erase or deactivate existing catalog data
    if not fetch_result.ok:
        safe_error = redact_sensitive_text(fetch_result.error or "HTTP check failed")
        source.last_http_status = fetch_result.status_code
        source.last_error = safe_error
        session.flush()
        return SourceCheckOutcomeSchema(
            source_id=source.id,
            source_key=source.source_key,
            url=source.url,
            status="error",
            http_status=fetch_result.status_code,
            changed=False,
            change_event_id=None,
            extracted_app_count=len(active_links),
            data_origin=current_origin,
            account_email=source.account_email,
            account_password=source.account_password,
            error=safe_error,
            checked_at=now,
        )

    # 2. Handle HTTP 304 Not Modified: preserve seed/observed records and existing credentials
    if fetch_result.not_modified:
        source.last_success_at = now
        source.last_http_status = 304
        source.last_error = None
        if fetch_result.etag:
            source.etag = fetch_result.etag
        if fetch_result.last_modified:
            source.last_modified = fetch_result.last_modified
        session.flush()
        return SourceCheckOutcomeSchema(
            source_id=source.id,
            source_key=source.source_key,
            url=source.url,
            status="not_modified",
            http_status=304,
            changed=False,
            change_event_id=None,
            extracted_app_count=len(active_links),
            data_origin=current_origin,
            account_email=source.account_email,
            account_password=source.account_password,
            error=None,
            checked_at=now,
        )

    # 3. Parse HTML content
    parsed: ParsedArticle = parse_public_listing_html(fetch_result.content)

    has_credentials = bool(parsed.account_email or parsed.account_password)
    if not parsed.apps and not parsed.sanitized_body_lines and not has_credentials:
        err_msg = "Page fetched but no parseable public app listing or article content found; preserving catalog"
        source.last_http_status = fetch_result.status_code or 200
        source.last_error = err_msg
        session.flush()
        return SourceCheckOutcomeSchema(
            source_id=source.id,
            source_key=source.source_key,
            url=source.url,
            status="parse_error",
            http_status=source.last_http_status,
            changed=False,
            change_event_id=None,
            extracted_app_count=len(active_links),
            data_origin=current_origin,
            account_email=source.account_email,
            account_password=source.account_password,
            error=err_msg,
            checked_at=now,
        )

    previous_hash = source.current_content_hash
    prev_email = source.account_email
    prev_password = source.account_password

    credentials_changed = False
    if parsed.account_email is not None or parsed.account_password is not None:
        if parsed.account_email != prev_email or parsed.account_password != prev_password:
            credentials_changed = True
        if parsed.account_email is not None:
            source.account_email = parsed.account_email
        if parsed.account_password is not None:
            source.account_password = parsed.account_password
        source.account_updated_at = now
        # Mark seed links on this live-verified source as observed
        for lk in active_links:
            lk.data_origin = "observed"
            lk.last_seen_at = now

    old_app_names: List[str] = []
    for lk in active_links:
        app_obj = session.get(App, lk.app_id)
        if app_obj:
            old_app_names.append(app_obj.canonical_name)

    added_titles: List[str] = []
    removed_titles: List[str] = []

    if parsed.apps:
        new_app_ids_ordered: List[int] = []
        new_canonical_names: List[str] = []
        max_order = max((lk.display_order for lk in active_links), default=-1)

        for idx, raw_title in enumerate(parsed.apps):
            try:
                app_obj, _created, _alias_cnt = ensure_app_and_aliases(session, raw_title)
            except ValueError:
                continue
            if app_obj.id in new_app_ids_ordered:
                continue
            new_app_ids_ordered.append(app_obj.id)
            new_canonical_names.append(app_obj.canonical_name)

            existing_link = session.get(SourceApp, (source.id, app_obj.id))
            if existing_link is None:
                max_order += 1
                session.add(
                    SourceApp(
                        source_id=source.id,
                        app_id=app_obj.id,
                        original_title=raw_title,
                        display_order=idx if not active_links else max_order,
                        data_origin="observed",
                        is_active=True,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                if existing_link.data_origin != "seed":
                    existing_link.original_title = raw_title
                    existing_link.display_order = idx
                existing_link.data_origin = "observed"
                existing_link.is_active = True
                existing_link.last_seen_at = now

        old_set = set(old_app_names)
        added_titles = [n for n in new_canonical_names if n not in old_set]

        for lk in active_links:
            if lk.app_id not in new_app_ids_ordered:
                app_obj = session.get(App, lk.app_id)
                is_seed_entry = (lk.data_origin == "seed") or (
                    app_obj is not None and app_obj.normalized_name in seed_norms
                )
                if not is_seed_entry:
                    lk.is_active = False
                    if app_obj:
                        removed_titles.append(app_obj.canonical_name)

    session.flush()
    updated_active_links = session.scalars(
        select(SourceApp)
        .where(SourceApp.source_id == source.id, SourceApp.is_active == True)  # noqa: E712
        .order_by(SourceApp.display_order.asc())
    ).all()
    effective_origin = _determine_source_origin(updated_active_links)
    active_count = len(updated_active_links)

    source.last_success_at = now
    source.last_http_status = fetch_result.status_code or 200
    source.last_error = None
    if fetch_result.etag:
        source.etag = fetch_result.etag
    if fetch_result.last_modified:
        source.last_modified = fetch_result.last_modified

    new_hash = parsed.content_hash
    if previous_hash == new_hash and not credentials_changed:
        session.flush()
        return SourceCheckOutcomeSchema(
            source_id=source.id,
            source_key=source.source_key,
            url=source.url,
            status="unchanged",
            http_status=source.last_http_status,
            changed=False,
            change_event_id=None,
            extracted_app_count=active_count,
            data_origin=effective_origin,
            account_email=source.account_email,
            account_password=source.account_password,
            error=None,
            checked_at=now,
        )

    if credentials_changed:
        event_type = "credentials_updated" if previous_hash is not None else "initial_observation"
        summary = (
            f"ID #{source.id} updated account credentials "
            f"(Account: {source.account_email or 'N/A'})."
        )
    elif previous_hash is None:
        event_type = "initial_observation"
        summary_parts = [f"Source #{source.id} verified live ({active_count} apps in catalog)."]
        if added_titles:
            summary_parts.append(f"Added: {', '.join(added_titles[:5])}")
        if removed_titles:
            summary_parts.append(f"Removed: {', '.join(removed_titles[:5])}")
        summary = " ".join(summary_parts)
    elif added_titles or removed_titles:
        event_type = "apps_updated"
        parts: List[str] = [f"Source #{source.id} catalog updated."]
        if added_titles:
            parts.append(f"Added ({len(added_titles)}): {', '.join(added_titles[:6])}.")
        if removed_titles:
            parts.append(f"Removed ({len(removed_titles)}): {', '.join(removed_titles[:6])}.")
        summary = " ".join(parts)
    else:
        event_type = "content_updated"
        safe_title = redact_sensitive_text(parsed.title or f"Source #{source.id}")
        summary = f"Source #{source.id} article content updated ('{safe_title[:80]}')."

    source.current_content_hash = new_hash

    existing_event = session.scalar(
        select(ChangeEvent).where(
            ChangeEvent.source_id == source.id,
            ChangeEvent.current_hash == new_hash,
        )
    )

    change_event_id: Optional[int] = None
    if existing_event is None:
        details = {
            "title": parsed.title,
            "added_apps": added_titles,
            "removed_apps": removed_titles,
            "observed_app_count": len(parsed.apps),
            "active_catalog_count": active_count,
            "account_email": source.account_email,
        }
        new_event = ChangeEvent(
            source_id=source.id,
            event_type=event_type,
            previous_hash=previous_hash,
            current_hash=new_hash,
            summary=summary,
            account_email=source.account_email,
            account_password=source.account_password,
            details_json=json.dumps(details, ensure_ascii=False),
            detected_at=now,
            notification_status="pending",
            notification_attempts=0,
        )
        session.add(new_event)
        session.flush()
        change_event_id = new_event.id
    else:
        change_event_id = existing_event.id

    return SourceCheckOutcomeSchema(
        source_id=source.id,
        source_key=source.source_key,
        url=source.url,
        status="changed",
        http_status=source.last_http_status,
        changed=True,
        change_event_id=change_event_id,
        extracted_app_count=active_count,
        data_origin=effective_origin,
        account_email=source.account_email,
        account_password=source.account_password,
        error=None,
        checked_at=now,
    )
