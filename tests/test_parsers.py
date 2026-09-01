"""Offline unit tests for the pure parsing/config logic (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from goodreads_mcp.client import (
    GoodreadsClient,
    WAFChallenge,
    _is_waf_challenge,
    parse_appsync_config,
    parse_appsync_endpoint,
    parse_page_api_key,
)
from goodreads_mcp.server import _clean_text, _legacy_id, _ms_to_iso


# ----------------------------------------------------------------- text utils


def test_clean_text_strips_html_and_collapses_whitespace():
    raw = '<p>Great   book!</p>\n<br/>  <a href="x">link</a>'
    assert _clean_text(raw) == "Great book! link"


def test_clean_text_unescapes_entities():
    assert _clean_text("Tom &amp; Jerry &lt;3") == "Tom & Jerry <3"


def test_clean_text_handles_none_and_empty():
    assert _clean_text(None) == ""
    assert _clean_text("") == ""


def test_ms_to_iso_converts_epoch_millis():
    # 1600396415413 ms = 2020-09-18 (UTC)
    assert _ms_to_iso(1600396415413) == "2020-09-18"


def test_ms_to_iso_handles_falsy_and_bad_input():
    assert _ms_to_iso(0) is None
    assert _ms_to_iso(None) is None
    assert _ms_to_iso("not-a-number") is None


def test_legacy_id_extracts_numeric():
    assert _legacy_id("54493401") == 54493401
    assert _legacy_id("54493401-project-hail-mary") == 54493401


def test_legacy_id_rejects_non_numeric():
    with pytest.raises(ValueError):
        _legacy_id("project-hail-mary")


# --------------------------------------------------------- appsync config


# Mimics the multi-environment config embedded in the _app JS bundle.
BUNDLE = (
    'foo({"Dev":{"auth":{"appsync":{"apiKey":'
    '"da2-devkey00000000000000000","endpoint":'
    '"https://dev.appsync-api.us-east-1.amazonaws.com/graphql",'
    '"region":"us-east-1"},"showAds":true,"shortName":"Dev"},'
    '"Prod":{"appsync":{"apiKey":'
    '"da2-prodkey0000000000000000","endpoint":'
    '"https://prod.appsync-api.us-east-1.amazonaws.com/graphql",'
    '"region":"us-east-1"},"showAds":true,"shortName":"Prod"}}})'
)


def test_parse_appsync_config_picks_prod():
    endpoint, key = parse_appsync_config(BUNDLE)
    assert key == "da2-prodkey0000000000000000"
    assert endpoint == "https://prod.appsync-api.us-east-1.amazonaws.com/graphql"


def test_parse_appsync_config_falls_back_to_first_pair():
    no_prod = BUNDLE.replace('"shortName":"Prod"', '"shortName":"Staging"')
    endpoint, key = parse_appsync_config(no_prod)
    assert key == "da2-devkey00000000000000000"


def test_parse_appsync_config_raises_when_absent():
    with pytest.raises(ValueError):
        parse_appsync_config("var x = 1; // no appsync here")


def test_parse_appsync_endpoint_picks_prod_without_bundle_key():
    bundle = BUNDLE.replace(
        '"apiKey":"da2-devkey00000000000000000",', '"apiKey":"",'
    ).replace(
        '"apiKey":"da2-prodkey0000000000000000",', '"apiKey":"",'
    )
    assert parse_appsync_endpoint(bundle) == (
        "https://prod.appsync-api.us-east-1.amazonaws.com/graphql"
    )


def _page(api_key: str | None) -> str:
    page_props = {"dataSource": "Production"}
    if api_key is not None:
        page_props["apiKey"] = api_key
    payload = {"props": {"pageProps": page_props}}
    return (
        '<script src="/_next/static/chunks/pages/_app-deadbeef.js"></script>'
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def test_parse_page_api_key_reads_next_page_props():
    assert parse_page_api_key(_page("da2-pagekey0000000000000000")) == (
        "da2-pagekey0000000000000000"
    )
    assert parse_page_api_key(_page(None)) is None
    assert parse_page_api_key(_page("not-an-appsync-key")) is None


def test_graphql_config_combines_page_key_with_bundle_endpoint():
    bundle = BUNDLE.replace(
        '"apiKey":"da2-devkey00000000000000000",', '"apiKey":"",'
    ).replace(
        '"apiKey":"da2-prodkey0000000000000000",', '"apiKey":"",'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=(
                _page("da2-pagekey0000000000000000")
                if request.url.path == "/giveaway"
                else bundle
            ),
        )
    )
    client = GoodreadsClient()
    client._client = httpx.Client(
        base_url="https://www.goodreads.com", transport=transport
    )

    assert client.graphql_config() == (
        "https://prod.appsync-api.us-east-1.amazonaws.com/graphql",
        "da2-pagekey0000000000000000",
    )


def test_graphql_config_uses_legacy_bundle_if_page_key_is_invalid():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=(
                _page("not-an-appsync-key")
                if request.url.path == "/giveaway"
                else BUNDLE
            ),
        )
    )
    client = GoodreadsClient()
    client._client = httpx.Client(
        base_url="https://www.goodreads.com", transport=transport
    )

    assert client.graphql_config() == (
        "https://prod.appsync-api.us-east-1.amazonaws.com/graphql",
        "da2-prodkey0000000000000000",
    )


def test_graphql_config_does_not_hide_discovery_failure_with_stale_key():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=(
                _page(None)
                if request.url.path == "/giveaway"
                else "var config = {};"
            ),
        )
    )
    client = GoodreadsClient()
    client._client = httpx.Client(
        base_url="https://www.goodreads.com", transport=transport
    )

    with pytest.raises(ValueError, match="No AppSync"):
        client.graphql_config()


def test_graphql_401_rediscovers_page_key_before_retry():
    bundle = BUNDLE.replace(
        '"apiKey":"da2-devkey00000000000000000",', '"apiKey":"",'
    ).replace(
        '"apiKey":"da2-prodkey0000000000000000",', '"apiKey":"",'
    )
    page_keys = iter(
        ("da2-expired000000000000000000", "da2-fresh00000000000000000000")
    )
    posted_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/giveaway":
            return httpx.Response(200, text=_page(next(page_keys)))
        if request.url.path.startswith("/_next/"):
            return httpx.Response(200, text=bundle)
        posted_keys.append(request.headers["x-api-key"])
        if request.headers["x-api-key"].startswith("da2-expired"):
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"data": {"ping": "pong"}})

    client = GoodreadsClient()
    client._client = httpx.Client(
        base_url="https://www.goodreads.com", transport=httpx.MockTransport(handler)
    )

    assert client.graphql("query { ping }") == {"ping": "pong"}
    assert posted_keys == [
        "da2-expired000000000000000000",
        "da2-fresh00000000000000000000",
    ]


# ------------------------------------------------------------------- shelf RSS


SHELF_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Dune</title>
    <author_name>Frank Herbert</author_name>
    <book_id>234225</book_id>
    <isbn>0441013597</isbn>
    <average_rating>4.27</average_rating>
    <user_rating>5</user_rating>
    <user_shelves>sci-fi, favorites</user_shelves>
    <user_date_added>Mon, 01 Jan 2024 00:00:00 -0800</user_date_added>
    <user_read_at>Tue, 02 Jan 2024 00:00:00 +0000</user_read_at>
    <book_published>1965</book_published>
    <link>https://www.goodreads.com/review/show/1</link>
  </item>
  <item>
    <title>Neuromancer</title>
    <author_name>William Gibson</author_name>
    <book_id>22328</book_id>
  </item>
</channel></rss>"""


def test_parse_shelf_rss_full_item():
    items = GoodreadsClient.parse_shelf_rss(SHELF_RSS)
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Dune"
    assert first["author"] == "Frank Herbert"
    assert first["book_id"] == "234225"
    assert first["my_rating"] == "5"
    assert first["shelves"] == "sci-fi, favorites"
    assert first["year_published"] == "1965"


def test_parse_shelf_rss_missing_fields_default_empty():
    items = GoodreadsClient.parse_shelf_rss(SHELF_RSS)
    second = items[1]
    assert second["title"] == "Neuromancer"
    assert second["isbn"] == ""
    assert second["my_rating"] == ""


# ----------------------------------------------------------------- next_data


def test_parse_next_data_extracts_json():
    payload = {"props": {"pageProps": {"apolloState": {"Book:1": {"title": "X"}}}}}
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )
    parsed = GoodreadsClient.parse_next_data(html)
    assert parsed["props"]["pageProps"]["apolloState"]["Book:1"]["title"] == "X"


def test_parse_next_data_missing_blob_raises():
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        GoodreadsClient.parse_next_data("<html><body>nothing here</body></html>")


# --------------------------------------------------------------- WAF detection


WAF_BODY = (
    "<!DOCTYPE html><html><head><script>"
    "window.awsWafCookieDomainList = [];</script></head>"
    '<body><div id="challenge-container"></div></body></html>'
)


def _resp(status: int, body: str, content_type: str = "text/html") -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": content_type}, text=body
    )


def test_waf_challenge_detected_on_202_html():
    assert _is_waf_challenge(_resp(202, WAF_BODY)) is True


def test_waf_not_flagged_on_200():
    assert _is_waf_challenge(_resp(200, WAF_BODY)) is False


def test_waf_not_flagged_on_normal_202():
    assert _is_waf_challenge(_resp(202, "<html>queued</html>")) is False


def test_waf_not_flagged_on_json_202():
    assert _is_waf_challenge(_resp(202, "{}", content_type="application/json")) is False


def test_request_raises_on_waf_challenge():
    client = GoodreadsClient()
    transport = httpx.MockTransport(
        lambda req: _resp(202, WAF_BODY)
    )
    client._client = httpx.Client(base_url="https://example.test", transport=transport)
    with pytest.raises(WAFChallenge):
        client.get("/book/show/1")
