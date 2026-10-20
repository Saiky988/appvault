"""Public read-only REST API endpoints (/api/v1)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import __version__
from app.api.dependencies import enforce_rate_limit, get_db_session
from app.schemas.api import (
    AppDetailSchema,
    AppSearchResponseSchema,
    AppSummarySchema,
    ChangeEventSchema,
    HealthResponseSchema,
    PaginatedResponse,
    SourceAppItemSchema,
    SourceSchema,
    StatsResponseSchema,
)
from app.services.catalog import get_app_detail, list_apps_paginated
from app.services.search import search_apps
from app.services.sources import (
    build_source_schema,
     compute_system_stats,
    get_or_create_worker_state,
    get_source_by_id_or_key,
    get_update_by_id,
    list_source_apps_paginated,
    list_sources_paginated,
    list_updates_paginated,
)
from app.db.session import check_db_health

router = APIRouter(prefix="/api/v1", tags=["Public Catalog API"], dependencies=[Depends(enforce_rate_limit)])


@router.get(
    "/health",
    response_model=HealthResponseSchema,
    summary="Check service and database health",
)
def get_health(session: Session = Depends(get_db_session)) -> HealthResponseSchema:
    db_ok = check_db_health(session)
    worker_state = get_or_create_worker_state(session)
    return HealthResponseSchema(
        status="ok" if db_ok else "degraded",
        database="connected" if db_ok else "unreachable",
        worker_paused=worker_state.is_paused,
        worker_last_heartbeat_at=worker_state.last_heartbeat_at,
        version=__version__,
        timestamp=datetime.now(timezone.utc),
    )


@router.get(
    "/stats",
    response_model=StatsResponseSchema,
    summary="Get catalog and monitoring statistics",
)
def get_stats(session: Session = Depends(get_db_session)) -> StatsResponseSchema:
    return compute_system_stats(session)


@router.get(
    "/apps",
    response_model=PaginatedResponse[AppSummarySchema],
    summary="List canonical applications with pagination",
)
def get_apps(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page (1-100)"),
    offset: int = Query(default=0, ge=0, description="Zero-based record offset"),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[AppSummarySchema]:
    return list_apps_paginated(session, limit=limit, offset=offset)


@router.get(
    "/apps/search",
    response_model=AppSearchResponseSchema,
    summary="Search applications across names, aliases, and fuzzy matches",
)
def search_apps_endpoint(
    q: str = Query(..., min_length=1, max_length=120, description="Search query text"),
    limit: int = Query(default=20, ge=1, le=50, description="Maximum results to return per page (1-50)"),
    offset: int = Query(default=0, ge=0, description="Zero-based offset"),
    session: Session = Depends(get_db_session),
) -> AppSearchResponseSchema:
    return search_apps(session, q, limit=limit, offset=offset)


@router.get(
    "/apps/{app_id}",
    response_model=AppDetailSchema,
    summary="Get detailed information for a specific application",
)
def get_app_by_id(
    app_id: int,
    session: Session = Depends(get_db_session),
) -> AppDetailSchema:
    detail = get_app_detail(session, app_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Application with id={app_id} not found.",
        )
    return detail


@router.get(
    "/sources",
    response_model=PaginatedResponse[SourceSchema],
    summary="List monitored sources with health status",
)
def get_sources(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page (1-100)"),
    offset: int = Query(default=0, ge=0, description="Zero-based offset"),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[SourceSchema]:
    return list_sources_paginated(session, limit=limit, offset=offset)


@router.get(
    "/sources/{source_id}",
    response_model=SourceSchema,
    summary="Get details and monitoring status for a source",
)
def get_source_detail(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourceSchema:
    source = get_source_by_id_or_key(session, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source with id={source_id} not found.",
        )
    return build_source_schema(session, source)


@router.get(
    "/sources/{source_id}/apps",
    response_model=PaginatedResponse[SourceAppItemSchema],
    summary="List applications associated with a specific source",
)
def get_source_apps(
    source_id: int,
    limit: int = Query(default=50, ge=1, le=100, description="Items per page (1-100)"),
    offset: int = Query(default=0, ge=0, description="Zero-based offset"),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[SourceAppItemSchema]:
    result = list_source_apps_paginated(session, source_id, limit=limit, offset=offset)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source with id={source_id} not found.",
        )
    return result


@router.get(
    "/updates",
    response_model=PaginatedResponse[ChangeEventSchema],
    summary="List recent detected source changes",
)
def get_updates(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page (1-100)"),
    offset: int = Query(default=0, ge=0, description="Zero-based offset"),
    source_id: Optional[int] = Query(default=None, description="Optional filter by source ID"),
    session: Session = Depends(get_db_session),
) -> PaginatedResponse[ChangeEventSchema]:
    return list_updates_paginated(session, limit=limit, offset=offset, source_id=source_id)


@router.get(
    "/updates/{event_id}",
    response_model=ChangeEventSchema,
    summary="Get details for a specific change event",
)
def get_update_detail(
    event_id: int,
    session: Session = Depends(get_db_session),
) -> ChangeEventSchema:
    event = get_update_by_id(session, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Change event with id={event_id} not found.",
        )
    return event
