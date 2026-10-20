"""Catalog service: app normalization, iTunes artwork lookup, seed records, and idempotent seeding."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import contains_sensitive_credentials
from app.db.models import App, AppAlias, ChangeEvent, Source, SourceApp
from app.schemas.api import (
    AppDetailSchema,
    AppSummarySchema,
    CatalogReloadResponseSchema,
    ChangeEventSchema,
    PaginatedResponse,
    SourceBriefSchema,
)

SEED_CATALOG_VERSION = "2026.10.5"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
_ICON_MEMORY_CACHE: Dict[str, str] = {}

SPELLING_CORRECTIONS: Dict[str, str] = {
    "subway subfers": "subway surfers",
    "plan vs zombile 2": "plants vs zombies 2",
    "plan vs zombie 2": "plants vs zombies 2",
    "guns n glory premmium": "guns n glory premium",
    "gunsnglory premmium": "guns n glory premium",
}

CURATED_ALIASES_BY_NORMALIZED_NAME: Dict[str, List[str]] = {
    "subway surfers": ["Subway Surfers", "Subway subfers"],
    "plants vs zombies 2": ["Plants vs. Zombies 2", "Plants vs Zombies 2", "Plan vs Zombile 2", "PvZ 2"],
    "guns n glory premium": ["Guns'n'Glory Premium", "Guns’n’Glory Premmium", "Guns n Glory Premium"],
    "minecraft play with friends": [
        "Minecraft",
        "Minecraft: Play with Friends",
        "Minecraft: Play with friends!",
        "Minecraft PE",
    ],
    "titan quest legendary e": ["Titan Quest: Legendary Edition", "Titan Quest"],
    "parabellum siege of": ["Parabellum: Siege of Legends", "Parabellum"],
    "goat simulator 3 m": ["Goat Simulator 3 - Multiverse", "Goat Simulator 3"],
    "star wars kotor": ["Star Wars: Knights of the Old Republic", "KOTOR", "KOTOR 1"],
    "star wars kotor ii": ["Star Wars: Knights of the Old Republic II", "KOTOR 2", "KOTOR II"],
    "tabs pocket edition": ["Totally Accurate Battle Simulator", "TABS"],
    "street fighter iv ce": ["Street Fighter 4 Champion Edition", "Street Fighter 4 CE"],
    "naruto ultimate ninja storm": ["Naruto Storm", "Ultimate Ninja Storm"],
}

SEED_SOURCES: Tuple[Dict[str, Any], ...] = (
    {
        "id": 19,
        "source_key": "19",
        "url": "https://gfhfdhfkfkdfkdfidfdfvdvdv232323.blogspot.com/2026/03/23-huong-dan-su-dung-id-apple-body-font.html?m=1",
        "apps": (
            "Minecraft: Play with friends!",
            "Subway subfers",
            "Hay Day",
            "Clash of Clans",
            "Plan vs Zombile 2",
            "Overwinter Survival",
            "Guns’n’Glory Premmium",
            "Earn to Die 2",
            "Mika’s Treasure 2 Collection",
            "Point Quest",
        ),
    },
    {
        "id": 20,
        "source_key": "20",
        "url": "https://gfhfdhfkfkdfkdfidfdfvdvdv232323.blogspot.com/2025/10/20-huong-dan-su-dung-id-apple-body-font.html?m=1",
        "apps": (
            "Minecraft: Play with friends!",
            "Subway subfers",
            "Hay Day",
            "Clash of Clans",
            "Plan vs Zombile 2",
            "Overwinter Survival",
            "Guns’n’Glory Premmium",
            "Earn to Die 2",
            "Mika’s Treasure 2 Collection",
            "Point Quest",
        ),
    },
    {
        "id": 15,
        "source_key": "15",
        "url": "https://sangshareidfree.blogspot.com/2024/09/13-huong-dan-su-dung-id-apple-body-font.html?m=1",
        "apps": (
            "After Inc.",
            "Balatro",
            "GRID™ Legends: Deluxe Edition",
            "Total War™: EMPIRE",
            "Age of History 3",
            "Poppy Playtime Chapter 3",
            "NARUTO: Ultimate Ninja STORM",
            "Shadowrocket",
            "Egern",
            "Survivalcraft",
            "Survivalcraft 2",
            "Unpacking",
        ),
    },
    {
        "id": 1,
        "source_key": "1",
        "url": "https://sangshareidfree.blogspot.com/2024/09/huong-dan-su-dung-id-apple-body-font_11.html?m=1",
        "apps": (
            "Star Wars™: KOTOR",
            "Star Wars™: KOTOR II",
            "Minecraft: Play with Friends",
            "This Is the Police",
            "This Is the Police 2",
            "Titan Quest: Legendary E",
            "True Skate",
            "Hitman: Absolution",
            "Parabellum: Siege of",
            "TABS Pocket Edition",
            "Goat Simulator 3 - M",
            "Fortnite",
            "Forager",
            "Slay the Spire",
        ),
    },
    {
        "id": 5,
        "source_key": "5",
        "url": "https://sangshareidfree.blogspot.com/2024/09/huong-dan-su-dung-id-apple-body-font_31.html?m=1",
        "apps": (
            "Shadow Fight 2 Special Edition",
            "Monopoly - Classic Board Game",
            "Street Fighter IV CE",
            "Dealer’s Life",
            "Dealer’s Life 2",
            "nPlayer",
            "Incredibox",
            "Boris and the Dark Survival",
            "Kingdom Rush Frontiers TD",
            "Kingdom Rush Vengeance TD Game",
            "Mindustry",
            "Party Hard Go",
            "LumaFusion",
            "Procreate Pocket",
            "Grimvalor",
            "Spectre Camera",
            "This War of Mine: Stories",
            "This War of Mine",
            "ProCam 8 - Pro Camera",
            "FILCA - SLR Film Camera",
            "FL Studio Mobile",
        ),
    },
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

def normalize_raw_title(text: str) -> str:
    if not text:
        return ""
    pre_cleaned = (
        text.replace("™", " ")
        .replace("®", " ")
        .replace("©", " ")
        .replace("’", " ")
        .replace("‘", " ")
        .replace("'", " ")
        .replace("`", " ")
    )
    decomposed = unicodedata.normalize("NFKD", pre_cleaned)
    cleaned_chars = [ch for ch in decomposed if not unicodedata.combining(ch)]
    ascii_str = "".join(cleaned_chars).lower()
    tokens = _NON_ALNUM_RE.sub(" ", ascii_str).split()
    return " ".join(tokens)

def normalize_app_name(title: str, *, apply_spelling_fixes: bool = True) -> str:
    raw_norm = normalize_raw_title(title)
    if not raw_norm:
        return ""
    if apply_spelling_fixes and raw_norm in SPELLING_CORRECTIONS:
        return SPELLING_CORRECTIONS[raw_norm]
    return raw_norm

async def fetch_itunes_artwork_512(
    search_term: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[str]:
    cleaned_term = (search_term or "").strip()
    if not cleaned_term:
        return None

    cache_key = normalize_app_name(cleaned_term, apply_spelling_fixes=True) or cleaned_term.lower()
    short_key = cache_key.split()[0] if cache_key else ""
    if cache_key in _ICON_MEMORY_CACHE:
        return _ICON_MEMORY_CACHE[cache_key]
    if short_key and short_key in _ICON_MEMORY_CACHE and cache_key.startswith("minecraft"):
        return _ICON_MEMORY_CACHE[short_key]

    query_candidates = [cache_key]
    if ":" in cleaned_term:
        base_part = normalize_app_name(cleaned_term.split(":", 1)[0], apply_spelling_fixes=True)
        if base_part and base_part not in query_candidates:
            query_candidates.append(base_part)
    if cleaned_term.lower() not in query_candidates:
        query_candidates.append(cleaned_term)

    try:
        async with httpx.AsyncClient(timeout=4.0, transport=transport) as client:
            for term in query_candidates:
                resp = await client.get(
                    ITUNES_SEARCH_URL,
                    params={
                        "term": term,
                        "entity": "software",
                        "country": "us",
                        "limit": 1,
                    },
                )
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                results = payload.get("results") or []
                if results and isinstance(results[0], dict):
                    icon = (
                        results[0].get("artworkUrl512")
                        or results[0].get("artworkUrl100")
                        or results[0].get("artworkUrl60")
                    )
                    if isinstance(icon, str) and icon.startswith("http"):
                        _ICON_MEMORY_CACHE[cache_key] = icon
                        if short_key:
                            _ICON_MEMORY_CACHE.setdefault(short_key, icon)
                        return icon
    except Exception:
        return None
    return None

def compute_source_health_status(source: Source) -> str:
    if not source.enabled:
        return "disabled"
    if source.last_error:
        return "error"
    if source.last_success_at is not None:
        return "healthy"
    return "seed_only"

def ensure_app_and_aliases(
    session: Session,
    original_title: str,
    *,
    platform: str = "iOS",
) -> Tuple[App, bool, int]:
    clean_title = " ".join((original_title or "").split()).strip()
    if not clean_title or contains_sensitive_credentials(clean_title):
        raise ValueError("Invalid or sensitive app title rejected")

    norm_name = normalize_app_name(clean_title, apply_spelling_fixes=True)
    raw_norm = normalize_raw_title(clean_title)
    if not norm_name:
        raise ValueError(f"App title '{clean_title}' produced empty normalized name")

    existing_app = session.scalar(select(App).where(App.normalized_name == norm_name))
    now = datetime.now(timezone.utc)
    created_app = False

    if existing_app is None:
        existing_app = App(
            canonical_name=clean_title,
            normalized_name=norm_name,
            platform=platform,
            created_at=now,
            updated_at=now,
        )
        session.add(existing_app)
        session.flush()
        created_app = True

    aliases_to_register: List[Tuple[str, str]] = []
    if raw_norm and raw_norm != norm_name:
        aliases_to_register.append((clean_title, raw_norm))
    if clean_title != existing_app.canonical_name:
        aliases_to_register.append((clean_title, raw_norm or norm_name))

    for curated_alias in CURATED_ALIASES_BY_NORMALIZED_NAME.get(norm_name, []):
        curated_raw = normalize_raw_title(curated_alias)
        if curated_raw:
            aliases_to_register.append((curated_alias, curated_raw))

    created_aliases = 0
    existing_alias_norms = set(
        session.scalars(
            select(AppAlias.normalized_alias).where(AppAlias.app_id == existing_app.id)
        ).all()
    )

    for alias_text, alias_norm in aliases_to_register:
        if not alias_norm or alias_norm in existing_alias_norms:
            continue
        session.add(
            AppAlias(
                app_id=existing_app.id,
                alias=alias_text,
                normalized_alias=alias_norm,
            )
        )
        existing_alias_norms.add(alias_norm)
        created_aliases += 1

    session.flush()
    return existing_app, created_app, created_aliases

def reload_seed_catalog(
    session: Session,
    seed_sources: Sequence[Dict[str, Any]] = SEED_SOURCES,
) -> CatalogReloadResponseSchema:
    from app.monitoring.parser import is_ignored_candidate_title

    sources_created = 0
    sources_updated = 0
    apps_created = 0
    apps_existing = 0
    links_created = 0
    links_updated = 0
    aliases_created = 0
    now = datetime.now(timezone.utc)
    all_seed_app_ids: Set[int] = set()

    for src_spec in seed_sources:
        src_id = int(src_spec["id"])
        src_key = str(src_spec["source_key"])
        src_url = str(src_spec["url"]).strip()
        titles: Sequence[str] = src_spec["apps"]

        source = session.get(Source, src_id)
        if source is None:
            source = session.scalar(select(Source).where(Source.source_key == src_key))

        if source is None:
            source = Source(
                id=src_id,
                source_key=src_key,
                url=src_url,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(source)
            session.flush()
            sources_created += 1
        else:
            if source.url != src_url:
                source.url = src_url
                source.updated_at = now
            sources_updated += 1

        seen_app_ids_in_source: set[int] = set()
        for order_idx, raw_title in enumerate(titles):
            app_obj, was_created, new_alias_cnt = ensure_app_and_aliases(session, raw_title)
            aliases_created += new_alias_cnt
            if was_created:
                apps_created += 1
            else:
                apps_existing += 1

            seen_app_ids_in_source.add(app_obj.id)
            all_seed_app_ids.add(app_obj.id)

            link = session.get(SourceApp, (source.id, app_obj.id))
            if link is None:
                link = SourceApp(
                    source_id=source.id,
                    app_id=app_obj.id,
                    original_title=raw_title,
                    display_order=order_idx,
                    data_origin="seed",
                    is_active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(link)
                links_created += 1
            else:
                if link.data_origin != "observed":
                    link.original_title = raw_title
                link.display_order = order_idx
                link.is_active = True
                links_updated += 1

        existing_source_links = session.scalars(
            select(SourceApp).where(SourceApp.source_id == source.id)
        ).all()
        for link in existing_source_links:
            if link.app_id not in seen_app_ids_in_source and (
                is_ignored_candidate_title(link.original_title)
                or contains_sensitive_credentials(link.original_title)
            ):
                session.delete(link)

        session.flush()

    all_apps = session.scalars(select(App)).all()
    for a in all_apps:
        if a.id not in all_seed_app_ids and (
            is_ignored_candidate_title(a.canonical_name)
            or contains_sensitive_credentials(a.canonical_name)
        ):
            session.delete(a)

    all_events = session.scalars(select(ChangeEvent)).all()
    for ev in all_events:
        low_sum = (ev.summary or "").lower()
        if "không đăng nhập" in low_sum or "không chia sẻ" in low_sum:
            session.delete(ev)

    session.flush()

    return CatalogReloadResponseSchema(
        version=SEED_CATALOG_VERSION,
        sources_created=sources_created,
        sources_updated=sources_updated,
        apps_created=apps_created,
        apps_existing=apps_existing,
        links_created=links_created,
        links_updated=links_updated,
        aliases_created=aliases_created,
    )

def get_app_source_briefs(session: Session, app_id: int) -> List[SourceBriefSchema]:
    links = session.scalars(
        select(SourceApp)
        .where(SourceApp.app_id == app_id, SourceApp.is_active == True)
        .order_by(SourceApp.source_id.asc(), SourceApp.display_order.asc())
    ).all()

    if not links:
        links = session.scalars(
            select(SourceApp)
            .where(SourceApp.app_id == app_id)
            .order_by(SourceApp.source_id.asc(), SourceApp.display_order.asc())
        ).all()

    source_briefs: List[SourceBriefSchema] = []
    for link in links:
        src = session.get(Source, link.source_id)
        if src is None:
            continue
        source_briefs.append(
            SourceBriefSchema(
                source_id=src.id,
                source_key=src.source_key,
                url=src.url,
                enabled=src.enabled,
                health_status=compute_source_health_status(src),
                original_title=link.original_title,
                display_order=link.display_order,
                data_origin=link.data_origin,
                account_email=src.account_email,
                account_password=src.account_password,
                account_updated_at=src.account_updated_at,
                last_checked_at=src.last_checked_at,
                last_success_at=src.last_success_at,
                last_http_status=src.last_http_status,
                last_error=src.last_error,
            )
        )
    return source_briefs

def build_app_summary(session: Session, app: App) -> AppSummarySchema:
    source_briefs = get_app_source_briefs(session, app.id)
    source_ids = [s.source_id for s in source_briefs]

    aliases = session.scalars(
        select(AppAlias.alias)
        .where(AppAlias.app_id == app.id)
        .order_by(AppAlias.alias.asc())
    ).all()

    return AppSummarySchema(
        id=app.id,
        canonical_name=app.canonical_name,
        normalized_name=app.normalized_name,
        platform=app.platform,
        icon_url=app.icon_url,
        source_ids=source_ids,
        sources=source_briefs,
        aliases=list(aliases),
        created_at=app.created_at,
        updated_at=app.updated_at,
    )

def list_apps_paginated(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
) -> PaginatedResponse[AppSummarySchema]:
    total = int(session.scalar(select(func.count(App.id))) or 0)
    apps = session.scalars(
        select(App)
        .order_by(App.canonical_name.asc(), App.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [build_app_summary(session, a) for a in apps]
    return PaginatedResponse[AppSummarySchema](
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )

def get_app_detail(session: Session, app_id: int) -> Optional[AppDetailSchema]:
    app = session.get(App, app_id)
    if app is None:
        return None
    summary = build_app_summary(session, app)
    recent_changes: List[ChangeEventSchema] = []
    if summary.source_ids:
        events = session.scalars(
            select(ChangeEvent)
            .where(ChangeEvent.source_id.in_(summary.source_ids))
            .order_by(ChangeEvent.detected_at.desc(), ChangeEvent.id.desc())
            .limit(10)
        ).all()
        for ev in events:
            src = session.get(Source, ev.source_id)
            recent_changes.append(
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

    return AppDetailSchema(
        **summary.model_dump(),
        recent_changes=recent_changes,
    )