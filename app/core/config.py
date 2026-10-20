"""Application configuration managed via Pydantic v2 and pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration for API, Discord bot, and monitoring worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="production", description="Runtime environment: development, testing, staging, production")
    log_level: str = Field(default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)")

    # Database
    database_url: str = Field(
        default="sqlite:///./data/appvault.db",
        description="SQLAlchemy database connection URL (SQLite or PostgreSQL)",
    )

    # FastAPI Server
    api_host: str = Field(default="127.0.0.1", description="Bind address for FastAPI server (localhost recommended for cloudflared)")
    api_port: int = Field(default=8000, description="Bind port for FastAPI server")
    api_admin_token: str = Field(
        default="change-me-in-production-min-24-chars-token",
        description="High-entropy Bearer token protecting /api/v1/admin/* endpoints",
    )
    cors_origins: str = Field(
        default="",
        description="Comma-separated list of allowed CORS origins (never use '*' with credentials)",
    )
    rate_limit_per_minute: int = Field(
        default=120,
        description="Maximum API requests per minute per client IP",
    )

    # Discord Bot
    discord_bot_token: str = Field(default="", description="Discord Bot Token from Discord Developer Portal")
    discord_guild_id: Optional[int] = Field(default=None, description="Optional development guild ID for instant slash-command sync")
    discord_admin_user_ids: str = Field(
        default="",
        description="Comma-separated Discord user IDs authorized for /check and /admin commands",
    )
    sync_commands_on_startup: bool = Field(
        default=False,
        description="Synchronize slash commands on bot startup (recommended False in prod after initial sync)",
    )

    # Background Monitoring Worker
    monitor_interval_seconds: int = Field(
        default=300,
        description="Default polling interval in seconds (default 300s = 5 minutes)",
    )
    monitor_concurrency: int = Field(
        default=3,
        description="Maximum concurrent source checks",
    )
    http_timeout_seconds: float = Field(
        default=15.0,
        description="HTTP connect and read timeout in seconds",
    )
    max_response_bytes: int = Field(
        default=2_097_152,
        description="Maximum allowed HTTP response body size in bytes (default 2 MiB)",
    )
    max_retries: int = Field(
        default=3,
        description="Maximum HTTP retry attempts for transient failures",
    )
    user_agent: str = Field(
        default="AppVaultMonitor/1.0 (+https://github.com/selfhosted/appvault; public-catalog-tracker)",
        description="Descriptive HTTP User-Agent header for monitoring requests",
    )
    respect_robots_txt: bool = Field(
        default=True,
        description="Check and respect robots.txt rules when available",
    )
    lease_ttl_seconds: int = Field(
        default=120,
        description="Database lease duration in seconds to prevent concurrent duplicate checks",
    )
    notification_max_attempts: int = Field(
        default=5,
        description="Maximum retry attempts for failed Discord notification deliveries",
    )

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, value: str) -> str:
        normalized = (value or "production").strip().lower()
        allowed = {"development", "testing", "staging", "production"}
        if normalized not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = (value or "INFO").strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized

    @field_validator("api_port")
    @classmethod
    def validate_api_port(cls, value: int) -> int:
        val = int(value)
        if not (1 <= val <= 65535):
            raise ValueError("API_PORT must be between 1 and 65535")
        return val

    @field_validator("monitor_interval_seconds")
    @classmethod
    def validate_monitor_interval(cls, value: int) -> int:
        val = int(value)
        if val < 15:
            raise ValueError("MONITOR_INTERVAL_SECONDS must be at least 15 seconds")
        return val

    @field_validator("monitor_concurrency")
    @classmethod
    def validate_monitor_concurrency(cls, value: int) -> int:
        val = int(value)
        if not (1 <= val <= 25):
            raise ValueError("MONITOR_CONCURRENCY must be between 1 and 25")
        return val

    @field_validator("http_timeout_seconds")
    @classmethod
    def validate_http_timeout(cls, value: float) -> float:
        val = float(value)
        if not (1.0 <= val <= 120.0):
            raise ValueError("HTTP_TIMEOUT_SECONDS must be between 1.0 and 120.0")
        return val

    @field_validator("max_response_bytes")
    @classmethod
    def validate_max_response_bytes(cls, value: int) -> int:
        val = int(value)
        if not (4096 <= val <= 20_971_520):
            raise ValueError("MAX_RESPONSE_BYTES must be between 4 KiB and 20 MiB")
        return val

    @field_validator("max_retries")
    @classmethod
    def validate_max_retries(cls, value: int) -> int:
        val = int(value)
        if not (0 <= val <= 10):
            raise ValueError("MAX_RETRIES must be between 0 and 10")
        return val

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("DATABASE_URL cannot be empty")
        if not (
            cleaned.startswith("sqlite:///")
            or cleaned.startswith("sqlite://")
            or cleaned.startswith("postgresql://")
            or cleaned.startswith("postgresql+psycopg://")
            or cleaned.startswith("postgresql+asyncpg://")
        ):
            raise ValueError("DATABASE_URL must use a supported sqlite or postgresql scheme")
        return cleaned

    @property
    def parsed_cors_origins(self) -> List[str]:
        """Return safe list of explicit CORS origins (excluding bare wildcard '*')."""
        if not self.cors_origins:
            return []
        origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return [o for o in origins if o != "*"]

    @property
    def parsed_admin_user_ids(self) -> set[int]:
        """Return set of configured Discord administrator user IDs."""
        if not self.discord_admin_user_ids:
            return set()
        result: set[int] = set()
        for raw in str(self.discord_admin_user_ids).split(","):
            part = raw.strip()
            if part.isdigit():
                result.add(int(part))
        return result

    def ensure_sqlite_directory(self) -> None:
        """Create parent directory for file-backed SQLite database if needed."""
        if self.database_url.startswith("sqlite:///"):
            raw_path = self.database_url.replace("sqlite:///", "", 1).split("?", 1)[0]
            if raw_path and raw_path != ":memory:":
                db_path = Path(raw_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_for_api(self) -> None:
        """Validate required settings before launching the API server."""
        token = (self.api_admin_token or "").strip()
        if self.app_env == "production" and (
            len(token) < 16 or token == "change-me-in-production-min-24-chars-token"
        ):
            raise RuntimeError(
                "API_ADMIN_TOKEN must be configured to a strong secret (>= 16 characters) in production."
            )

    def validate_for_bot(self) -> None:
        """Validate required settings before launching the Discord bot."""
        if not (self.discord_bot_token or "").strip():
            raise RuntimeError(
                "DISCORD_BOT_TOKEN is required to start the Discord bot. Set it in .env or environment."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
