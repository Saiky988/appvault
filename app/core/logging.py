"""Structured logging configuration with automatic secret redaction."""
from __future__ import annotations

import logging
import sys
from typing import Optional

from app.core.security import redact_sensitive_text


class SecretRedactingFilter(logging.Filter):
    """Ensure credentials, tokens, and email addresses are never written to logs."""

    def __init__(self, extra_secrets: Optional[list[str]] = None) -> None:
        super().__init__()
        self._secrets = [s for s in (extra_secrets or []) if s and len(s) >= 6]

    def _scrub(self, value: str) -> str:
        scrubbed = redact_sensitive_text(value)
        for secret in self._secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[REDACTED_SECRET]")
        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._scrub(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
        return True


def setup_logging(
    level: str = "INFO",
    secrets_to_redact: Optional[list[str]] = None,
) -> logging.Logger:
    """Configure root and appvault loggers with secret redaction."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    redactor = SecretRedactingFilter(extra_secrets=secrets_to_redact)

    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
            handler.addFilter(redactor)

    # Silence noisy third-party loggers that might log request URLs or headers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    logger = logging.getLogger("appvault")
    logger.setLevel(numeric_level)
    return logger
