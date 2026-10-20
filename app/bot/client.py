"""Discord bot client entry point with controlled command synchronization and graceful shutdown."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.bot.commands.slash import register_slash_commands  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.core.security import redact_sensitive_text  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.services.catalog import reload_seed_catalog  # noqa: E402
from app.services.sources import get_or_create_worker_state  # noqa: E402

logger = logging.getLogger("appvault.bot")


class AppVaultBot(discord.Client):
    """Discord bot client using discord.py 2.x application commands."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        force_sync: bool = False,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings or get_settings()
        self.force_sync = force_sync
        self.tree = app_commands.CommandTree(self)
        register_slash_commands(self.tree, self)
        self.tree.error(self._on_app_command_error)

    def compute_command_signature_hash(self) -> str:
        """Compute deterministic hash of registered slash command names and descriptions."""
        cmds = sorted(
            (c.name, getattr(c, "description", ""))
            for c in self.tree.get_commands()
        )
        payload = json.dumps(cmds, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def setup_hook(self) -> None:
        """Synchronize slash commands when the command tree hash changes or when forced."""
        sig_hash = self.compute_command_signature_hash()
        should_sync = self.force_sync or self.settings.sync_commands_on_startup

        with session_scope() as session:
            state = get_or_create_worker_state(session)
            if state.last_command_sync_hash != sig_hash:
                should_sync = True

        if not should_sync:
            logger.info(
                "Slash command definitions unchanged (hash=%s); skipping redundant startup sync.",
                sig_hash[:12],
            )
            return

        try:
            if self.settings.discord_guild_id:
                guild_obj = discord.Object(id=int(self.settings.discord_guild_id))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                logger.info(
                    "Synchronized %d slash command(s) to development guild %s",
                    len(synced),
                    self.settings.discord_guild_id,
                )
            else:
                synced = await self.tree.sync()
                logger.info("Synchronized %d global slash command(s)", len(synced))

            with session_scope() as session:
                state = get_or_create_worker_state(session)
                state.last_command_sync_hash = sig_hash
        except discord.RateLimited as exc:
            logger.warning("Discord rate limited command sync; retry after %.1fs", exc.retry_after)
        except discord.HTTPException as exc:
            logger.error("Failed to synchronize Discord commands: status=%s", getattr(exc, "status", "unknown"))

    async def on_ready(self) -> None:
        user_str = str(self.user) if self.user else "AppVaultBot"
        logger.info("AppVault Discord bot connected as %s", user_str)

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        safe_err = redact_sensitive_text(str(error))
        logger.error(
            "Slash command failure (user=%s, channel=%s): %s",
            getattr(interaction.user, "id", "unknown"),
            getattr(interaction, "channel_id", "unknown"),
            safe_err,
        )
        msg = "An error occurred while processing the command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    msg,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    msg,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception:
            pass


async def _async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AppVault Discord Bot")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Force slash command synchronization on startup",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(
        settings.log_level,
        secrets_to_redact=[settings.api_admin_token, settings.discord_bot_token],
    )
    settings.validate_for_bot()

    init_db()
    with session_scope() as session:
        reload_seed_catalog(session)

    bot = AppVaultBot(settings=settings, force_sync=args.sync)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:
            pass

    try:
        await bot.start(settings.discord_bot_token, reconnect=True)
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
