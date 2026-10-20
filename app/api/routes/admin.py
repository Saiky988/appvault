"""Authenticated administrative REST API endpoints (/api/v1/admin)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import enforce_rate_limit, get_db_session, require_admin_token
from app.monitoring.scheduler import (
    SourceCheckAlreadyRunningError,
    check_all_enabled_sources,
    check_single_source,
)
from app.schemas.api import (
    AdminBulkCheckResponseSchema,
    CatalogReloadResponseSchema,
    SourceCheckOutcomeSchema,
    WorkerStateResponseSchema,
)
from app.services.catalog import reload_seed_catalog
from app.services.sources import get_source_by_id_or_key, set_worker_paused

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["Admin API"],
    dependencies=[Depends(enforce_rate_limit), Depends(require_admin_token)],
)


@router.post(
    "/sources/{source_id}/check",
    response_model=SourceCheckOutcomeSchema,
    summary="Trigger an immediate manual check for a specific source",
)
async def admin_check_source(
    source_id: int,
    session: Session = Depends(get_db_session),
) -> SourceCheckOutcomeSchema:
    source = get_source_by_id_or_key(session, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source with id={source_id} not found.",
        )
    try:
        return await check_single_source(source.id, worker_id="api-admin")
    except SourceCheckAlreadyRunningError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/check-all",
    response_model=AdminBulkCheckResponseSchema,
    summary="Trigger checks for all enabled sources subject to concurrency limits",
)
async def admin_check_all_sources() -> AdminBulkCheckResponseSchema:
    outcomes = await check_all_enabled_sources(worker_id="api-admin-bulk", only_due=False)
    return AdminBulkCheckResponseSchema(
        checked_count=len(outcomes),
        results=outcomes,
    )


@router.post(
    "/pause",
    response_model=WorkerStateResponseSchema,
    summary="Pause background source polling globally",
)
def admin_pause_worker(session: Session = Depends(get_db_session)) -> WorkerStateResponseSchema:
    return set_worker_paused(session, True)


@router.post(
    "/resume",
    response_model=WorkerStateResponseSchema,
    summary="Resume background source polling globally",
)
def admin_resume_worker(session: Session = Depends(get_db_session)) -> WorkerStateResponseSchema:
    return set_worker_paused(session, False)


@router.post(
    "/catalog/reload",
    response_model=CatalogReloadResponseSchema,
    summary="Idempotently reload initial seed catalog without deleting history or subscriptions",
)
def admin_reload_catalog(session: Session = Depends(get_db_session)) -> CatalogReloadResponseSchema:
    return reload_seed_catalog(session)
