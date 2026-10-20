"""Concise, production-grade, mobile-friendly Discord Embed builders (single backticks for 1-tap mobile copy)."""
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
        return "Chưa cập nhật"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def build_search_embed(
    search_resp: AppSearchResponseSchema,
    *,
    page: int = 1,
    per_page: int = 6,
) -> discord.Embed:
    """Build a clean, concise mobile embed for /search results."""
    total_items = len(search_resp.items)
    total_pages = max(1, math.ceil(total_items / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    page_slice = search_resp.items[start : start + per_page]

    safe_query = redact_sensitive_text(search_resp.query)[:80]
    embed = discord.Embed(
        title=f"Tìm kiếm: {safe_query}",
        description=(
            f"Tìm thấy **{search_resp.total}** kết quả phù hợp."
            if total_items
            else "Không tìm thấy game phù hợp trong danh mục."
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
            else "Chưa có"
        )

        # Pick the first source that has credentials to preview, or the first available source
        with_creds = [s for s in app.sources if s.account_email and s.account_password]
        preview_src = with_creds[0] if with_creds else (app.sources[0] if app.sources else None)

        lines: List[str] = [f"ID hỗ trợ: `{source_ids_display}`"]
        if preview_src and (preview_src.account_email or preview_src.account_password):
            lines.append(f"Tài khoản (ID #{preview_src.source_id}): `{preview_src.account_email or 'Chưa có'}`")
            lines.append(f"Mật khẩu (ID #{preview_src.source_id}): `{preview_src.account_password or 'Chưa có'}`")

        embed.add_field(
            name=f"{app.canonical_name} — ID: {source_ids_display}",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    embed.set_footer(text=f"Trang {safe_page}/{total_pages} • AppVault")
    return embed


def build_app_detail_embed(
    detail: AppDetailSchema,
    *,
    selected_source: Optional[SourceBriefSchema] = None,
    selection_mode: str = "random",
) -> discord.Embed:
    """Build a concise, production-ready embed for /app using single backticks for easy mobile copy."""
    all_source_ids = [s.source_id for s in detail.sources] or list(detail.source_ids)
    source_ids_str = ", ".join(f"#{sid}" for sid in all_source_ids) if all_source_ids else "Chưa có"

    chosen = selected_source or (detail.sources[0] if detail.sources else None)
    title_str = (
        f"{detail.canonical_name} — ID #{chosen.source_id}"
        if chosen
        else detail.canonical_name
    )

    embed = discord.Embed(
        title=title_str,
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )

    if chosen is not None:
        email_display = chosen.account_email or "Đang cập nhật"
        pass_display = chosen.account_password or "Đang cập nhật"
        updated_str = _fmt_dt(chosen.account_updated_at or chosen.last_success_at)

        embed.add_field(
            name="Tài khoản",
            value=f"`{email_display}`",
            inline=False,
        )
        embed.add_field(
            name="Mật khẩu",
            value=f"`{pass_display}`",
            inline=False,
        )
        embed.add_field(
            name="ID đang xem",
            value=f"`#{chosen.source_id}`",
            inline=True,
        )
        embed.add_field(
            name="ID hỗ trợ game này",
            value=f"`{source_ids_str}`",
            inline=True,
        )
        embed.set_footer(text=f"AppVault • Cập nhật: {updated_str}")
    else:
        embed.description = "Hiện chưa có ID khả dụng cho game này."
        embed.set_footer(text="AppVault")

    return embed


def build_source_apps_embed(
    *,
    source: SourceSchema,
    source_apps: Sequence[SourceAppItemSchema],
    page: int = 1,
    per_page: int = 12,
) -> discord.Embed:
    """Build a clean, paginated embed for /source-apps and /catalog [source_id]."""
    total = len(source_apps)
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages))
    start = (safe_page - 1) * per_page
    items = source_apps[start : start + per_page]

    app_lines = [
        f"`{idx:02d}.` {item.original_title}"
        for idx, item in enumerate(items, start=start + 1)
    ]

    cred_block = ""
    if source.account_email or source.account_password:
        cred_block = (
            f"**Tài khoản**: `{source.account_email or 'Chưa có'}`\n"
            f"**Mật khẩu**: `{source.account_password or 'Chưa có'}`\n\n"
        )

    body_block = "\n".join(app_lines) if app_lines else "Chưa có ứng dụng nào trong ID này."

    embed = discord.Embed(
        title=f"Danh sách Game — ID #{source.id} ({total} Game)",
        description=f"{cred_block}{body_block}"[:4096],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    updated_str = _fmt_dt(source.account_updated_at or source.last_success_at)
    embed.set_footer(text=f"Trang {safe_page}/{total_pages} • ID #{source.id} • Cập nhật: {updated_str}")
    return embed


def build_catalog_embed(
    *,
    source: Optional[SourceSchema],
    source_apps: Optional[Sequence[SourceAppItemSchema]],
    all_apps: Optional[Sequence[AppSummarySchema]],
    page: int = 1,
    per_page: int = 12,
) -> discord.Embed:
    """Build a clean, paginated embed for /catalog [source_id]."""
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
        sources_text = ", ".join(f"#{sid}" for sid in sids) if sids else "Chưa có"
        lines.append(f"- **{a.canonical_name}** (`{sources_text}`)")

    embed = discord.Embed(
        title=f"Danh mục Game ({total} Game)",
        description=("\n".join(lines) if lines else "Danh mục trống.")[:4096],
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Trang {safe_page}/{total_pages} • AppVault")
    return embed


def build_source_detail_embed(source: SourceSchema) -> discord.Embed:
    """Build a clean, concise embed for /source id:<integer>."""
    email_val = source.account_email or "Đang cập nhật"
    pass_val = source.account_password or "Đang cập nhật"
    updated_str = _fmt_dt(source.account_updated_at or source.last_success_at)

    embed = discord.Embed(
        title=f"Thông tin ID #{source.id}",
        color=discord.Color.green() if source.health_status == "healthy" else discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Tài khoản", value=f"`{email_val}`", inline=False)
    embed.add_field(name="Mật khẩu", value=f"`{pass_val}`", inline=False)
    embed.add_field(name="Số lượng game", value=f"`{source.app_count}`", inline=True)
    embed.add_field(name="Cập nhật", value=f"`{updated_str}`", inline=True)
    embed.set_footer(text=f"AppVault • ID #{source.id}")
    return embed


def build_updates_embed(
    events: Sequence[ChangeEventSchema],
    limit: int,
    *,
    sources: Optional[Sequence[SourceSchema]] = None,
    game_query: Optional[str] = None,
) -> discord.Embed:
    """Build a clean, concise embed for /updates showing latest account credentials with single backticks."""
    title_str = "Cập nhật Tài khoản Mới nhất"
    if game_query:
        title_str += f" — {game_query}"

    embed = discord.Embed(
        title=title_str,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if events:
        for ev in events[:limit]:
            email_val = ev.account_email or "Đang cập nhật"
            pass_val = ev.account_password or "Đang cập nhật"
            embed.add_field(
                name=f"ID #{ev.source_id} • {_fmt_dt(ev.detected_at)}",
                value=(
                    f"Tài khoản: `{email_val}`\n"
                    f"Mật khẩu: `{pass_val}`"
                ),
                inline=False,
            )
    elif sources:
        for src in sources[:limit]:
            email_val = src.account_email or "Đang cập nhật"
            pass_val = src.account_password or "Đang cập nhật"
            embed.add_field(
                name=f"ID #{src.id} • {_fmt_dt(src.account_updated_at or src.last_success_at)}",
                value=(
                    f"Tài khoản: `{email_val}`\n"
                    f"Mật khẩu: `{pass_val}`"
                ),
                inline=False,
            )
    else:
        embed.description = "Chưa có dữ liệu cập nhật tài khoản."

    embed.set_footer(text="AppVault")
    return embed


def build_status_embed(
    stats: StatsResponseSchema,
    *,
    latency_ms: float,
    db_healthy: bool,
    worker_last_heartbeat: Optional[datetime],
) -> discord.Embed:
    """Build a concise embed for /status."""
    worker_status = "Tạm dừng" if stats.worker_paused else "Đang chạy"
    embed = discord.Embed(
        title="Trạng thái Hệ thống AppVault",
        color=discord.Color.green() if db_healthy else discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Độ trễ Bot", value=f"`{latency_ms:.0f} ms`", inline=True)
    embed.add_field(name="Trạng thái", value=f"`{worker_status}`", inline=True)
    embed.add_field(name="Tổng số ID", value=f"`{stats.enabled_sources}/{stats.total_sources}`", inline=True)
    embed.add_field(name="Tổng số Game", value=f"`{stats.total_apps}`", inline=True)
    embed.add_field(name="Kênh nhận thông báo", value=f"`{stats.active_subscriptions}`", inline=True)
    embed.add_field(
        name="Cập nhật gần nhất",
        value=f"`{_fmt_dt(stats.last_successful_check_at)}`",
        inline=True,
    )
    embed.set_footer(text="AppVault")
    return embed


def build_check_outcome_embed(outcome: SourceCheckOutcomeSchema) -> discord.Embed:
    """Build a concise embed summarizing a manual /check result."""
    is_ok = outcome.status in ("changed", "unchanged", "not_modified")
    email_val = outcome.account_email or "Chưa có"
    pass_val = outcome.account_password or "Chưa có"

    embed = discord.Embed(
        title=f"Kiểm tra ID #{outcome.source_id}",
        color=discord.Color.green() if is_ok else discord.Color.orange(),
        timestamp=outcome.checked_at,
    )
    embed.add_field(name="Tài khoản", value=f"`{email_val}`", inline=False)
    embed.add_field(name="Mật khẩu", value=f"`{pass_val}`", inline=False)
    embed.add_field(name="Số lượng game", value=f"`{outcome.extracted_app_count}`", inline=True)
    embed.add_field(name="Thay đổi mới", value="`Có`" if outcome.changed else "`Không`", inline=True)
    embed.set_footer(text=f"AppVault • ID #{outcome.source_id}")
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

    @discord.ui.button(label="Trước", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Chỉ người gọi lệnh mới có thể chuyển trang.",
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

    @discord.ui.button(label="Sau", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Chỉ người gọi lệnh mới có thể chuyển trang.",
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
