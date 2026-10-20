"""FastAPI dependencies: Bearer token authentication, database session, and rate limiting."""
from __future__ import annotations

from collections import deque
import threading
import time
from typing import Deque, Dict, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.security import verify_admin_token
from app.db.session import get_db_session

bearer_scheme = HTTPBearer(auto_error=False, description="High-entropy Bearer token for admin endpoints")


class SlidingWindowRateLimiter:
    """Thread-safe per-IP sliding window rate limiter."""

    def __init__(self, max_requests_per_minute: int = 120) -> None:
        self.max_requests = max_requests_per_minute
        self.window_seconds = 60.0
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow_request(self, client_key: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            dq = self._hits.setdefault(client_key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False
            dq.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter()


def enforce_rate_limit(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce per-IP request rate limit."""
    rate_limiter.max_requests = settings.rate_limit_per_minute
    client_host = request.client.host if request.client else "127.0.0.1"
    if not rate_limiter.allow_request(client_host):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down requests.",
        )


def require_admin_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """Validate Bearer token against API_ADMIN_TOKEN in constant time."""
    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_admin_token(credentials.credentials, settings.api_admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid administrator token.",
        )


__all__ = [
    "SlidingWindowRateLimiter",
    "enforce_rate_limit",
    "get_db_session",
    "rate_limiter",
    "require_admin_token",
]
