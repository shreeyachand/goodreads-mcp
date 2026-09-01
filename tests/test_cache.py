"""Unit tests for the bounded TTL cache."""

from __future__ import annotations

import pytest

from goodreads_mcp.cache import TTLCache, ttl_cache


def test_ttl_cache_reuses_then_expires_value():
    now = [10.0]
    cache = TTLCache(maxsize=2, ttl_seconds=5, timer=lambda: now[0])
    calls = 0

    def make_value():
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    assert cache.get_or_set("book", make_value) == "value-1"
    assert cache.get_or_set("book", make_value) == "value-1"
    assert calls == 1

    now[0] = 15.0
    assert cache.get_or_set("book", make_value) == "value-2"
    assert calls == 2


def test_ttl_cache_evicts_least_recently_used_entry():
    cache = TTLCache(maxsize=2, ttl_seconds=60, timer=lambda: 0)
    cache.get_or_set("a", lambda: "A")
    cache.get_or_set("b", lambda: "B")
    cache.get_or_set("a", lambda: "unused")  # a is now most recently used
    cache.get_or_set("c", lambda: "C")

    assert cache.get_or_set("a", lambda: "new-A") == "A"
    assert cache.get_or_set("b", lambda: "new-B") == "new-B"


def test_ttl_cache_does_not_cache_exceptions():
    cache = TTLCache(maxsize=2, ttl_seconds=60)
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary failure")

    with pytest.raises(RuntimeError):
        cache.get_or_set("book", fail)
    with pytest.raises(RuntimeError):
        cache.get_or_set("book", fail)
    assert calls == 2
    assert len(cache) == 0


def test_ttl_cache_decorator_can_be_cleared():
    calls = 0

    @ttl_cache(maxsize=2, ttl_seconds=60)
    def lookup(book_id: str) -> str:
        nonlocal calls
        calls += 1
        return f"{book_id}-{calls}"

    assert lookup("1") == "1-1"
    assert lookup("1") == "1-1"
    lookup.cache_clear()  # type: ignore[attr-defined]
    assert lookup("1") == "1-2"
