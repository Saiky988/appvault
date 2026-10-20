"""Tests for FastAPI endpoints, pagination, authentication, and Discord slash commands with autocomplete."""
from __future__ import annotations

import discord
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.bot.client import AppVaultBot
from app.bot.commands.slash import (
    get_game_autocomplete_choices,
    get_source_id_autocomplete_choices,
)
from app.core.config import get_settings
from app.db.models import Source
from app.services.catalog import reload_seed_catalog
from app.services.search import find_best_matching_app, search_apps
from app.services.sources import list_source_apps_paginated
from tests.conftest import TEST_ADMIN_TOKEN


def test_public_api_endpoints_and_pagination(api_client: TestClient, db_session: Session) -> None:
    """Verify all public /api/v1 endpoints return expected schemas and support pagination."""
    reload_seed_catalog(db_session)
    db_session.commit()

    r_health = api_client.get("/api/v1/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "ok"
    assert r_health.json()["database"] == "connected"

    r_stats = api_client.get("/api/v1/stats")
    assert r_stats.status_code == 200
    stats_data = r_stats.json()
    assert stats_data["total_sources"] == 5
    assert stats_data["total_apps"] == 56
    assert stats_data["total_source_app_links"] == 67

    r_apps_p1 = api_client.get("/api/v1/apps?limit=10&offset=0")
    r_apps_p2 = api_client.get("/api/v1/apps?limit=10&offset=10")
    assert r_apps_p1.status_code == 200
    assert r_apps_p2.status_code == 200
    p1_data = r_apps_p1.json()
    p2_data = r_apps_p2.json()
    assert p1_data["total"] == 56
    assert len(p1_data["items"]) == 10
    assert len(p2_data["items"]) == 10
    assert p1_data["items"][0]["id"] != p2_data["items"][0]["id"]

    r_search = api_client.get("/api/v1/apps/search", params={"q": "Subway Surfers"})
    assert r_search.status_code == 200
    s_data = r_search.json()
    assert s_data["total"] >= 1
    found_app_id = s_data["items"][0]["app"]["id"]

    r_app_detail = api_client.get(f"/api/v1/apps/{found_app_id}")
    assert r_app_detail.status_code == 200
    assert r_app_detail.json()["normalized_name"] == "subway surfers"

    r_app_404 = api_client.get("/api/v1/apps/999999")
    assert r_app_404.status_code == 404

    r_sources = api_client.get("/api/v1/sources")
    assert r_sources.status_code == 200
    assert r_sources.json()["total"] == 5

    r_src19 = api_client.get("/api/v1/sources/19")
    assert r_src19.status_code == 200
    assert r_src19.json()["id"] == 19
    assert r_src19.json()["app_count"] == 10

    r_src19_apps = api_client.get("/api/v1/sources/19/apps?limit=5&offset=0")
    assert r_src19_apps.status_code == 200
    assert r_src19_apps.json()["total"] == 10
    assert len(r_src19_apps.json()["items"]) == 5
    assert r_src19_apps.json()["items"][1]["original_title"] == "Subway subfers"

    r_updates = api_client.get("/api/v1/updates")
    assert r_updates.status_code == 200
    assert "items" in r_updates.json()


def test_admin_api_authentication_and_actions(api_client: TestClient, db_session: Session) -> None:
    """Verify admin endpoints reject missing/invalid tokens and execute pause/resume/reload when authorized."""
    reload_seed_catalog(db_session)
    db_session.commit()

    r_no_auth = api_client.post("/api/v1/admin/pause")
    assert r_no_auth.status_code == 401

    r_bad_auth = api_client.post(
        "/api/v1/admin/pause",
        headers={"Authorization": "Bearer wrong-token-value"},
    )
    assert r_bad_auth.status_code == 403
    assert TEST_ADMIN_TOKEN not in r_bad_auth.text

    auth_headers = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}

    r_pause = api_client.post("/api/v1/admin/pause", headers=auth_headers)
    assert r_pause.status_code == 200
    assert r_pause.json()["is_paused"] is True

    r_resume = api_client.post("/api/v1/admin/resume", headers=auth_headers)
    assert r_resume.status_code == 200
    assert r_resume.json()["is_paused"] is False

    r_reload = api_client.post("/api/v1/admin/catalog/reload", headers=auth_headers)
    assert r_reload.status_code == 200
    assert r_reload.json()["sources_updated"] == 5


def test_unified_source_relationships_and_autocomplete(db_session: Session) -> None:
    """Ensure /search, /app, /catalog, /source-apps, and autocomplete share identical relationships."""
    reload_seed_catalog(db_session)
    db_session.commit()

    initial_choices = get_game_autocomplete_choices("")
    assert len(initial_choices) == 25
    assert any("Minecraft" in c.name for c in initial_choices)

    typed_choices = get_game_autocomplete_choices("Mine")
    assert len(typed_choices) >= 1
    assert "Minecraft" in typed_choices[0].name
    assert "#1, #19, #20" in typed_choices[0].name

    id_choices = get_source_id_autocomplete_choices("Minecraft", "")
    id_vals = [c.value for c in id_choices]
    assert id_vals == ["Random", "1", "19", "20"]

    mc_search = search_apps(db_session, "Minecraft")
    assert mc_search.total >= 1
    assert mc_search.items[0].app.source_ids == [1, 19, 20]

    mc_detail = find_best_matching_app(db_session, "Minecraft")
    assert mc_detail is not None
    assert mc_detail.source_ids == [1, 19, 20]

    for sid in (1, 19, 20):
        src_apps = list_source_apps_paginated(db_session, sid, limit=100, offset=0)
        assert src_apps is not None
        assert any(item.app_id == mc_detail.id for item in src_apps.items)


@pytest.mark.asyncio
async def test_discord_slash_commands_app_credentials_and_game_subscribe(db_session: Session) -> None:
    """Verify concise /app embed with single-backtick mobile copy, /source-apps pagination, and /subscribe."""
    reload_seed_catalog(db_session)
    src20 = db_session.get(Source, 20)
    assert src20 is not None
    src20.account_email = "mc_id20@example.com"
    src20.account_password = "SecretPass20!"
    db_session.commit()

    bot = AppVaultBot(settings=get_settings())
    cmd_names = {c.name for c in bot.tree.get_commands()}
    assert {
        "search",
        "app",
        "source-apps",
        "catalog",
        "source",
        "updates",
        "subscribe",
        "unsubscribe",
        "status",
        "check",
        "admin",
    }.issubset(cmd_names)

    # 1. Execute /app name:Minecraft (Random mode picks Source 20 which has credentials)
    ix_app_rand = discord.Interaction(user_id=1001, guild_id=2001, channel_id=3001)
    app_cmd = bot.tree.get_command("app")
    assert app_cmd is not None
    await app_cmd.callback(ix_app_rand, "Minecraft", "Random")
    app_embed = ix_app_rand.response.messages[0]["embed"]
    assert app_embed is not None
    assert "ID #20" in (app_embed.title or "")
    assert app_embed.url is None
    assert app_embed.fields[0]["value"] == "`mc_id20@example.com`"
    assert app_embed.fields[1]["value"] == "`SecretPass20!`"
    assert "#1, #19, #20" in app_embed.fields[3]["value"]

    # 2. Execute /app name:Minecraft id:20 (Specific ID mode)
    ix_app_20 = discord.Interaction(user_id=1001, guild_id=2001, channel_id=3001)
    await app_cmd.callback(ix_app_20, "Minecraft", "20")
    app_embed_20 = ix_app_20.response.messages[0]["embed"]
    assert "ID #20" in (app_embed_20.title or "")
    assert app_embed_20.fields[0]["value"] == "`mc_id20@example.com`"

    # 3. Execute /source-apps for Source 5 (21 apps -> 3 pages at 10 per page)
    ix_sa = discord.Interaction(user_id=1001, guild_id=2001, channel_id=3001)
    sa_cmd = bot.tree.get_command("source-apps")
    await sa_cmd.callback(ix_sa, 5)
    sa_msg = ix_sa.response.messages[0]
    sa_embed_p1 = sa_msg["embed"]
    sa_view = sa_msg["view"]
    assert "ID #5 (21 Game)" in (sa_embed_p1.title or "")
    assert "Trang 1/3" in (sa_embed_p1.footer["text"] or "")

    next_btn = [c for c in sa_view.children if getattr(c, "custom_id", None) == "next_page"][0]
    await next_btn.callback(ix_sa, next_btn)
    sa_embed_p2 = ix_sa.response.messages[-1]["embed"]
    assert "Trang 2/3" in (sa_embed_p2.footer["text"] or "")

    # 4. Execute /subscribe with optional game="Minecraft"
    ix_sub = discord.Interaction(
        user_id=1001,
        guild_id=2001,
        channel_id=3001,
        permissions=discord.Permissions(manage_channels=True),
    )
    sub_cmd = bot.tree.get_command("subscribe")
    target_ch = discord.TextChannel(id=3001, name="updates", guild_id=2001)
    await sub_cmd.callback(ix_sub, target_ch, "Minecraft")
    sub_msg = ix_sub.response.messages[0]["content"] or ""
    assert "Minecraft" in sub_msg
    assert "#1, #19, #20" in sub_msg
