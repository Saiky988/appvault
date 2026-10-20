"""Pydantic v2 response and request schemas for AppVault REST API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class PaginatedResponse(BaseModel, Generic[T]):
    total: int = Field(..., ge=0, description="Total matching records before pagination")
    limit: int = Field(..., ge=1, description="Maximum items returned per page")
    offset: int = Field(..., ge=0, description="Zero-based item offset")
    items: List[T]


class SourceBriefSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_id: int
    source_key: str
    url: str
    enabled: bool
    health_status: str
    original_title: str
    display_order: int = 0
    data_origin: str
    last_checked_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_http_status: Optional[int] = None
    last_error: Optional[str] = None


class AppSummarySchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    canonical_name: str
    normalized_name: str
    platform: Optional[str] = "iOS"
    source_ids: List[int] = Field(default_factory=list)
    sources: List[SourceBriefSchema] = Field(default_factory=list)
    aliases: List[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ChangeEventSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    source_key: Optional[str] = None
    source_url: Optional[str] = None
    event_type: str
    previous_hash: Optional[str] = None
    current_hash: str
    summary: str
    detected_at: datetime
    notification_status: str
    notification_attempts: int = 0


class AppDetailSchema(AppSummarySchema):
    recent_changes: List[ChangeEventSchema] = Field(default_factory=list)


class AppSearchResultItemSchema(BaseModel):
    app: AppSummarySchema
    match_type: str = Field(..., description="exact, case_insensitive, alias, partial, or fuzzy")
    score: float = Field(..., ge=0.0, le=1.0)
    matched_on: str


class AppSearchResponseSchema(BaseModel):
    query: str
    normalized_query: str
    total: int
    limit: int
    offset: int
    items: List[AppSearchResultItemSchema]


class SourceSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_key: str
    url: str
    enabled: bool
    health_status: str
    check_interval_seconds: int
    has_custom_interval: bool = False
    app_count: int = 0
    seed_app_count: int = 0
    observed_app_count: int = 0
    last_checked_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_http_status: Optional[int] = None
    last_error: Optional[str] = None
    current_content_hash: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SourceAppItemSchema(BaseModel):
    app_id: int
    canonical_name: str
    normalized_name: str
    original_title: str
    display_order: int
    data_origin: str
    is_active: bool
    first_seen_at: datetime
    last_seen_at: datetime


class HealthResponseSchema(BaseModel):
    status: str
    database: str
    worker_paused: bool
    worker_last_heartbeat_at: Optional[datetime] = None
    version: str
    timestamp: datetime


class StatsResponseSchema(BaseModel):
    total_apps: int
    total_sources: int
    enabled_sources: int
    healthy_sources: int
    seed_only_sources: int
    error_sources: int
    total_source_app_links: int
    total_aliases: int
    total_change_events: int
    pending_notifications: int
    active_subscriptions: int
    worker_paused: bool
    last_successful_check_at: Optional[datetime] = None


class SourceCheckOutcomeSchema(BaseModel):
    source_id: int
    source_key: str
    url: str
    status: str
    http_status: Optional[int] = None
    changed: bool = False
    change_event_id: Optional[int] = None
    extracted_app_count: int = 0
    data_origin: str = "seed"
    error: Optional[str] = None
    checked_at: datetime


class AdminBulkCheckResponseSchema(BaseModel):
    checked_count: int
    results: List[SourceCheckOutcomeSchema]


class WorkerStateResponseSchema(BaseModel):
    is_paused: bool
    active_worker_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    updated_at: datetime


class CatalogReloadResponseSchema(BaseModel):
    version: str
    sources_created: int
    sources_updated: int
    apps_created: int
    apps_existing: int
    links_created: int
    links_updated: int
    aliases_created: int
