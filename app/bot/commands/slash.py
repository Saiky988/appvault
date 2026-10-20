"""Discord application commands (slash commands) with game/ID autocomplete for AppVault."""
from __future__ import annotations

import logging
import math
import random
import re
from typing import List, Optional

import discord
from discord import app_commands
from sqlalchemy import select

from app.bot.embeds import (
    PaginationView,
    build_app_detail_embed,
    build_catalog_embed,
    build_check_outcome_embed,
    build_search_embed,
    build_source_apps_embed,
    build_source_detail_embed,
    build_status_embed,
    build_updates_embed,
)
from app.core.config import get_settings
from app.core.security import redact_sensitive_text
from app.db.models import App, Source
from app.db.session import check_db_health, session_scope
from app.monitoring.scheduler import (
    SourceCheckAlreadyRunningError,
    check_all_enabled_sources,
    check_single_source,
)
from app.schemas.api import SourceBriefSchema
from app.services.catalog import list_apps_paginated, reload_seed_catalog
from app.services.search import MAX_SEARCH_RESULTS, find_best_matching_app, search_apps
from app.services.sources import (
    build_source_schema,
    compute_system_stats,
    disable_discord_subscription,
    get_or_create_worker_state,
    get_source_by_id_or_key,
    list_source_apps_paginated,
    list_sources_paginated,
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


def get_game_autocomplete_choices(current: str, *, include_all_option: bool = False) -> List[app_commands.Choice[str]]:
    """Return up to 25 game choices: shows featured catalog games when empty, or live fuzzy search matches as user types."""
    query = (current or "").strip()
    choices: List[app_commands.Choice[str]] = []

    if include_all_option and (not query or "all".startswith(query.lower())):
        choices.append(app_commands.Choice(name="All Games (Tat ca game)", value="all"))

    limit = 25 - len(choices)
    with session_scope() as session:
        if not query:
            apps = session.scalars(
                select(App).order_by(App.id.asc()).limit(limit)
            ).all()
            for a in apps:
                choices.append(
                    app_commands.Choice(
                        name=a.canonical_name[:100],
                        value=a.canonical_name[:100],
                    )
                )
        else:
            search_resp = search_apps(session, query, limit=limit, offset=0)
            for item in search_resp.items[:limit]:
                app = item.app
                sids = ", ".join(f"#{sid}" for sid in app.source_ids)
                label = f"{app.canonical_name} (ID: {sids})" if sids else app.canonical_name
                choices.append(
                    app_commands.Choice(
                        name=label[:100],
                        value=app.canonical_name[:100],
                    )
                )
    return choices[:25]


def get_source_id_autocomplete_choices(
    game_name: Optional[str],
    current: str,
) -> List[app_commands.Choice[str]]:
    """Return choices for `/app id:`: Random + matching IDs for the selected game (or all IDs)."""
    q = (current or "").strip().lower()
    choices: List[app_commands.Choice[str]] = []

    with session_scope() as session:
        matching_ids: List[int] = []
        if game_name and game_name.strip():
            detail = find_best_matching_app(session, game_name.strip())
            if detail and detail.source_ids:
                matching_ids = list(detail.source_ids)

        if not matching_ids:
            all_srcs = session.scalars(select(Source).order_by(Source.id.asc())).all()
            matching_ids = [s.id for s in all_srcs]

    random_label = (
        f"Random (Ngau nhien trong ID: {', '.join(str(i) for i in matching_ids)})"
        if matching_ids
        else "Random"
    )
    if not q or "random".startswith(q):
        choices.append(app_commands.Choice(name=random_label[:100], value="Random"))

    for sid in matching_ids:
        sid_str = str(sid)
        if not q or q in sid_str or f"id {sid_str}".startswith(q):
            choices.append(app_commands.Choice(name=f"ID {sid}", value=sid_str))

    return choices[:25]


def _select_source_for_app(
    sources: List[SourceBriefSchema],
    id_option: Optional[str],
) -> tuple[Optional[SourceBriefSchema], str]:
    """Select a SourceBriefSchema based on id_option ('Random', None, or a specific ID like '19')."""
    if not sources:
        return None, "none"

    cleaned_opt = (id_option or "").strip().lower()
    if not cleaned_opt or cleaned_opt == "random":
        # Prefer sources that already have account credentials extracted
        with_creds = [s for s in sources if s.account_email and s.account_password]
        pool = with_creds if with_creds else sources
        return random.choice(pool), "random"  # noqa: S311

    # Extract digits if user passed 'ID 19' or '#19' or '19'
    m = re.search(r"\d+", cleaned_opt)
    target_id = int(m.group(0)) if m else None
    if target_id is not None:
        for s in sources:
            if s.source_id == target_id:
                return s, f"id:{target_id}"

    return None, f"invalid:{id_option}"


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

    @cmd_search.autocomplete("query")
    async def _search_query_autocomplete(
        _interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return get_game_autocomplete_choices(current, include_all_option=False)

    @tree.command(name="app", description="Get account and password for a game (Random or specific ID)")
    @app_commands.describe(
        name="Select or type a game name",
        id="Optional: Random or specific Source ID (e.g. Random, 1, 5, 15, 19, 20)",
    )
    async def cmd_app(
        interaction: discord.Interaction,
        name: str,
        id: Optional[str] = None,
    ) -> None:
        with session_scope() as session:
            detail = find_best_matching_app(session, name)

        if detail is None:
            await interaction.response.send_message(
                f"No matching application found for `{redact_sensitive_text(name)[:60]}`.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        selected_source, mode_label = _select_source_for_app(detail.sources, id)
        if selected_source is None and mode_label.startswith("invalid:"):
            valid_ids = ", ".join(f"#{s.source_id}" for s in detail.sources) or "None"
            await interaction.response.send_message(
                f"Game **{detail.canonical_name}** is not in ID `{id}`. Available IDs for this game: {valid_ids}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # If the selected source does not yet have cached credentials, attempt an on-demand fetch
        if selected_source is not None and not (selected_source.account_email and selected_source.account_password):
            try:
                await check_single_source(
                    selected_source.source_id,
                    worker_id=f"app-ondemand-{interaction.user.id}",
                )
                with session_scope() as session:
                    refreshed = find_best_matching_app(session, detail.canonical_name)
                    if refreshed is not None:
                        detail = refreshed
                        for s in detail.sources:
                            if s.source_id == selected_source.source_id:
                                selected_source = s
                                break
            except Exception:
                pass

        embed = build_app_detail_embed(
            detail,
            selected_source=selected_source,
            selection_mode=mode_label,
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cmd_app.autocomplete("name")
    async def _app_name_autocomplete(
        _interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return get_game_autocomplete_choices(current, include_all_option=False)

    @cmd_app.autocomplete("id")
    async def _app_id_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        ns = getattr(interaction, "namespace", None)
        selected_game = getattr(ns, "name", None) if ns is not None else None
        return get_source_id_autocomplete_choices(selected_game, current)

    @tree.command(name="source-apps", description="List all applications associated with a specific source ID")
    @app_commands.describe(source_id="Source ID (e.g. 1, 5, 15, 19, 20)")
    async def cmd_source_apps(interaction: discord.Interaction, source_id: int) -> None:
        per_page = 10
        with session_scope() as session:
            src_obj = get_source_by_id_or_key(session, source_id)
            if src_obj is None:
                await interaction.response.send_message(
                    f"Source `#{source_id}` was not found.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            src_schema = build_source_schema(session, src_obj)
            src_apps_resp = list_source_apps_paginated(session, src_obj.id, limit=500, offset=0)
            src_apps = src_apps_resp.items if src_apps_resp else []

        total_pages = max(1, math.ceil(len(src_apps) / per_page))
        embed = build_source_apps_embed(
            source=src_schema,
            source_apps=src_apps,
            page=1,
            per_page=per_page,
        )

        if total_pages > 1:
            view = PaginationView(
                total_pages=total_pages,
                page_builder=lambda p: build_source_apps_embed(
                    source=src_schema,
                    source_apps=src_apps,
                    page=p,
                    per_page=per_page,
                ),
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

    @tree.command(name="catalog", description="Browse all apps in a specific source or across the entire catalog")
    @app_commands.describe(source_id="Optional Source ID (e.g. 1, 5, 15, 19, 20)")
    async def cmd_catalog(
        interaction: discord.Interaction,
        source_id: Optional[int] = None,
    ) -> None:
        per_page = 10
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
                src_apps_resp = list_source_apps_paginated(session, src_obj.id, limit=500, offset=0)
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
                all_apps_resp = list_apps_paginated(session, limit=500, offset=0)
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

    @tree.command(name="source", description="Show source details, account credentials, monitoring health, and URL")
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

    @tree.command(name="updates", description="Show recent account updates and latest credentials")
    @app_commands.describe(
        limit="Maximum number of recent updates to display (1-20)",
        game="Optional: Filter account updates by game name",
    )
    async def cmd_updates(
        interaction: discord.Interaction,
        limit: int = 5,
        game: Optional[str] = None,
    ) -> None:
        safe_limit = max(1, min(int(limit), 20))
        with session_scope() as session:
            filter_sids: Optional[List[int]] = None
            matched_game_title: Optional[str] = None
            if game and game.strip() and game.strip().lower() != "all":
                detail = find_best_matching_app(session, game.strip())
                if detail is not None:
                    filter_sids = list(detail.source_ids)
                    matched_game_title = detail.canonical_name
                else:
                    filter_sids = []
                    matched_game_title = game.strip()

            updates_resp = list_updates_paginated(
                session,
                limit=safe_limit,
                offset=0,
                source_ids=filter_sids,
            )
            all_sources = list_sources_paginated(session, limit=20, offset=0).items
            if filter_sids is not None:
                all_sources = [s for s in all_sources if s.id in filter_sids]

        embed = build_updates_embed(
            updates_resp.items,
            safe_limit,
            sources=all_sources,
            game_query=matched_game_title,
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cmd_updates.autocomplete("game")
    async def _updates_game_autocomplete(
        _interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return get_game_autocomplete_choices(current, include_all_option=True)

    @tree.command(name="subscribe", description="Subscribe a channel to latest account updates (all games or a specific game)")
    @app_commands.describe(
        channel="Target Discord text channel for notifications",
        game="Optional: Game name to subscribe to (leave empty for all games)",
    )
    async def cmd_subscribe(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        game: Optional[str] = None,
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

        selected_game_filter: Optional[str] = None
        matched_game_label = "All Games"
        matched_sids_label = "All IDs"

        with session_scope() as session:
            if game and game.strip() and game.strip().lower() != "all":
                detail = find_best_matching_app(session, game.strip())
                if detail is None:
                    await interaction.response.send_message(
                        f"No matching game found for `{redact_sensitive_text(game)[:60]}`.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                selected_game_filter = detail.canonical_name
                matched_game_label = detail.canonical_name
                matched_sids_label = ", ".join(f"#{sid}" for sid in detail.source_ids) or "None"

            sub, created = upsert_discord_subscription(
                session,
                guild_id=str(interaction.guild_id),
                channel_id=str(channel.id),
                game_filter=selected_game_filter,
            )

        action_word = "Subscribed" if created else "Updated subscription for"
        if selected_game_filter:
            msg = (
                f"{action_word} <#{sub.channel_id}> to account updates for "
                f"**{matched_game_label}** (IDs: {matched_sids_label})."
            )
        else:
            msg = f"{action_word} <#{sub.channel_id}> to all AppVault account updates."

        await interaction.response.send_message(
            msg,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @cmd_subscribe.autocomplete("game")
    async def _subscribe_game_autocomplete(
        _interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        return get_game_autocomplete_choices(current, include_all_option=True)

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
                f"Disabled AppVault notifications for <#{channel_id}>.",
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
                "You are not authorized to run manual source checks.",
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
                redact_sensitive_text(str(exc)),
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
                "Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            state = set_worker_paused(session, True)
        await interaction.response.send_message(
            f"Background monitoring worker paused (updated at `{state.updated_at.strftime('%H:%M:%S UTC')}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="resume", description="Resume background polling across all sources")
    async def cmd_admin_resume(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            state = set_worker_paused(session, False)
        await interaction.response.send_message(
            f"Background monitoring worker resumed (updated at `{state.updated_at.strftime('%H:%M:%S UTC')}`).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="check-all", description="Check all enabled sources subject to concurrency limits")
    async def cmd_admin_check_all(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "Administrator authorization required.",
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
            f"- **ID #{o.source_id}**: `{o.status}` | Account: `{o.account_email or 'N/A'}` | Pass: `{o.account_password or 'N/A'}`"
            + (f" — Error: `{redact_sensitive_text(o.error)[:60]}`" if o.error else "")
            for o in outcomes
        ]
        summary_text = "\n".join(lines) if lines else "No enabled sources were checked."
        await interaction.followup.send(
            f"Completed check across **{len(outcomes)}** source(s):\n{summary_text}"[:2000],
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin_group.command(name="reload-catalog", description="Idempotently reload seed catalog records")
    async def cmd_admin_reload_catalog(interaction: discord.Interaction) -> None:
        if not is_authorized_admin(interaction):
            await interaction.response.send_message(
                "Administrator authorization required.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        with session_scope() as session:
            res = reload_seed_catalog(session)
        await interaction.response.send_message(
            f"Reloaded seed catalog `v{res.version}`:\n"
            f"- Sources: `{res.sources_created}` created, `{res.sources_updated}` updated\n"
            f"- Apps: `{res.apps_created}` created, `{res.apps_existing}` existing\n"
            f"- Source Links: `{res.links_created}` created, `{res.links_updated}` updated\n"
            f"- Aliases added: `{res.aliases_created}`",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    tree.add_command(admin_group)
