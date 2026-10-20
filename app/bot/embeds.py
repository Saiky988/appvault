"""Mobile-friendly Discord Embed builders and interactive pagination views."""
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


def _health_badge(health: str) -> str:
    mapping = {
        "healthy": "🟢 Healthy",
        "seed_only": "🟡 Seed Data (Unverified)",
        "error": "🔴 Error",
        "disabled": "⚪ Disabled",
    }
    return mapping.get(health, f"⚪ {health}")


def _origin_badge(origin: str) -> str:
    return "🌐 Observed Page Data" if origin == "observed" else "📦 Seed Data"


def build_search_embed(
    search_resp: AppSearchResponseSchema,
    *,
    page: int = 1,
    per_page: int = 5,
) -> discord.Embed:
    """Build a paginated mobile-friendly embed for /search results."""
    total_items = len(search_resp.items)
    total_pages = max(1, math.ceil(total_items / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    page_slice = search_resp.items[start : start + per_page]

    embed = discord.Embed(
        title=f"🔎 Search Results for '{redact_sensitive_text(search_resp.query)[:80]}'",
        description=(
            f"Found **{search_resp.total}** matching app(s) "
            f"(normalized: `{search_resp.normalized_query}`). Showing page **{safe_page}/{total_pages}**."
            if total_items
            else "No matching applications found in the catalog."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    for item in page_slice:
        app = item.app
        src_lines: List[str] = []
        for s in app.sources[:4]:
            src_lines.append(
                f"• **Source #{s.source_id}** ({_health_badge(s.health_status)}) — "
                f"{_origin_badge(s.data_origin)}\n"
                f"  Checked: `{_fmt_dt(s.last_checked_at)}` | [Article Link]({s.url})"
            )
        if not src_lines:
            src_lines.append("• No active sources")

        value_text = (
            f"**Match**: `{item.match_type}` ({int(item.score * 100)}%)\n"
            + "\n".join(src_lines)
        )
        embed.add_field(
            name=f"📱 {redact_sensitive_text(app.canonical_name)} (ID #{app.id})",
            value=value_text[:1024],
            inline=False,
        )

    embed.set_footer(text=f"Page {safe_page}/{total_pages} • AppVault Catalog")
    return embed


def build_app_detail_embed(detail: AppDetailSchema) -> discord.Embed:
    """Build a detailed embed for /app name:<text>."""
    embed = discord.Embed(
        title=f"📱 {redact_sensitive_text(detail.canonical_name)}",
        description=(
            f"**Normalized Name**: `{detail.normalized_name}`\n"
            f"**Platform**: `{detail.platform or 'iOS'}`\n"
            f"**Aliases**: {', '.join(f'`{a}`' for a in detail.aliases[:8]) if detail.aliases else 'None'}"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if detail.sources:
        src_lines: List[str] = []
        for s in detail.sources[:6]:
            err_note = f"\n  ⚠️ Error: `{redact_sensitive_text(s.last_error)[:90]}`" if s.last_error else ""
            src_lines.append(
                f"• **Source #{s.source_id}** (`{s.original_title}`)\n"
                f"  Status: {_health_badge(s.health_status)} | {_origin_badge(s.data_origin)}\n"
                f"  Last Checked: `{_fmt_dt(s.last_checked_at)}` | [Source Page]({s.url}){err_note}"
            )
        embed.add_field(
            name=f"🗂️ Tracked Sources ({len(detail.sources)})",
            value="\n".join(src_lines)[:1024],
            inline=False,
        )

    if detail.recent_changes:
        chg_lines = [
            f"• `{_fmt_dt(c.detected_at)}` [**#{c.source_id}** `{c.event_type}`]: "
            f"{redact_sensitive_text(c.summary)[:120]}"
            for c in detail.recent_changes[:5]
        ]
        embed.add_field(
            name="🕒 Recent Source Change History",
            value="\n".join(chg_lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="🕒 Recent Source Change History",
            value="No change events recorded yet for this app's sources.",
            inline=False,
        )

    embed.set_footer(text=f"App ID #{detail.id} • AppVault")
    return embed


def build_catalog_embed(
    *,
    source: Optional[SourceSchema],
    source_apps: Optional[Sequence[SourceAppItemSchema]],
    all_apps: Optional[Sequence[AppSummarySchema]],
    page: int = 1,
    per_page: int = 8,
) -> discord.Embed:
    """Build a paginated embed for /catalog [source_id]."""
    if source is not None and source_apps is not None:
        total = len(source_apps)
        total_pages = max(1, math.ceil(total / per_page))
        safe_page = max(1, min(page, total_pages))
        start = (safe_page - 1) * per_page
        items = source_apps[start : start + per_page]

        lines = [
            f"`{item.display_order + 1:02d}.` **{redact_sensitive_text(item.original_title)}** "
            f"({_origin_badge(item.data_origin)})"
            for item in items
        ]
        desc = (
            f"**Status**: {_health_badge(source.health_status)} | "
            f"**Last Checked**: `{_fmt_dt(source.last_checked_at)}`\n"
            f"**Source Link**: [Open Public Listing]({source.url})\n\n"
            + ("\n".join(lines) if lines else "No applications in this source.")
        )
        embed = discord.Embed(
            title=f"📚 Source #{source.id} Catalog ({total} Apps)",
            description=desc[:4096],
            color=discord.Color.green() if source.health_status == "healthy" else discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Page {safe_page}/{total_pages} • Source #{source.id}")
        return embed

    apps_list = list(all_apps or [])
    total = len(apps_list)
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    items_all = apps_list[start : start + per_page]

    lines = [
        f"• **{redact_sensitive_text(a.canonical_name)}** — Sources: "
        + (", ".join(f"`#{sid}`" for sid in a.source_ids) if a.source_ids else "None")
        for a in items_all
    ]
    embed = discord.Embed(
        title=f"📚 AppVault Complete Catalog ({total} Unique Apps)",
        description=("\n".join(lines) if lines else "Catalog is empty.")[:4096],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Page {safe_page}/{total_pages} • Browse specific source with /catalog source_id:<id>")
    return embed


def build_source_detail_embed(source: SourceSchema) -> discord.Embed:
    """Build embed for /source id:<integer>."""
    embed = discord.Embed(
        title=f"🛰️ Source #{source.id} (Key: {source.source_key})",
        url=source.url,
        description=f"**Public URL**: {source.url}",
        color=discord.Color.green() if source.health_status == "healthy" else (
            discord.Color.red() if source.health_status == "error" else discord.Color.orange()
        ),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Health Status", value=_health_badge(source.health_status), inline=True)
    embed.add_field(name="Enabled", value="✅ Yes" if source.enabled else "❌ No", inline=True)
    embed.add_field(
        name="Polling Interval",
        value=f"`{source.check_interval_seconds}s`" + (" (override)" if source.has_custom_interval else " (default)"),
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
            name="⚠️ Last Monitoring Error",
            value=f"```{redact_sensitive_text(source.last_error)[:900]}```",
            inline=False,
        )
    embed.set_footer(text="AppVault Source Monitor")
    return embed


def build_updates_embed(events: Sequence[ChangeEventSchema], limit: int) -> discord.Embed:
    """Build embed for /updates [limit]."""
    embed = discord.Embed(
        title=f"🔔 Recent Detected Changes (up to {limit})",
        description="No change events detected yet." if not events else f"Showing **{len(events)}** recent event(s):",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    for ev in events[:10]:
        link_part = f" | [Source Link]({ev.source_url})" if ev.source_url else ""
        embed.add_field(
            name=f"Event #{ev.id} • Source #{ev.source_id} (`{ev.event_type}`)",
            value=(
                f"{redact_sensitive_text(ev.summary)[:400]}\n"
                f"🕒 `{_fmt_dt(ev.detected_at)}` • Status: `{ev.notification_status}`{link_part}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text="AppVault Change Log")
    return embed


def build_status_embed(
    stats: StatsResponseSchema,
    *,
    latency_ms: float,
    db_healthy: bool,
    worker_last_heartbeat: Optional[datetime],
) -> discord.Embed:
    """Build embed for /status."""
    worker_status = "⏸️ Paused" if stats.worker_paused else "▶️ Running"
    embed = discord.Embed(
        title="📊 AppVault System Status",
        color=discord.Color.green() if db_healthy else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Bot Latency", value=f"`{latency_ms:.1f} ms`", inline=True)
    embed.add_field(name="Database Health", value="🟢 Connected" if db_healthy else "🔴 Error", inline=True)
    embed.add_field(name="Worker Status", value=worker_status, inline=True)
    embed.add_field(
        name="Sources",
        value=(
            f"Enabled: **{stats.enabled_sources}/{stats.total_sources}**\n"
            f"🟢 Healthy: `{stats.healthy_sources}` | 🟡 Seed: `{stats.seed_only_sources}` | 🔴 Error: `{stats.error_sources}`"
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
    """Build embed summarizing a manual /check result."""
    is_ok = outcome.status in ("changed", "unchanged", "not_modified")
    embed = discord.Embed(
        title=f"🔍 Manual Check — Source #{outcome.source_id}",
        url=outcome.url,
        color=discord.Color.green() if is_ok else discord.Color.orange(),
        timestamp=outcome.checked_at,
    )
    embed.add_field(name="Outcome", value=f"`{outcome.status}`", inline=True)
    embed.add_field(
        name="HTTP Status",
        value=f"`{outcome.http_status}`" if outcome.http_status else "`N/A`",
        inline=True,
    )
    embed.add_field(name="Data Origin", value=_origin_badge(outcome.data_origin), inline=True)
    embed.add_field(name="Catalog Apps", value=f"`{outcome.extracted_app_count}`", inline=True)
    embed.add_field(name="Changed", value="✅ Yes" if outcome.changed else "No", inline=True)
    if outcome.error:
        embed.add_field(
            name="⚠️ Error (Seed Catalog Preserved)",
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

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, custom_id="prev_page")
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

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="next_page")
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
