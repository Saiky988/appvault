"""Idempotent seed catalog CLI script for AppVault."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.services.catalog import reload_seed_catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed or reload AppVault initial catalog idempotently.")
    parser.add_argument(
        "--check-after-seed",
        action="store_true",
        help="Attempt a live HTTP check on seeded sources after loading seed data.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    logger = setup_logging(
        settings.log_level,
        secrets_to_redact=[settings.api_admin_token, settings.discord_bot_token],
    )

    init_db()
    with session_scope() as session:
        result = reload_seed_catalog(session)
        logger.info(
            "Seed catalog v%s loaded: sources(created=%d, updated=%d), apps(created=%d, existing=%d), "
            "links(created=%d, updated=%d), aliases(created=%d)",
            result.version,
            result.sources_created,
            result.sources_updated,
            result.apps_created,
            result.apps_existing,
            result.links_created,
            result.links_updated,
            result.aliases_created,
        )

    if args.check_after_seed:
        import asyncio
        from app.monitoring.scheduler import check_all_enabled_sources

        outcomes = asyncio.run(check_all_enabled_sources(worker_id="seed-cli"))
        for outcome in outcomes:
            logger.info(
                "Source #%d (%s): status=%s http=%s apps=%d error=%s",
                outcome.source_id,
                outcome.url,
                outcome.status,
                outcome.http_status,
                outcome.extracted_app_count,
                outcome.error,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
