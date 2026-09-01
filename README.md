# 📚 goodreads-mcp

A **read-only** MCP server for Goodreads — built without the Goodreads API, because there hasn't been one since December 2020. Lets an LLM find and research books, ratings, and reviews. Tools ride on RSS feeds, the JSON autocomplete endpoint, and the `__NEXT_DATA__` blob embedded in book pages. No login, no cookies, no writes — public data only.

## tools

| tool | stability |
|---|---|
| `search_books` | stable (JSON endpoint) |
| `get_book` | stable (`__NEXT_DATA__` via `.xml` path) — details, cover, ratings histogram, all series memberships, review-language breakdown |
| `get_reviews` | GraphQL — paginated reader reviews (text, rating, likes, date, spoiler flag, permalink) with server-side `min_rating` / `max_rating` and `exclude_spoilers`; `limit` up to 100 |
| `similar_books` | GraphQL — paginated "readers also enjoyed" recommendations |
| `author_books` | GraphQL — paginated author bibliography (from any of their books) |
| `series_books` | GraphQL — paginated series books with reading-order placement; selectable membership for books in multiple series |
| `get_editions` | GraphQL — paginated editions (format, ISBN, publisher, date) |
| `book_lists` | GraphQL — paginated Listopia lists a book appears on (title, votes, size) |
| `popular_books` | GraphQL — most popular books by release year (or year+month), ranked |
| `compare_books` | takes several book ids, ranks them by rating with positive/critical share |
| `get_shelf` | stable (RSS) — public shelves |
| `list_shelves` | best effort (HTML) — public profiles |

The discovery tools all take a `book_id` and return results carrying `book_id`/title/author/rating/url, so an agent can chain them — e.g. `similar_books` → `get_reviews` on a recommendation. This is the structured book graph a general web search can't assemble.

GraphQL discovery tools page in batches of 20 and accept a total `limit` up to
100. Responses include `returned` and `has_more`, keeping larger lookups useful
without allowing unbounded traffic.

> **WAF note:** Goodreads book HTML pages now sit behind an AWS WAF JavaScript
> challenge (HTTP 202) that plain HTTP clients can't solve. `get_book` routes
> around it via the `.xml`-suffixed page, so it still works without a browser. If
> Goodreads ever extends the WAF to a path we depend on, the client raises
> `WAFChallenge` with a clear message instead of a confusing parse error.

## install

```bash
cd goodreads-mcp
python3.10 -m venv .venv && .venv/bin/pip install -e .
```

Requires Python ≥ 3.10.

## config (optional)

No login or cookies — everything is public data. The only setting is your numeric `user_id`, the default for the shelf tools. It's the number in `goodreads.com/user/show/<ID>-yourname`; you can also pass `user_id` to each shelf tool per call.

```bash
mkdir -p ~/.config/goodreads-mcp
cat > ~/.config/goodreads-mcp/config.json << 'EOF'
{ "user_id": "12345678" }
EOF
```

Env var `GOODREADS_USER_ID` overrides the file.

## Claude Desktop config

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "goodreads": {
      "command": "/path/to/goodreads-mcp/.venv/bin/goodreads-mcp"
    }
  }
}
```

Or for development, `mcp dev goodreads_mcp/server.py` gives you the Inspector UI to poke each tool.

## first-run verification

The endpoints are unofficial, so verify in this order:

1. `search_books("project hail mary")` — should just work
2. `get_book("54493401")` — confirms the `.xml`/WAF workaround; check the histogram is populated
3. `get_reviews("54493401")` — should return real review text
4. `get_shelf("to-read")` — checks your `user_id` + RSS
5. `list_shelves()` — best-effort shelf-name scrape

## tests

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest                       # offline parser/unit tests
GOODREADS_LIVE=1 .venv/bin/pytest      # + live network smoke tests
```

## design notes

- **Request-first, no browser automation.** Everything is `httpx` against JSON/RSS/embedded-JSON/GraphQL surfaces; the only HTML regex is in `list_shelves` and the GraphQL config discovery.
- **GraphQL backbone (reviews).** `get_reviews` calls Goodreads' AppSync GraphQL endpoint — the same backend the website uses. The web app injects a public read-only API key into page-level `__NEXT_DATA__` and keeps the production endpoint in its `_app` bundle; the client resolves both at runtime and caches them, so rotations self-heal (`client.graphql_config`). Legacy bundles that carry a paired key and endpoint are still supported. This is what enables real pagination (past the ~30 reviews a page embeds) and server-side rating filters. GraphQL partial-success is respected: a deleted review's sub-resource just comes back `null` rather than failing the call.
- **WAF-aware.** Book pages sit behind an AWS WAF JS challenge; `get_book` uses the `.xml` path that isn't gated, and the client raises `WAFChallenge` if it ever gets a challenge body so failures are loud, not silent. (The GraphQL endpoint is a separate AppSync host and isn't WAF-gated.)
- **Polite client.** Single persistent session, browser-faithful headers, exponential backoff on 429/503; `get_reviews` caps paging at 100 reviews.
- **Short-lived caching.** Book-page data and GraphQL identifier resolution use a bounded five-minute TTL cache. Chained tools and comparisons avoid immediately refetching the same public data while still refreshing changing Goodreads stats.
- **Caveats**: all of this is unofficial and depends on markup/endpoints/keys that can drift.

## shipped since v0.1

- **richer book data** — `get_book` includes covers, the ratings histogram, every series membership, normalized publication dates, and configurable review-language depth; `series_books` can traverse any listed membership, and `get_reviews` returns paginated, filterable reader reviews.
- **author bibliography** — `author_books` returns an author's works (ranked by popularity) plus a link to their author page (`author_url`).
- **bounded discovery pagination** — similar books, bibliographies, series, editions, and Listopia memberships can return up to 100 results with `has_more` metadata.
- **TTL caching** — repeated book-page and identifier lookups are reused for five minutes.

## ideas for v2

- author page detail (bio, photo, follower count) — not currently exposed cleanly: the author page is legacy server-rendered HTML with no structured JSON, and there's no discoverable GraphQL contributor-detail query, so this would require brittle DOM scraping. `author_books` links to the page instead.
