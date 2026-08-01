"""Offline tests for tool behavior around pagination and rich book data."""

from __future__ import annotations

import json
from typing import Any

import pytest

from goodreads_mcp import server


def test_paginated_graphql_edges_follows_tokens(monkeypatch):
    calls: list[dict[str, Any]] = []
    pages = [
        {
            "totalCount": 4,
            "edges": [{"node": {"legacyId": 1}}, {"node": {"legacyId": 2}}],
            "pageInfo": {"hasNextPage": True, "nextPageToken": "page-2"},
        },
        {
            "totalCount": 4,
            "edges": [{"node": {"legacyId": 3}}, {"node": {"legacyId": 4}}],
            "pageInfo": {"hasNextPage": False, "nextPageToken": None},
        },
    ]

    def graphql(query, variables):
        calls.append(variables)
        return {"connection": pages[len(calls) - 1]}

    monkeypatch.setattr(server.gr, "graphql", graphql)
    edges, has_more, total = server._paginated_graphql_edges(
        "query", "connection", {"id": "book"}, 4
    )

    assert [edge["node"]["legacyId"] for edge in edges] == [1, 2, 3, 4]
    assert calls[0]["pagination"] == {"limit": 4}
    assert calls[1]["pagination"] == {"limit": 2, "after": "page-2"}
    assert has_more is False
    assert total == 4


def test_paginated_graphql_edges_reports_more_when_limit_reached(monkeypatch):
    def graphql(query, variables):
        return {
            "connection": {
                "edges": [{"node": {"legacyId": 1}}],
                "pageInfo": {"hasNextPage": True, "nextPageToken": "next"},
            }
        }

    monkeypatch.setattr(server.gr, "graphql", graphql)
    edges, has_more, total = server._paginated_graphql_edges(
        "query", "connection", {}, 1
    )

    assert len(edges) == 1
    assert has_more is True
    assert total is None


def test_paginated_graphql_edges_uses_bounded_page_sizes(monkeypatch):
    calls: list[dict[str, Any]] = []

    def graphql(query, variables):
        calls.append(variables)
        page_size = variables["pagination"]["limit"]
        return {
            "connection": {
                "edges": [{"node": {"legacyId": i}} for i in range(page_size)],
                "pageInfo": {
                    "hasNextPage": len(calls) == 1,
                    "nextPageToken": "page-2" if len(calls) == 1 else None,
                },
            }
        }

    monkeypatch.setattr(server.gr, "graphql", graphql)
    edges, has_more, _ = server._paginated_graphql_edges("query", "connection", {}, 35)

    assert len(edges) == 35
    assert [call["pagination"]["limit"] for call in calls] == [20, 15]
    assert calls[1]["pagination"]["after"] == "page-2"
    assert has_more is False


def test_paginated_graphql_edges_rejects_negative_limit():
    with pytest.raises(ValueError, match="zero or greater"):
        server._paginated_graphql_edges("query", "connection", {}, -1)


def test_get_book_exposes_all_series_and_richer_fields(monkeypatch):
    apollo = {
        "Book:1": {
            "legacyId": 1,
            "title": "A Book",
            "titleComplete": "A Book: Complete",
            "imageUrl": "https://images.example/cover.jpg",
            "description": "<p>A description.</p>",
            "primaryContributorEdge": {"node": {"name": "An Author"}},
            "work": {
                "stats": {
                    "averageRating": 4.1,
                    "ratingsCount": 10,
                    "ratingsCountDist": [1, 1, 2, 3, 3],
                    "textReviewsCount": 3,
                    "textReviewsLanguageCounts": [
                        {"isoLanguageCode": "spa", "count": 2},
                        {"isoLanguageCode": "eng", "count": 10},
                        {"isoLanguageCode": "fra", "count": 1},
                    ],
                }
            },
            "details": {"publicationTime": 1600396415413},
            "bookSeries": [
                {"userPosition": "1", "series": {"title": "Main Series"}},
                {"userPosition": "2", "series": {"title": "Shared World"}},
            ],
        }
    }
    monkeypatch.setattr(server, "_fetch_book_apollo", lambda book_id: apollo)

    book = server.get_book("1", review_language_limit=2)

    assert book["cover"] == "https://images.example/cover.jpg"
    assert book["publication_date"] == "2020-09-18"
    assert book["series"] == "Main Series"
    assert book["series_memberships"] == [
        {"series": "Main Series", "position": "1"},
        {"series": "Shared World", "position": "2"},
    ]
    assert book["review_languages"] == {"eng": 10, "spa": 2}


def test_get_book_rejects_negative_language_limit(monkeypatch):
    apollo = {"Book:1": {"legacyId": 1, "title": "A Book"}}
    monkeypatch.setattr(server, "_fetch_book_apollo", lambda book_id: apollo)

    with pytest.raises(ValueError, match="zero or greater"):
        server.get_book("1", review_language_limit=-1)


def test_series_books_can_select_a_secondary_series(monkeypatch):
    resolved = {
        "legacy_id": 1,
        "title": "A Book",
        "series_memberships": [
            {"id": "series-1", "title": "Main Series", "position": "1"},
            {"id": "series-2", "title": "Shared World", "position": "2"},
        ],
    }
    captured: dict[str, Any] = {}

    def paginate(query, connection_name, variables, limit):
        captured.update(variables)
        return [], False, None

    monkeypatch.setattr(server, "_resolve_book_ids", lambda book_id: resolved)
    monkeypatch.setattr(server, "_paginated_graphql_edges", paginate)

    result = server.series_books("1", series_index=1)

    assert captured["input"] == {"id": "series-2"}
    assert result["series"] == "Shared World"
    assert result["series_index"] == 1


def test_series_books_rejects_unknown_series_index(monkeypatch):
    resolved = {
        "legacy_id": 1,
        "title": "A Book",
        "series_memberships": [
            {"id": "series-1", "title": "Main Series", "position": "1"}
        ],
    }
    monkeypatch.setattr(server, "_resolve_book_ids", lambda book_id: resolved)

    with pytest.raises(ValueError, match="out of range"):
        server.series_books("1", series_index=1)


def test_book_apollo_lookup_uses_ttl_cache(monkeypatch):
    calls = 0
    payload = {"props": {"pageProps": {"apolloState": {"Book:1": {"title": "Cached"}}}}}
    page = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )

    class Response:
        text = page

    def get(url):
        nonlocal calls
        calls += 1
        return Response()

    server._fetch_book_apollo.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(server.gr, "get", get)
    try:
        assert server._fetch_book_apollo("1")["Book:1"]["title"] == "Cached"
        assert server._fetch_book_apollo("1")["Book:1"]["title"] == "Cached"
        assert calls == 1
    finally:
        server._fetch_book_apollo.cache_clear()  # type: ignore[attr-defined]
