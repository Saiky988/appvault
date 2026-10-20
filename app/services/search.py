"""Multi-strategy app search service: exact, case-insensitive, alias, partial, and fuzzy matching."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import App, AppAlias, SourceApp
from app.schemas.api import (
    AppDetailSchema,
    AppSearchResponseSchema,
    AppSearchResultItemSchema,
)
from app.services.catalog import (
    build_app_summary,
    get_app_detail,
    normalize_app_name,
    normalize_raw_title,
)

MAX_SEARCH_RESULTS = 50
FUZZY_MIN_THRESHOLD = 0.62


def _compute_fuzzy_similarity(query_norm: str, candidate_norm: str) -> float:
    """Compute bounded fuzzy similarity score between two normalized strings."""
    if not query_norm or not candidate_norm:
        return 0.0
    full_ratio = SequenceMatcher(None, query_norm, candidate_norm).ratio()

    # Also check token-level best similarity so single-word typos match multi-word game titles
    q_tokens = query_norm.split()
    c_tokens = candidate_norm.split()
    if not q_tokens or not c_tokens:
        return full_ratio

    token_scores: List[float] = []
    for qt in q_tokens:
        best_tok = max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens)
        token_scores.append(best_tok)
    avg_tok_ratio = sum(token_scores) / len(token_scores)
    return max(full_ratio, avg_tok_ratio * 0.92)


def search_apps(
    session: Session,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
    max_results: int = MAX_SEARCH_RESULTS,
) -> AppSearchResponseSchema:
    """Search across canonical names, original source titles, and aliases with ranked scoring."""
    raw_query = (query or "").strip()
    norm_query = normalize_app_name(raw_query, apply_spelling_fixes=True)
    raw_norm_query = normalize_raw_title(raw_query)

    if not raw_query or not (norm_query or raw_norm_query):
        return AppSearchResponseSchema(
            query=raw_query,
            normalized_query=norm_query,
            total=0,
            limit=limit,
            offset=offset,
            items=[],
        )

    apps = session.scalars(select(App)).all()
    all_aliases = session.scalars(select(AppAlias)).all()
    all_links = session.scalars(
        select(SourceApp).where(SourceApp.is_active == True)  # noqa: E712
    ).all()

    aliases_by_app: Dict[int, List[AppAlias]] = {}
    for al in all_aliases:
        aliases_by_app.setdefault(al.app_id, []).append(al)

    titles_by_app: Dict[int, List[str]] = {}
    for link in all_links:
        titles_by_app.setdefault(link.app_id, []).append(link.original_title)

    ranked_matches: List[Tuple[float, int, App, str, str]] = []
    # Priority tier order: 5=exact, 4=case_insensitive, 3=alias, 2=partial, 1=fuzzy
    for app in apps:
        best_tier = 0
        best_score = 0.0
        best_type = ""
        matched_on = app.canonical_name

        # 1. Exact & Case-insensitive checks on canonical name or original source titles
        candidate_titles = [app.canonical_name] + titles_by_app.get(app.id, [])
        for title in candidate_titles:
            if title == raw_query:
                best_tier = 5
                best_score = 1.0
                best_type = "exact"
                matched_on = title
                break
            if title.lower() == raw_query.lower():
                if best_tier < 4:
                    best_tier = 4
                    best_score = 0.99
                    best_type = "case_insensitive"
                    matched_on = title

        if best_tier < 4 and app.normalized_name in (norm_query, raw_norm_query):
            if norm_query != raw_norm_query and app.normalized_name == norm_query:
                best_tier = 3
                best_score = 0.96
                best_type = "alias"
                matched_on = app.canonical_name
            else:
                best_tier = 4
                best_score = 0.98
                best_type = "case_insensitive"
                matched_on = app.canonical_name

        # 2. Alias exact or prefix/partial match
        app_aliases = aliases_by_app.get(app.id, [])
        if best_tier < 3:
            for al in app_aliases:
                if al.normalized_alias in (norm_query, raw_norm_query) or al.alias.lower() == raw_query.lower():
                    best_tier = 3
                    best_score = 0.95
                    best_type = "alias"
                    matched_on = al.alias
                    break

        # 3. Partial substring or token match on canonical, original titles, or aliases
        if best_tier < 2:
            for q_cand in {norm_query, raw_norm_query}:
                if not q_cand:
                    continue
                if q_cand in app.normalized_name:
                    pos_bonus = 0.05 if app.normalized_name.startswith(q_cand) else 0.0
                    length_ratio = min(len(q_cand) / max(len(app.normalized_name), 1), 1.0)
                    best_tier = 2
                    best_score = round(0.80 + 0.10 * length_ratio + pos_bonus, 4)
                    best_type = "partial"
                    matched_on = app.canonical_name
                    break

            if best_tier < 2:
                for al in app_aliases:
                    for q_cand in {norm_query, raw_norm_query}:
                        if q_cand and q_cand in al.normalized_alias:
                            best_tier = 2
                            best_score = 0.78
                            best_type = "alias"
                            matched_on = al.alias
                            break
                    if best_tier == 2:
                        break

        # 4. Fuzzy match
        if best_tier == 0:
            sim_main = max(
                _compute_fuzzy_similarity(norm_query, app.normalized_name),
                _compute_fuzzy_similarity(raw_norm_query, app.normalized_name),
            )
            best_alias_sim = 0.0
            best_alias_text = app.canonical_name
            for al in app_aliases:
                s = max(
                    _compute_fuzzy_similarity(norm_query, al.normalized_alias),
                    _compute_fuzzy_similarity(raw_norm_query, al.normalized_alias),
                )
                if s > best_alias_sim:
                    best_alias_sim = s
                    best_alias_text = al.alias

            top_sim = max(sim_main, best_alias_sim)
            if top_sim >= FUZZY_MIN_THRESHOLD:
                best_tier = 1
                best_score = round(min(top_sim * 0.85, 0.77), 4)
                best_type = "fuzzy"
                matched_on = app.canonical_name if sim_main >= best_alias_sim else best_alias_text

        if best_tier > 0:
            ranked_matches.append((best_score, best_tier, app, best_type, matched_on))

    ranked_matches.sort(key=lambda item: (-item[1], -item[0], item[2].canonical_name.lower()))
    capped_matches = ranked_matches[:max_results]
    total = len(capped_matches)
    sliced = capped_matches[offset : offset + limit]

    items = [
        AppSearchResultItemSchema(
            app=build_app_summary(session, m_app),
            match_type=m_type,
            score=m_score,
            matched_on=m_matched_on,
        )
        for m_score, _tier, m_app, m_type, m_matched_on in sliced
    ]

    return AppSearchResponseSchema(
        query=raw_query,
        normalized_query=norm_query,
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def find_best_matching_app(session: Session, name_query: str) -> Optional[AppDetailSchema]:
    """Resolve a single best-matching App for `/app name:<text>`."""
    results = search_apps(session, name_query, limit=1, offset=0)
    if not results.items:
        return None
    top_app_id = results.items[0].app.id
    return get_app_detail(session, top_app_id)
