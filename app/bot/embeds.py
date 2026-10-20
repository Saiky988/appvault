"""Mobile-friendly, icon-free Discord Embed builders and interactive pagination views."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Callable, List, Optional, Sequence

import discord

from app.core.security import redact_sensitive_text
from app.schemas.api import (
    AppDetailSchema,
    AppSearchResponseSchema,
    AppSummarySchema,
    ChangeEventSchema,
    SourceAppItemSchema,
    SourceBriefSchema,
    SourceCheckOutcomeSchema,
    SourceSchema,
    StatsResponseSchema,
)


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return "Never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _health_label(health: str) -> str:
    mapping = {
        "healthy": "Healthy",
        "seed_only": "Seed (Unverified)",
        "error": "Error",
        "disabled": "Disabled",
    }
    return mapping.get(health, str(health).capitalize())


def _origin_label(origin: str) -> str:
    return "Observed Page Content" if origin == "observed" else "Seed Data"


def _active_label(enabled: bool) -> str:
    return "Active" if enabled else "Inactive"


def build_search_embed(
    search_resp: AppSearchResponseSchema,
    *,
    page: int = 1,
    per_page: int = 5,
) -> discord.Embed:
    """Build a paginated, icon-free embed for /search results showing all matching source IDs and credentials."""
    total_items = len(search_resp.items)
    total_pages = max(1, math.ceil(total_items / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    page_slice = search_resp.items[start : start + per_page]

    safe_query = redact_sensitive_text(search_resp.query)[:80]
    embed = discord.Embed(
        title=f"Search Results: {safe_query}",
        description=(
            f"Found **{search_resp.total}** matching application(s) "
            f"(normalized query: `{search_resp.normalized_query}`). "
            f"Showing page **{safe_page}/{total_pages}**."
            if total_items
            else "No matching applications found in the catalog."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    for item in page_slice:
        app = item.app
        all_source_ids = [s.source_id for s in app.sources] or list(app.source_ids)
        source_ids_display = (
            ", ".join(f"#{sid}" for sid in all_source_ids)
            if all_source_ids
            else "None"
        )

        src_lines: List[str] = []
        for s in app.sources:
            cred_part = (
                f"\n  Account: `{s.account_email}` | Password: `{s.account_password}`"
                if (s.account_email or s.account_password)
                else ""
            )
            src_lines.append(
                f"- **Source #{s.source_id}** | State: `{_active_label(s.enabled)}` | "
                f"Health: `{_health_label(s.health_status)}` | "
                f"Provenance: `{_origin_label(s.data_origin)}`{cred_part}\n"
                f"  Checked: `{_fmt_dt(s.last_checked_at)}` | [Source #{s.source_id} URL]({s.url})"
            )

        if not src_lines:
            src_lines.append("- No associated sources registered in catalog.")

        header_meta = (
            f"**Matching Source IDs**: {source_ids_display}\n"
            f"**Match**: `{item.match_type}` ({int(item.score * 100)}%) | **Catalog App ID**: `#{app.id}`\n"
        )
        value_text = header_meta + "\n".join(src_lines)

        embed.add_field(
            name=f"{app.canonical_name} [Sources: {source_ids_display}]",
            value=value_text[:1024],
            inline=False,
        )

    embed.set_footer(text=f"Page {safe_page}/{total_pages} | AppVault Catalog")
    return embed


def build_app_detail_embed(
    detail: AppDetailSchema,
    *,
    selected_source: Optional[SourceBriefSchema] = None,
    selection_mode: str = "random",
) -> discord.Embed:
    """Build an icon-free embed for /app showing the selected ID's account & password plus source relationships."""
    all_source_ids = [s.source_id for s in detail.sources] or list(detail.source_ids)
    source_ids_str = ", ".join(f"#{sid}" for sid in all_source_ids) if all_source_ids else "None"
    aliases_str = ", ".join(f"`{a}`" for a in detail.aliases[:8]) if detail.aliases else "None"

    chosen = selected_source or (detail.sources[0] if detail.sources else None)
    title_suffix = f" — ID #{chosen.source_id}" if chosen else ""

    embed = discord.Embed(
        title=f"{detail.canonical_name}{title_suffix}",
        url=chosen.url if chosen else None,
        description=(
            f"**Game**: `{detail.canonical_name}`\n"
            f"**Available IDs**: {source_ids_str}"
            + (f" (Selected: `ID #{chosen.source_id}` via `{selection_mode}`)" if chosen else "")
            + f"\n**Aliases**: {aliases_str}"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if chosen is not None:
        email_display = chosen.account_email or "Chua cap nhat (Hay chay /check)"
        pass_display = chosen.account_password or "Chua cap nhat (Hay chay /check)"
        embed.add_field(
            name=f"Tai Khoan (ID #{chosen.source_id})",
            value=f"```\n{email_display}\n```",
            inline=False,
        )
        embed.add_field(
            name=f"Mat Khau (ID #{chosen.source_id})",
            value=f"```\n{pass_display}\n```",
            inline=False,
        )
        embed.add_field(
            name="Selected ID Status",
            value=(
                f"ID: `#{chosen.source_id}` | State: `{_active_label(chosen.enabled)}` | "
                f"Health: `{_health_label(chosen.health_status)}`\n"
                f"Updated: `{_fmt_dt(chosen.account_updated_at or chosen.last_success_at)}` | "
                f"[Open ID #{chosen.source_id} Page]({chosen.url})"
            ),
            inline=False,
        )

    if detail.sources:
        src_lines: List[str] = []
        for s in detail.sources:
            cred_status = (
                f"Account: `{s.account_email}`"
                if s.account_email
                else f"Provenance: `{_origin_label(s.data_origin)}`"
            )
            src_lines.append(
                f"- **Source #{s.source_id}** (Listed as: `{s.original_title}`)\n"
                f"  State: `{_active_label(s.enabled)}` | Health: `{_health_label(s.health_status)}` | {cred_status}\n"
                f"  Last Checked: `{_fmt_dt(s.last_checked_at)}` | [Source #{s.source_id} Page]({s.url})"
            )
        embed.add_field(
            name=f"Source Relationships ({len(detail.sources)})",
            value="\n".join(src_lines)[:1024],
            inline=False,
        )

    if detail.recent_changes:
        chg_lines = [
            f"- `{_fmt_dt(c.detected_at)}` [**Source #{c.source_id}** `{c.event_type}`]: "
            f"{c.summary[:120]}"
            for c in detail.recent_changes[:5]
        ]
        embed.add_field(
            name="Change History",
            value="\n".join(chg_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Change History",
            value="No changes have been detected yet. Associated sources remain active in the catalog.",
            inline=False,
        )

    embed.set_footer(text=f"App ID #{detail.id} | Sources: {source_ids_str} | AppVault")
    return embed


def build_source_apps_embed(
    *,
    source: SourceSchema,
    source_apps: Sequence[SourceAppItemSchema],
    page: int = 1,
    per_page: int = 10,
) -> discord.Embed:
    """Build a paginated, icon-free embed for /source-apps and /catalog [source_id]."""
    total = len(source_apps)
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    items = source_apps[start : start + per_page]

    app_lines = [
        f"`{idx:02d}.` **{item.original_title}** "
        f"— Provenance: `{_origin_label(item.data_origin)}`"
        for idx, item in enumerate(items, start=start + 1)
    ]

    header_block = (
        f"**Source ID**: `#{source.id}` (Key: `{source.source_key}`)\n"
        f"**Source URL**: {source.url}\n"
        f"**State**: `{_active_label(source.enabled)}` | **Health**: `{_health_label(source.health_status)}`\n"
        f"**Last Checked**: `{_fmt_dt(source.last_checked_at)}` | "
        f"**Last Successful Fetch**: `{_fmt_dt(source.last_success_at)}`\n"
        f"**Total Apps**: `{total}` (Seed: `{source.seed_app_count}`, Observed: `{source.observed_app_count}`)"
    )
    if source.account_email or source.account_password:
        header_block += (
            f"\n**Tai Khoan**: `{source.account_email or 'N/A'}` | "
            f"**Mat Khau**: `{source.account_password or 'N/A'}`"
        )
    if source.last_error:
        header_block += f"\n**Last Error**: `{redact_sensitive_text(source.last_error)[:140]}`"

    body_block = "\n".join(app_lines) if app_lines else "No applications associated with this source."

    embed = discord.Embed(
        title=f"Source #{source.id} Applications ({total} Total)",
        url=source.url,
        description=f"{header_block}\n\n**Applications (Page {safe_page}/{total_pages})**:\n{body_block}"[:4096],
        color=discord.Color.green() if source.health_status == "healthy" else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Page {safe_page}/{total_pages} | Source #{source.id} | AppVault")
    return embed


def build_catalog_embed(
    *,
    source: Optional[SourceSchema],
    source_apps: Optional[Sequence[SourceAppItemSchema]],
    all_apps: Optional[Sequence[AppSummarySchema]],
    page: int = 1,
    per_page: int = 10,
) -> discord.Embed:
    """Build a paginated, icon-free embed for /catalog [source_id]."""
    if source is not None and source_apps is not None:
        return build_source_apps_embed(
            source=source,
            source_apps=source_apps,
            page=page,
            per_page=per_page,
        )

    apps_list = list(all_apps or [])
    total = len(apps_list)
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    items_all = apps_list[start : start + per_page]

    lines: List[str] = []
    for a in items_all:
        sids = [s.source_id for s in a.sources] or list(a.source_ids)
        sources_text = ", ".join(f"`#{sid}`" for sid in sids) if sids else "None"
        origins = sorted({_origin_label(s.data_origin) for s in a.sources})
        origin_text = f" ({', '.join(origins)})" if origins else ""
        lines.append(
            f"- **{a.canonical_name}** — Source IDs: {sources_text}{origin_text}"
        )

    embed = discord.Embed(
        title=f"AppVault Catalog ({total} Unique Applications)",
        description=("\n".join(lines) if lines else "Catalog is empty.")[:4096],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(
        text=f"Page {safe_page}/{total_pages} | Use /source-apps source_id:<id> to inspect a source"
    )
    return embed


def build_source_detail_embed(source: SourceSchema) -> discord.Embed:
    """Build an icon-free embed for /source id:<integer>."""
    embed = discord.Embed(
        title=f"Source #{source.id} Details (Key: {source.source_key})",
        url=source.url,
        description=f"**Public URL**: {source.url}",
        color=discord.Color.green() if source.health_status == "healthy" else (
            discord.Color.red() if source.health_status == "error" else discord.Color.orange()
        ),
        timestamp=datetime.now(timezone.utc),
    )
    if source.account_email or source.account_password:
        embed.add_field(
            name="Tai Khoan (Email)",
            value=f"```\n{source.account_email or 'N/A'}\n```",
            inline=False,
        )
        embed.add_field(
            name="Mat Khau (Password)",
            value=f"```\n{source.account_password or 'N/A'}\n```",
            inline=False,
        )
    embed.add_field(name="Health Status", value=f"`{_health_label(source.health_status)}`", inline=True)
    embed.add_field(name="State", value=f"`{_active_label(source.enabled)}`", inline=True)
    embed.add_field(
        name="Polling Interval",
        value=f"`{source.check_interval_seconds}s`"
        + (" (custom)" if source.has_custom_interval else " (default)"),
        inline=True,
    )
    embed.add_field(name="Last Checked", value=f"`{_fmt_dt(source.last_checked_at)}`", inline=True)
    embed.add_field(name="Last Successful Fetch", value=f"`{_fmt_dt(source.last_success_at)}`", inline=True)
    embed.add_field(
        name="Last HTTP Status",
        value=f"`{source.last_http_status}`" if source.last_http_status else "`N/A`",
        inline=True,
    )
    embed.add_field(
        name="Catalog Counts",
        value=(
            f"Total: **{source.app_count}** "
            f"(Seed: `{source.seed_app_count}`, Observed: `{source.observed_app_count}`)"
        ),
        inline=False,
    )
    if source.current_content_hash:
        embed.add_field(
            name="Content Hash",
            value=f"`{source.current_content_hash[:24]}...`",
            inline=False,
        )
    if source.last_error:
        embed.add_field(
            name="Last Monitoring Error",
            value=f"```{redact_sensitive_text(source.last_error)[:900]}```",
            inline=False,
        )
    embed.set_footer(text="AppVault Source Monitor")
    return embed


def build_updates_embed(
    events: Sequence[ChangeEventSchema],
    limit: int,
    *,
    sources: Optional[Sequence[SourceSchema]] = None,
    game_query: Optional[str] = None,
) -> discord.Embed:
    """Build an icon-free embed for /updates showing latest account credentials and change events."""
    title_str = f"Recent Account Updates (Limit: {limit})"
    if game_query:
        title_str += f" — Filter: {game_query}"

    embed = discord.Embed(
        title=title_str,
        description=(
            f"Showing **{len(events)}** recent update event(s):"
            if events
            else "No change events recorded yet. Showing current source account snapshots below:"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if events:
        for ev in events[:10]:
            link_part = f" | [Source Link]({ev.source_url})" if ev.source_url else ""
            cred_lines = ""
            if ev.account_email or ev.account_password:
                cred_lines = (
                    f"Tai Khoan: `{ev.account_email or 'N/A'}`\n"
                    f"Mat Khau: `{ev.account_password or 'N/A'}`\n"
                )
            embed.add_field(
                name=f"Event #{ev.id} | ID #{ev.source_id} ({ev.event_type})",
                value=(
                    f"{cred_lines}"
                    f"{ev.summary[:300]}\n"
                    f"Updated: `{_fmt_dt(ev.detected_at)}` | Status: `{ev.notification_status}`{link_part}"
                )[:1024],
                inline=False,
            )
    elif sources:
        for src in sources[:10]:
            embed.add_field(
                name=f"ID #{src.id} | State: {_active_label(src.enabled)} ({_health_label(src.health_status)})",
                value=(
                    f"Tai Khoan: `{src.account_email or 'Chua cap nhat'}`\n"
                    f"Mat Khau: `{src.account_password or 'Chua cap nhat'}`\n"
                    f"Updated: `{_fmt_dt(src.account_updated_at or src.last_success_at)}` | [Open Page]({src.url})"
                )[:1024],
                inline=False,
            )

    embed.set_footer(text="AppVault Account Updates")
    return embed


def build_status_embed(
    stats: StatsResponseSchema,
    *,
    latency_ms: float,
    db_healthy: bool,
    worker_last_heartbeat: Optional[datetime],
) -> discord.Embed:
    """Build an icon-free embed for /status."""
    worker_status = "Paused" if stats.worker_paused else "Running"
    embed = discord.Embed(
        title="AppVault System Status",
        color=discord.Color.green() if db_healthy else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Bot Latency", value=f"`{latency_ms:.1f} ms`", inline=True)
    embed.add_field(
        name="Database Health",
        value="`Connected`" if db_healthy else "`Unreachable`",
        inline=True,
    )
    embed.add_field(name="Worker Status", value=f"`{worker_status}`", inline=True)
    embed.add_field(
        name="Sources",
        value=(
            f"Enabled: **{stats.enabled_sources}/{stats.total_sources}**\n"
            f"Healthy: `{stats.healthy_sources}` | Seed: `{stats.seed_only_sources}` | Error: `{stats.error_sources}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Catalog & Notifications",
        value=(
            f"Canonical Apps: **{stats.total_apps}** ({stats.total_source_app_links} source links)\n"
            f"Change Events: **{stats.total_change_events}** (Pending alerts: `{stats.pending_notifications}`)\n"
            f"Subscribed Channels: **{stats.active_subscriptions}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Last Successful Check",
        value=f"`{_fmt_dt(stats.last_successful_check_at)}`",
        inline=True,
    )
    embed.add_field(
        name="Worker Heartbeat",
        value=f"`{_fmt_dt(worker_last_heartbeat)}`",
        inline=True,
    )
    embed.set_footer(text="AppVault Operational Status")
    return embed


def build_check_outcome_embed(outcome: SourceCheckOutcomeSchema) -> discord.Embed:
    """Build an icon-free embed summarizing a manual /check result."""
    is_ok = outcome.status in ("changed", "unchanged", "not_modified")
    embed = discord.Embed(
        title=f"Manual Check — Source #{outcome.source_id}",
        url=outcome.url,
        color=discord.Color.green() if is_ok else discord.Color.orange(),
        timestamp=outcome.checked_at,
    )
    if outcome.account_email or outcome.account_password:
        embed.add_field(
            name="Tai Khoan (Email)",
            value=f"```\n{outcome.account_email or 'N/A'}\n```",
            inline=False,
        )
        embed.add_field(
            name="Mat Khau (Password)",
            value=f"```\n{outcome.account_password or 'N/A'}\n```",
            inline=False,
        )
    embed.add_field(name="Outcome", value=f"`{outcome.status}`", inline=True)
    embed.add_field(
        name="HTTP Status",
        value=f"`{outcome.http_status}`" if outcome.http_status else "`N/A`",
        inline=True,
    )
    embed.add_field(name="Data Provenance", value=f"`{_origin_label(outcome.data_origin)}`", inline=True)
    embed.add_field(name="Catalog Apps", value=f"`{outcome.extracted_app_count}`", inline=True)
    embed.add_field(name="Changed", value="`Yes`" if outcome.changed else "`No`", inline=True)
    if outcome.error:
        embed.add_field(
            name="Error (Seed Catalog Preserved)",
            value=f"```{redact_sensitive_text(outcome.error)[:800]}```",
            inline=False,
        )
    embed.set_footer(text="AppVault Manual Source Check")
    return embed


class PaginationView(discord.ui.View):
    """Interactive Discord UI View for navigating multi-page embeds."""

    def __init__(
        self,
        *,
        total_pages: int,
        page_builder: Callable[[int], discord.Embed],
        author_id: int,
        current_page: int = 1,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.total_pages = max(1, total_pages)
        self.page_builder = page_builder
        self.author_id = author_id
        self.current_page = max(1, min(current_page, self.total_pages))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        for child in self.children:
            if getattr(child, "custom_id", None) == "prev_page":
                child.disabled = self.current_page <= 1
            elif getattr(child, "custom_id", None) == "next_page":
                child.disabled = self.current_page >= self.total_pages

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the user who ran this command can turn pages.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.current_page = max(1, self.current_page - 1)
        self._sync_buttons()
        embed = self.page_builder(self.current_page)
        await interaction.response.edit_message(
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the user who ran this command can turn pages.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.current_page = min(self.total_pages, self.current_page + 1)
        self._sync_buttons()
        embed = self.page_builder(self.current_page)
        await interaction.response.edit_message(
            embed=embed,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
