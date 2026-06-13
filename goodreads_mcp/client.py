"""Goodreads HTTP client (read-only).

Goodreads has had no public API since Dec 2020, so everything here rides
on three unofficial-but-stable read surfaces, in order of robustness:

  1. Shelf RSS feeds   — /review/list_rss/{user_id}?shelf=... (public
                          shelves; structured XML)
  2. Search autocomplete — /book/auto_complete?format=json&q=... (JSON)
  3. Embedded page JSON — book/giveaway pages are Next.js; __NEXT_DATA__
                          carries the full Apollo state. Parse that, not
                          the DOM.

No auth, no cookies, no writes — this server only reads public data.

House rules (these endpoints are unofficial; be a polite guest):
  * single client, persistent session
  * exponential backoff on 429/503
  * browser-faithful headers
  * detect AWS WAF JS challenges and fail loudly instead of feeding the
    challenge page to a parser
"""

from __future__ import annotations

import json
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://www.goodreads.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# --- AppSync GraphQL (Goodreads' backend) --------------------------------
# The web frontend ships a public, read-only API key in its JS bundle. We
# resolve it (and the prod endpoint) from the bundle at runtime so a key
# rotation self-heals; the hardcoded pair below is a last-resort fallback.
GRAPHQL_FALLBACK_ENDPOINT = (
    "https://kxbwmqov6jgg3daaamb744ycu4.appsync-api.us-east-1.amazonaws.com/graphql"
)
GRAPHQL_FALLBACK_KEY = "da2-xpgsdydkbregjhpr6ejzqdhuwy"

# An id-free Next.js page whose _app bundle carries the AppSync config.
CONFIG_DISCOVERY_PATH = "/giveaway"
APP_CHUNK_RE = re.compile(r'src="(/_next/static/chunks/pages/_app-[0-9a-f]+\.js)"')
# pattern in the bundle: "<api-key>","endpoint":"https://...appsync-api.../graphql"
APPSYNC_PAIR_RE = re.compile(
    r'"(da2-[a-z0-9]+)","endpoint":'
    r'"(https://[a-z0-9.\-]+\.appsync-api\.[a-z0-9.\-]+/graphql)"'
)


class WAFChallenge(Exception):
    """Raised when Goodreads returns an AWS WAF JS challenge instead of content.

    The challenge comes back as HTTP 202 with a tiny HTML body that only a
    real browser can solve, so raise_for_status() won't catch it. We detect
    the body and fail loudly rather than letting downstream parsers choke on
    the challenge markup.
    """


WAF_MARKERS = ("awsWafCookieDomainList", "challenge-container", "AwsWafIntegration")


def _is_waf_challenge(resp: httpx.Response) -> bool:
    if resp.status_code != 202:
        return False
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype:
        return False
    head = resp.text[:2048]
    return any(marker in head for marker in WAF_MARKERS)


class GraphQLError(Exception):
    """Raised when the GraphQL endpoint returns an `errors` array."""


def parse_appsync_config(bundle_js: str) -> tuple[str, str]:
    """Extract the (endpoint, api_key) for the *prod* environment from the
    _app JS bundle. The bundle embeds several environments; the prod one is
    the (key, endpoint) pair followed by "shortName":"Prod" before the next
    pair. Falls back to the first pair if prod can't be identified.
    """
    pairs = list(APPSYNC_PAIR_RE.finditer(bundle_js))
    if not pairs:
        raise ValueError("No AppSync (key, endpoint) pair found in bundle.")
    for i, m in enumerate(pairs):
        nxt = pairs[i + 1].start() if i + 1 < len(pairs) else len(bundle_js)
        if '"shortName":"Prod"' in bundle_js[m.end():nxt]:
            return m.group(2), m.group(1)
    return pairs[0].group(2), pairs[0].group(1)


@dataclass
class GoodreadsClient:
    max_retries: int = 3
    _client: httpx.Client | None = None
    _graphql_config: tuple[str, str] | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=BASE,
                headers=HEADERS,
                follow_redirects=True,
                timeout=30.0,
            )
        return self._client

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        """GET with backoff on 429/503."""
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            resp = self.client.request(method, url, **kw)
            if resp.status_code not in (429, 503) or attempt == self.max_retries:
                resp.raise_for_status()
                if _is_waf_challenge(resp):
                    raise WAFChallenge(
                        f"Goodreads returned a WAF JS challenge for {url!r} "
                        "(HTTP 202). This path can't be fetched without a real "
                        "browser; try an alternate endpoint (e.g. the .xml book "
                        "page, RSS feed, or JSON autocomplete)."
                    )
                return resp
            time.sleep(delay + random.uniform(0, 0.5))
            delay *= 2
        raise RuntimeError("unreachable")

    def get(self, url: str, **kw) -> httpx.Response:
        return self._request("GET", url, **kw)

    # ------------------------------------------------------------- graphql

    def graphql_config(self, force: bool = False) -> tuple[str, str]:
        """Resolve (endpoint, api_key) for the AppSync GraphQL backend.

        Scrapes the prod key/endpoint from the web app's _app JS bundle so a
        key rotation self-heals; cached per process. Falls back to a known
        hardcoded pair if discovery fails.
        """
        if self._graphql_config and not force:
            return self._graphql_config
        try:
            page = self.get(CONFIG_DISCOVERY_PATH).text
            m = APP_CHUNK_RE.search(page)
            if not m:
                raise ValueError("Could not locate _app JS bundle for config.")
            bundle = self.get(m.group(1)).text
            self._graphql_config = parse_appsync_config(bundle)
        except Exception:  # noqa: BLE001 — any failure -> use the fallback pair
            self._graphql_config = (GRAPHQL_FALLBACK_ENDPOINT, GRAPHQL_FALLBACK_KEY)
        return self._graphql_config

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """POST a GraphQL query to AppSync and return its `data`.

        Tolerates field-level errors (GraphQL partial success — e.g. a
        deleted review's sub-resource resolves to null). Only raises
        GraphQLError when `data` is absent, i.e. the query truly failed.
        """
        endpoint, key = self.graphql_config()
        resp = self.client.post(
            endpoint,
            headers={"x-api-key": key, "content-type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if data is None:
            raise GraphQLError(str(body.get("errors")))
        return data

    # ------------------------------------------------------------- parsers

    @staticmethod
    def parse_next_data(html: str) -> dict[str, Any]:
        m = NEXT_DATA_RE.search(html)
        if not m:
            raise ValueError("No __NEXT_DATA__ blob found on page.")
        return json.loads(m.group(1))

    @staticmethod
    def parse_shelf_rss(xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        items = []
        for item in root.iter("item"):
            def t(tag: str) -> str:
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""

            items.append(
                {
                    "title": t("title"),
                    "author": t("author_name"),
                    "book_id": t("book_id"),
                    "isbn": t("isbn"),
                    "average_rating": t("average_rating"),
                    "my_rating": t("user_rating"),
                    "shelves": t("user_shelves"),
                    "date_added": t("user_date_added"),
                    "date_read": t("user_read_at"),
                    "year_published": t("book_published"),
                    "link": t("link"),
                }
            )
        return items
