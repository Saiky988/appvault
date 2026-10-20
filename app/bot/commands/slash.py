"""Discord application commands (slash commands) for AppVault."""
from __future__ import annotations

import logging
import math
from typing import Optional

import discord
from discord import app_commands

from app.bot.embeds import (
    PaginationView,
    build_app_detail_embed,
    build_catalog_embed,
    build_check_outcome_embed,
    build_search_embed,
    build_source_detail_embed,
    build_status_embed,
    build_updates_embed,
)
from app.core.config import get_settings
from app.core.security import redact_sensitive_text
from app.db.session import check_db_health, session_scope
from app.monitoring.scheduler import (
    SourceCheckAlreadyRunningError,
    check_all_enabled_sources,
    check_single_source,
)
from app.services.catalog import list_apps_paginated, reload_seed_catalog
from app.services.search import MAX_SEARCH_RESULTS, find_best_matching_app, search_apps
from app.services.sources import (
    build_source_schema,
    compute_system_stats,
    disable_discord_subscription,
    get_or_create_worker_state,
    get_source_by_id_or_key,
    list_source_apps_paginated,
    list_updates_paginated,
    set_worker_paused,
    upsert_discord_subscription,
)

logger = logging.getLogger("appvault.bot.commands")


def is_authorized_admin(interaction: discord.Interaction) -> bool:
    """Return True if the interaction user is in DISCORD_ADMIN_USER_IDS."""
    settings = get_settings()
    admin_ids = settings.parsed_admin_user_ids
    user_id = getattr(interaction.user, "id", None)
    return user_id is not None and int(user_id) in admin_ids


def has_channel_subscription_permission(interaction: discord.Interaction) -> bool:
    """Return True if user is a configured admin or has Manage Guild / Manage Channels permission."""
    if is_authorized_admin(interaction):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "manage_channels", False)
    )


def register_slash_commands(tree: app_commands.CommandTree, client: discord.Client) -> None:
    """Register all AppVault slash commands and /admin command group onto the CommandTree."""

    @tree.command(name="search", description="Search across the app catalog by name, alias, or fuzzy match")
    @app_commands.describe(query="Application or game name to search for")
    async def cmd_search(interaction: discord.Interaction, query: str) -> None:
        per_page = 5
        with session_scope() as session:
            search_resp = search_apps(session, query, limit=MAX_SEARCH_RESULTS, offset=0)

        total_pages = max(1, math.ceil(len(search_resp.items) / per_page))
        embed = build_search_embed(search_resp, page=1, per_page=per_page)

        if total_pages > 1:
            view = PaginationView(
                total_pages=total_pages,
                page_builder=lambda p: build_search_embed(search_resp, page=p, per_page=per_page),
                author_id=interaction.user.id,
            )
            await interaction.response.send_message(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @tree.command(name="app", description="Show details, sources, and change history for one matching app")
    @app_commands.describe(name="Name or alias of the application")
    async def cmd_app(interaction: discord.Interaction, name: str) -> None:
        with session_scope() as session:
            detail = find_best_matching_app(session, name)

        if detail is None:
            await interaction.response.send_message(
                f"No matching application found for `{redact_sensitive_text(name)[:60]}`.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        embed = build_app_detail_embed(detail)
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="catalog", description="Browse all apps in a specific source or across the entire catalog")
    @app_commands.describe(source_id="Optional Source ID (e.g. 1, 5, 15, 19, 20)")
    async def cmd_catalog(
        interaction: discord.Interaction,
        source_id: Optional[int] = None,
    ) -> None:
        per_page = 8
        with session_scope() as session:
            if source_id is not None:
                src_obj = get_source_by_id_or_key(session, source_id)
                if src_obj is None:
                    await interaction.response.send_message(
                        f"Source `#{source_id}` was not found.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                src_schema = build_source_schema(session, src_obj)
                src_apps_resp = list_source_apps_paginated(session, src_obj.id, limit=200, offset=0)
                src_apps = src_apps_resp.items if src_apps_resp else []
                total_pages = max(1, math.ceil(len(src_apps) / per_page))
                embed = build_catalog_embed(
                    source=src_schema,
                    source_apps=src_apps,
                    all_apps=None,
                    page=1,
                    per_page=per_page,
                )
                page_builder = lambda p: build_catalog_embed(  # noqa: E731
                    source=src_schema,
                    source_apps=src_apps,
                    all_apps=None,
                    page=p,
                    per_page=per_page,
                )
            else:
                all_apps_resp = list_apps_paginated(session, limit=200, offset=0)
                all_apps = all_apps_resp.items
                total_pages = max(1, math.ceil(len(all_apps) / per_page))
                embed = build_catalog_embed(
                    source=None,
                    source_apps=None,
                    all_apps=all_apps,
                    page=1,
                    per_page=per_page,
                )
                page_builder = lambda p: build_catalog_embed(  # noqa: E731
                    source=None,
                    source_apps=None,
                    all_apps=all_apps,
                    page=p,
                    per_page=per_page,
                )

        if total_pages > 1:
            view = PaginationView(
                total_pages=total_pages,
                page_builder=page_builder,
                author_id=interaction.user.id,
            )
            await interaction.response.send_message(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @tree.command(name="source", description="Show source details, monitoring health, interval, and URL")
    @app_commands.describe(id="Source ID (e.g. 1, 5, 15, 19, 20)")
    async def cmd_source(interaction: discord.Interaction, id: int) -> None:
        with session_scope() as session:
            src_obj = get_source_by_id_or_key(session, id)
            if src_obj is None:
                await interaction.response.send_message(
                    f"Source `#{id}` was not found.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            src_schema = build_source_schema(session, src_obj)

        embed = build_source_detail_embed(src_schema)
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="updates", description="Show recent detected source changes with timestamps and links")
    @app_commands.describe(limit="Maximum number of recent updates to display (1-20)")
    async def cmd_updates(
        interaction: discord.Interaction,
        limit: int = 5,
    ) -> None:
        safe_limit = max(1, min(int(limit), 20))
        with session_scope() as session:
            updates_resp = list_updates_paginated(session, limit=safe_limit, offset=0)

        embed = build_updates_embed(updates_resp.items, safe_limit)
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="subscribe", description="Subscribe a channel to source change notifications")
    @app_commands.describe(channel="Target Discord text channel for notifications")
    async def cmd_subscribe(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not has_channel_subscription_permission(interaction):
            await interaction.response.send_message(
                "You need `Manage Channels`, `Manage Server`, or bot admin privileges to manage subscriptions.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Subscriptions can only be configured inside a Discord server.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        with session_scope() as session:
            sub, created = upsert_discord_subscription(
                session,
                guild_id=str(interaction.guild_id),
                channel_id=str(channel.id),
            )

        action_word = "Subscribed" if created else "Updated subscription for"
        await interaction.response.send_message(
            f"✅ {action_word} <#{sub.channel_id}> to AppVault change notifications.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="unsubscribe", description="Disable change notifications for the current channel")
    async def cmd_unsubscribe(interaction: discord.Interaction) -> None:
        if not has_channel_subscription_permission(interaction):
            await interaction.response.send_message(
                "You need `Manage Channels`, `Manage Server`, or bot admin privileges to unsubscribe.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        channel_id = str(interaction.channel_id)
        with session_scope() as session:
            disabled = disable_discord_subscription(session, channel_id)

        if disabled:
            await interaction.response.send_message(
                f"🔕 Disabled AppVault notifications for <#{channel_id}>.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                f"Channel <#{channel_id}> does not have an active subscription.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @tree.command(name="status", description="Show bot latency, database health, worker status, and source counts")
    async def cmd_status(interaction: discord.Interaction) -> None:
        with session_scope() as session:
            db_ok = check_db_health(session)
            stats = compute_system_stats(session)
            w_state = get_or_create_worker_state(session)
            heartbeat = w_state.last_heartbeat_at

        latency_ms = max(0.0, float(getattr(client, "latency", 0.0)) * 1000.0)
        embed = build_status_embed(
            stats,
            latency_ms=latency_ms,
            db_healthy=db_ok,
            worker_last_heartbeat=heartbeat,
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tree.command(name="check", description="Request an immediate manual check for a source (Admins only)")
    @app_commands.describe(source_id="Source ID to check (e.g. 1, 5, 15, 19, 20)")
    async def cmd_check(interaction: discord.Interaction, source_id: int) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "🚫 You are not authorized to run manual source checks.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        with session_scope() as session:
            src = get_source_by_id_or_key(session, source_id)
            if src is None:
                await interaction.response.send_message(
                    f"Source `#{source_id}` was not found.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            target_id = src.id

        await interaction.response.defer(thinking=True)
        try:
            outcome = await check_single_source(
                target_id,
                worker_id=f"discord-admin-{interaction.user.id}",
            )
        except SourceCheckAlreadyRunningError as exc:
            await interaction.followup.send(
                f"⏳ {redact_sensitive_text(str(exc))}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        embed = build_check_outcome_embed(outcome)
        await interaction.followup.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    admin_group = app_commands.Group(
        name="admin",
        description="Administrative controls for AppVault polling and seed catalog",
    )

    @admin_group.command(name="pause", description="Pause background polling across all sources")
    async def cmd_admin_pause(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "🚫 Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            state = set_worker_paused(session, True)
        await interaction.response.send_message(
            f"⏸️ Background monitoring worker paused (updated at `{state.updated_at.strftime('%H:%M:%S UTC')}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="resume", description="Resume background polling across all sources")
    async def cmd_admin_resume(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "🚫 Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            state = set_worker_paused(session, False)
        await interaction.response.send_message(
            f"▶️ Background monitoring worker resumed (updated at `{state.updated_at.strftime('%H:%M:%S UTC')}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="check-all", description="Check all enabled sources subject to concurrency limits")
    async def cmd_admin_check_all(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "🚫 Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(thinking=True)
        outcomes = await check_all_enabled_sources(
            worker_id=f"discord-admin-bulk-{interaction.user.id}",
            only_due=False,
        )
        lines = [
            f"• **Source #{o.source_id}**: `{o.status}` (HTTP `{o.http_status or 'N/A'}` | Apps: `{o.extracted_app_count}`)"
            + (f" — ⚠️ `{redact_sensitive_text(o.error)[:60]}`" if o.error else "")
            for o in outcomes
        ]
        summary_text = "\n".join(lines) if lines else "No enabled sources were checked."
        await interaction.followup.send(
            f"✅ Completed check across **{len(outcomes)}** source(s):\n{summary_text}"[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="reload-catalog", description="Idempotently reload seed catalog records")
    async def cmd_admin_reload_catalog(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "🚫 Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            res = reload_seed_catalog(session)
        await interaction.response.send_message(
            f"🔄 Reloaded seed catalog `v{res.version}`:\n"
            f"• Sources: `{res.sources_created}` created, `{res.sources_updated}` updated\n"
            f"• Apps: `{res.apps_created}` created, `{res.apps_existing}` existing\n"
            f"• Source Links: `{res.links_created}` created, `{res.links_updated}` updated\n"
            f"• Aliases added: `{res.aliases_created}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    tree.add_command(admin_group)
