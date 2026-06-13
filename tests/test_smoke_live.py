"""Opt-in live smoke tests against real Goodreads endpoints.

These hit the network and can break when Goodreads changes its markup or
WAF posture — exactly the failures worth catching. They're skipped unless
GOODREADS_LIVE=1 so the default test run stays offline and deterministic.

    GOODREADS_LIVE=1 pytest tests/test_smoke_live.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("GOODREADS_LIVE") != "1",
    reason="set GOODREADS_LIVE=1 to run live network smoke tests",
)


def test_search_books_live():
    from goodreads_mcp import server

    results = server.search_books("project hail mary", max_results=3)
    assert results, "autocomplete returned nothing"
    top = results[0]
    assert top["book_id"]
    assert top["title"]


def test_get_book_live_bypasses_waf():
    """Regression guard: the plain book page is WAF-gated; .xml must work."""
    from goodreads_mcp import server

    book = server.get_book("54493401")
    assert book["title"] == "Project Hail Mary"
    assert book["author"]
    assert book["genres"]


def test_get_book_accepts_slug_form_live():
    from goodreads_mcp import server

    book = server.get_book("11870085-the-fault-in-our-stars")
    assert book["book_id"] == 11870085


def test_get_book_histogram_and_languages_live():
    from goodreads_mcp import server

    book = server.get_book("54493401")
    hist = book["ratings_histogram"]
    assert set(hist) == {"1", "2", "3", "4", "5"}
    assert all(isinstance(v, int) for v in hist.values())
    # most books skew positive: 5-star count should dominate 1-star
    assert hist["5"] > hist["1"]
    assert book["text_reviews_count"]
    assert book["review_languages"]


def test_get_book_series_position_live():
    from goodreads_mcp import server

    book = server.get_book("2767052")  # The Hunger Games, book 1
    assert book["series"]
    assert book["series_position"] == "1"


def test_graphql_config_resolves_live():
    from goodreads_mcp.client import GoodreadsClient

    endpoint, key = GoodreadsClient().graphql_config(force=True)
    assert "appsync-api" in endpoint and endpoint.endswith("/graphql")
    assert key.startswith("da2-")


def test_get_reviews_live():
    from goodreads_mcp import server

    res = server.get_reviews("54493401", limit=5)
    assert res["returned"] == 5
    assert res["total_text_reviews"] and res["total_text_reviews"] > 1000
    for r in res["reviews"]:
        assert r["reviewer"]
        assert 1 <= r["rating"] <= 5
        assert r["text"] and "<" not in r["text"]  # prose, HTML stripped
    # citation links (most reviews have one; deleted sub-resources -> null)
    urls = [r["url"] for r in res["reviews"] if r["url"]]
    assert urls
    assert all(u.startswith("https://www.goodreads.com/review/show/") for u in urls)


def test_get_reviews_paginates_past_30_live():
    """The whole point of the GraphQL backbone: more than one page."""
    from goodreads_mcp import server

    res = server.get_reviews("54493401", limit=45)
    assert res["returned"] == 45


def test_get_reviews_rating_filters_live():
    from goodreads_mcp import server

    # positive
    pos = server.get_reviews(
        "2767052", limit=20, min_rating=5, exclude_spoilers=True
    )
    assert all(r["rating"] == 5 for r in pos["reviews"])
    assert all(r["spoiler"] is False for r in pos["reviews"])
    # critical
    crit = server.get_reviews("54493401", limit=8, max_rating=2)
    assert crit["reviews"]
    assert all(r["rating"] <= 2 for r in crit["reviews"])


# --------------------------------------------------- discovery (Tier 2/3)


def test_similar_books_live():
    from goodreads_mcp import server

    res = server.similar_books("2767052", limit=5)
    assert len(res["similar"]) == 5
    b = res["similar"][0]
    assert b["book_id"] and b["title"] and b["author"]
    assert b["url"].startswith("https://www.goodreads.com/book/show/")


def test_author_books_live():
    from goodreads_mcp import server

    res = server.author_books("2767052", limit=5)  # Suzanne Collins
    assert res["author"] == "Suzanne Collins"
    assert res["total_works"] and res["total_works"] > 1
    titles = [w["title"] for w in res["works"]]
    assert "The Hunger Games" in titles


def test_series_books_live():
    from goodreads_mcp import server

    res = server.series_books("2767052", limit=10)
    assert res["series"] == "The Hunger Games"
    # the main-sequence book 1 should be present and flagged primary
    main = [b for b in res["books"] if b["placement"] == "1"]
    assert main and main[0]["is_primary"] is True
    assert main[0]["title"] == "The Hunger Games"


def test_series_books_standalone_returns_note_live():
    from goodreads_mcp import server

    res = server.series_books("54493401")  # Project Hail Mary, standalone
    assert res["series"] is None
    assert res["books"] == []


def test_get_editions_live():
    from goodreads_mcp import server

    res = server.get_editions("2767052", limit=5)
    assert res["total_editions"] and res["total_editions"] > 1
    assert len(res["editions"]) == 5
    e = res["editions"][0]
    assert e["format"]
    assert e["url"].startswith("https://www.goodreads.com/book/show/")


def test_get_shelf_rss_live():
    from goodreads_mcp import server

    # user 1 (Otis Chandler, GR founder) has a large public 'read' shelf
    items = server.get_shelf("read", user_id="1", page=1)
    assert items, "shelf RSS returned no items"
    assert items[0]["title"]
    assert items[0]["book_id"]
