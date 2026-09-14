"""Small dependency-free TTL cache helpers.

Goodreads' public endpoints are unofficial, so avoiding repeated requests is
both faster for callers and more considerate of the service. Values expire
after a short interval and the cache is bounded to prevent unbounded process
growth.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from functools import wraps
from threading import RLock
from time import monotonic
from typing import Any, ParamSpec, TypeVar, cast

P = ParamSpec("P")
R = TypeVar("R")


class TTLCache:
    """A bounded, thread-safe cache whose entries expire after a fixed TTL."""

    def __init__(
        self,
        maxsize: int,
        ttl_seconds: float,
        *,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1.")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0.")
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._timer = timer
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = RLock()

    def get_or_set(self, key: Hashable, factory: Callable[[], R]) -> R:
        """Return a fresh cached value or compute and store it.

        The factory runs outside the lock because it may perform network I/O.
        Exceptions are never cached.
        """
        now = self._timer()
        with self._lock:
            cached = self._items.get(key)
            if cached is not None:
                expires_at, value = cached
                if expires_at > now:
                    self._items.move_to_end(key)
                    return cast(R, value)
                del self._items[key]

        value = factory()
        with self._lock:
            self._items[key] = (self._timer() + self.ttl_seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def ttl_cache(*, maxsize: int, ttl_seconds: float):
    """Decorate a function with a small TTL/LRU cache.

    The decorated functions in this project accept hashable scalar arguments.
    A ``cache_clear`` attribute is exposed for deterministic tests and manual
    invalidation.
    """
    cache = TTLCache(maxsize=maxsize, ttl_seconds=ttl_seconds)

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            key = (args, tuple(sorted(kwargs.items())))
            return cache.get_or_set(key, lambda: func(*args, **kwargs))

        wrapped.cache_clear = cache.clear  # type: ignore[attr-defined]
        wrapped.cache_instance = cache  # type: ignore[attr-defined]
        return wrapped

    return decorate
