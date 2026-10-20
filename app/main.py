"""FastAPI application entry point for AppVault."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from app import __version__
from app.api.routes.admin import router as admin_router
from app.api.routes.public import router as public_router
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.security import redact_sensitive_text
from app.db.session import init_db, session_scope
from app.services.catalog import reload_seed_catalog

logger = logging.getLogger("appvault.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    cfg = settings or get_settings()
    setup_logging(
        cfg.log_level,
        secrets_to_redact=[cfg.api_admin_token, cfg.discord_bot_token],
    )

    app = FastAPI(
        title="AppVault API",
        description=(
            "Self-hosted public app/game catalog tracker, change monitor, and notification service. "
            "No credentials or account secrets are ever collected or returned."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    allowed_origins = cfg.parsed_cors_origins
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.on_event("startup")
    def _on_startup() -> None:
        cfg.validate_for_api()
        init_db()
        with session_scope() as session:
            reload_seed_catalog(session)
        logger.info("AppVault FastAPI service initialized (env=%s)", cfg.app_env)

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        safe_msg = redact_sensitive_text(str(exc.detail))
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": safe_msg,
                    "details": None,
                }
            },
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        sanitized_errors: list[dict[str, Any]] = []
        for err in exc.errors():
            sanitized_errors.append(
                {
                    "loc": list(err.get("loc", ())),
                    "msg": redact_sensitive_text(str(err.get("msg", "Invalid value"))),
                    "type": str(err.get("type", "value_error")),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request parameter validation failed.",
                    "details": {"errors": sanitized_errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled API exception: %s", redact_sensitive_text(str(exc)))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_server_error",
                    "message": "An unexpected internal error occurred.",
                    "details": None,
                }
            },
        )

    app.include_router(public_router)
    app.include_router(admin_router)
    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    settings.validate_for_api()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
