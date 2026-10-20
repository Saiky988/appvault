"""Tests for FastAPI public/admin endpoints, pagination, authentication, and Discord slash commands."""
from __future__ import annotations

import discord
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from app.bot.client import AppVaultBot
from app.core.config import get_settings
from app.services.catalog import reload_seed_catalog
from tests.conftest import TEST_ADMIN_TOKEN


def test_public_api_endpoints_and_pagination(api_client: TestClient, db_session: Session) -> None:
    """Verify all public /api/v1 endpoints return expected schemas and support pagination."""
    reload_seed_catalog(db_session)
    db_session.commit()

    # 1. Health & Stats
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

    # 2. Apps pagination
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

    # 3. App search & detail
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

    # 4. Sources & Source Apps
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

    # 5. Updates
    r_updates = api_client.get("/api/v1/updates")
    assert r_updates.status_code == 200
    assert "items" in r_updates.json()


def test_admin_api_authentication_and_actions(api_client: TestClient, db_session: Session) -> None:
    """Verify admin endpoints reject missing/invalid tokens and execute pause/resume/reload when authorized."""
    reload_seed_catalog(db_session)
    db_session.commit()

    # Missing token -> 401
    r_no_auth = api_client.post("/api/v1/admin/pause")
    assert r_no_auth.status_code == 401

    # Invalid token -> 403
    r_bad_auth = api_client.post(
        "/api/v1/admin/pause",
        headers={"Authorization": "Bearer wrong-token-value"},
    )
    assert r_bad_auth.status_code == 403
    assert TEST_ADMIN_TOKEN not in r_bad_auth.text

    auth_headers = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}

    # Valid pause & resume
    r_pause = api_client.post("/api/v1/admin/pause", headers=auth_headers)
    assert r_pause.status_code == 200
    assert r_pause.json()["is_paused"] is True

    r_resume = api_client.post("/api/v1/admin/resume", headers=auth_headers)
    assert r_resume.status_code == 200
    assert r_resume.json()["is_paused"] is False

    # Valid catalog reload
    r_reload = api_client.post("/api/v1/admin/catalog/reload", headers=auth_headers)
    assert r_reload.status_code == 200
    assert r_reload.json()["sources_updated"] == 5


@pytest.mark.asyncio
async def test_discord_slash_commands_execution(db_session: Session) -> None:
    """Verify Discord bot registers all required slash commands and executes them with safe embeds."""
    reload_seed_catalog(db_session)
    db_session.commit()

    bot = AppVaultBot(settings=get_settings())
    cmd_names = {c.name for c in bot.tree.get_commands()}
    assert {
        "search",
        "app",
        "catalog",
        "source",
        "updates",
        "subscribe",
        "unsubscribe",
        "status",
        "check",
        "admin",
    }.issubset(cmd_names)

    # Execute /search
    ix_search = discord.Interaction(user_id=1001, guild_id=2001, channel_id=3001)
    search_cmd = bot.tree.get_command("search")
    assert search_cmd is not None
    await search_cmd.callback(ix_search, "Balatro")
    assert len(ix_search.response.messages) == 1
    embed = ix_search.response.messages[0]["embed"]
    assert embed is not None
    assert "Balatro" in embed.fields[0]["name"]

    # Execute /subscribe with Manage Channels permission
    ix_sub = discord.Interaction(
        user_id=1001,
        guild_id=2001,
        channel_id=3001,
        permissions=discord.Permissions(manage_channels=True),
    )
    sub_cmd = bot.tree.get_command("subscribe")
    target_ch = discord.TextChannel(id=3001, name="updates", guild_id=2001)
    await sub_cmd.callback(ix_sub, target_ch)
    assert "Subscribed" in (ix_sub.response.messages[0]["content"] or "")

    # Unauthorized user trying /admin pause is rejected
    admin_group = bot.tree.get_command("admin")
    pause_cmd = {c.name: c for c in admin_group.commands}["pause"]
    ix_unauth = discord.Interaction(user_id=5555, guild_id=2001, channel_id=3001)
    await pause_cmd.callback(ix_unauth)
    assert "Administrator authorization required" in (ix_unauth.response.messages[0]["content"] or "")

    # Authorized admin (user_id=9001 from fixture) running /admin pause succeeds
    ix_admin = discord.Interaction(user_id=9001, guild_id=2001, channel_id=3001)
    await pause_cmd.callback(ix_admin)
    assert "paused" in (ix_admin.response.messages[0]["content"] or "").lower()
