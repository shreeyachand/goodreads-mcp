"""Offline tests for server-level integration of shared helpers."""

from __future__ import annotations

import json

from goodreads_mcp import server


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
