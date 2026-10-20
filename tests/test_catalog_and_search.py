"""Tests for app normalization, deduplication, seed idempotency, and multi-strategy search."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import App, AppAlias, ChangeEvent, DiscordSubscription, Source, SourceApp
from app.services.catalog import (
    SEED_SOURCES,
    ensure_app_and_aliases,
    normalize_app_name,
    normalize_raw_title,
    reload_seed_catalog,
)
from app.services.search import find_best_matching_app, search_apps
from app.services.sources import upsert_discord_subscription


def test_app_normalization_and_spelling_correction() -> None:
    """Verify normalization strips trademarks/punctuation and fixes typos only in normalized fields."""
    assert normalize_raw_title("Subway subfers") == "subway subfers"
    assert normalize_app_name("Subway subfers") == "subway surfers"

    assert normalize_raw_title("Plan vs Zombile 2") == "plan vs zombile 2"
    assert normalize_app_name("Plan vs Zombile 2") == "plants vs zombies 2"

    assert normalize_raw_title("Guns’n’Glory Premmium") == "guns n glory premmium"
    assert normalize_app_name("Guns’n’Glory Premmium") == "guns n glory premium"

    assert normalize_app_name("Minecraft: Play with friends!") == "minecraft play with friends"
    assert normalize_app_name("Minecraft: Play with Friends") == "minecraft play with friends"
    assert normalize_app_name("GRID™ Legends: Deluxe Edition") == "grid legends deluxe edition"


def test_app_deduplication_across_sources(db_session: Session) -> None:
    """Verify identical normalized names deduplicate into one App while preserving original titles."""
    app1, created1, _ = ensure_app_and_aliases(db_session, "Minecraft: Play with friends!")
    app2, created2, _ = ensure_app_and_aliases(db_session, "Minecraft: Play with Friends")
    db_session.commit()

    assert created1 is True
    assert created2 is False
    assert app1.id == app2.id
    assert app1.normalized_name == "minecraft play with friends"


def test_seed_import_idempotency_and_original_title_preservation(db_session: Session) -> None:
    """Verify seed importer loads all 5 sources (67 links, 56 unique apps) idempotently without losing state."""
    first_run = reload_seed_catalog(db_session)
    db_session.commit()

    assert first_run.sources_created == 5
    assert first_run.apps_created == 56
    assert first_run.links_created == 67

    # Verify original supplied titles and ordering on Source 19 are strictly preserved
    src19_links = db_session.scalars(
        select(SourceApp)
        .where(SourceApp.source_id == 19)
        .order_by(SourceApp.display_order.asc())
    ).all()
    assert [lk.original_title for lk in src19_links] == list(SEED_SOURCES[0]["apps"])
    assert src19_links[1].original_title == "Subway subfers"
    assert src19_links[4].original_title == "Plan vs Zombile 2"
    assert src19_links[6].original_title == "Guns’n’Glory Premmium"
    assert all(lk.data_origin == "seed" for lk in src19_links)

    # Add a subscription and a change event to verify reload does not erase them
    upsert_discord_subscription(db_session, guild_id="111", channel_id="222")
    db_session.add(
        ChangeEvent(
            source_id=19,
            event_type="apps_updated",
            previous_hash=None,
            current_hash="abc123hash",
            summary="Synthetic change event",
            notification_status="sent",
        )
    )
    db_session.commit()

    # Run seed importer a second time
    second_run = reload_seed_catalog(db_session)
    db_session.commit()

    assert second_run.sources_created == 0
    assert second_run.sources_updated == 5
    assert second_run.apps_created == 0
    assert second_run.links_created == 0

    assert db_session.scalar(select(func.count(Source.id))) == 5
    assert db_session.scalar(select(func.count(App.id))) == 56
    assert db_session.scalar(select(func.count(SourceApp.source_id))) == 67
    assert db_session.scalar(select(func.count(DiscordSubscription.id))) == 1
    assert db_session.scalar(select(func.count(ChangeEvent.id))) == 1
    assert db_session.scalar(select(func.count(AppAlias.id))) > 10


def test_search_exact_alias_partial_and_fuzzy(db_session: Session) -> None:
    """Verify exact, case-insensitive, alias, partial, and fuzzy search ranking."""
    reload_seed_catalog(db_session)
    db_session.commit()

    # 1. Exact match
    res_exact = search_apps(db_session, "Balatro")
    assert res_exact.total >= 1
    assert res_exact.items[0].app.canonical_name == "Balatro"
    assert res_exact.items[0].match_type == "exact"
    assert 15 in res_exact.items[0].app.source_ids

    # 2. Case-insensitive match across multiple sources (Sources 1, 19, 20)
    res_mc = search_apps(db_session, "minecraft: play with friends")
    assert res_mc.total >= 1
    assert set(res_mc.items[0].app.source_ids) == {1, 19, 20}

    # 3. Spelling-corrected / alias search ("Subway Surfers" finds "Subway subfers")
    res_subway = search_apps(db_session, "Subway Surfers")
    assert res_subway.total >= 1
    assert res_subway.items[0].app.normalized_name == "subway surfers"
    assert set(res_subway.items[0].app.source_ids) == {19, 20}

    # 4. Original misspelled query ("Plan vs Zombile 2") and abbreviation ("PvZ 2") both work
    res_pvz_orig = search_apps(db_session, "Plan vs Zombile 2")
    res_pvz_alias = search_apps(db_session, "PvZ 2")
    assert res_pvz_orig.total >= 1
    assert res_pvz_alias.total >= 1
    assert res_pvz_orig.items[0].app.id == res_pvz_alias.items[0].app.id

    # 5. Partial substring match ("Kingdom Rush" matches both Kingdom Rush games on Source 5)
    res_partial = search_apps(db_session, "Kingdom Rush")
    assert res_partial.total >= 2
    found_names = {item.app.canonical_name for item in res_partial.items}
    assert "Kingdom Rush Frontiers TD" in found_names
    assert "Kingdom Rush Vengeance TD Game" in found_names

    # 6. Fuzzy match ("Balarto" or "LumaFuzion")
    res_fuzzy = search_apps(db_session, "LumaFuzion")
    assert res_fuzzy.total >= 1
    assert res_fuzzy.items[0].app.canonical_name == "LumaFusion"
    assert res_fuzzy.items[0].match_type == "fuzzy"

    # 7. Single best match lookup for /app command
    app_detail = find_best_matching_app(db_session, "KOTOR")
    assert app_detail is not None
    assert "KOTOR" in app_detail.canonical_name
