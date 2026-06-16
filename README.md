# 📚 goodreads-mcp

A **read-only** MCP server for Goodreads — built without the Goodreads API, because there hasn't been one since December 2020. Lets an LLM find and research books, ratings, and reviews. Tools ride on RSS feeds, the JSON autocomplete endpoint, and the `__NEXT_DATA__` blob embedded in book pages. No login, no cookies, no writes — public data only.

## tools

| tool | stability |
|---|---|
| `search_books` | stable (JSON endpoint) |
| `get_book` | stable (`__NEXT_DATA__` via `.xml` path) — details, ratings histogram, series, review-language breakdown |
| `get_reviews` | GraphQL — paginated reader reviews (text, rating, likes, date, spoiler flag, permalink) with server-side `min_rating` / `max_rating` and `exclude_spoilers`; `limit` up to 100 |
| `similar_books` | GraphQL — "readers also enjoyed" recommendations |
| `author_books` | GraphQL — an author's bibliography (from any of their books) |
| `series_books` | GraphQL — books in a series with reading-order placement |
| `get_editions` | GraphQL — published editions (format, ISBN, publisher, date) |
| `book_lists` | GraphQL — Listopia lists a book appears on (title, votes, size) |
| `popular_books` | GraphQL — most popular books by release year (or year+month), ranked |
| `compare_books` | takes several book ids, ranks them by rating with positive/critical share |
| `get_shelf` | stable (RSS) — public shelves |
| `list_shelves` | best effort (HTML) — public profiles |

The discovery tools all take a `book_id` and return results carrying `book_id`/title/author/rating/url, so an agent can chain them — e.g. `similar_books` → `get_reviews` on a recommendation. This is the structured book graph a general web search can't assemble.

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
- **GraphQL backbone (reviews).** `get_reviews` calls Goodreads' AppSync GraphQL endpoint — the same backend the website uses. The web app ships a public read-only API key in its JS bundle; the client scrapes the endpoint + key from that bundle at runtime and caches them, so a key rotation self-heals (`client.graphql_config`). A hardcoded pair is kept as a fallback. This is what enables real pagination (past the ~30 reviews a page embeds) and server-side rating filters. GraphQL partial-success is respected: a deleted review's sub-resource just comes back `null` rather than failing the call.
- **WAF-aware.** Book pages sit behind an AWS WAF JS challenge; `get_book` uses the `.xml` path that isn't gated, and the client raises `WAFChallenge` if it ever gets a challenge body so failures are loud, not silent. (The GraphQL endpoint is a separate AppSync host and isn't WAF-gated.)
- **Polite client.** Single persistent session, browser-faithful headers, exponential backoff on 429/503; `get_reviews` caps paging at 100 reviews.
- **Caveats**: all of this is unofficial and depends on markup/endpoints/keys that can drift.

## shipped since v0.1

- **richer book data** — `get_book` now includes the ratings histogram, series/position, and review-language breakdown; `series_books` and `similar_books` cover series and recommendations; `get_reviews` returns paginated, filterable reader reviews.
- **author bibliography** — `author_books` returns an author's works (ranked by popularity) plus a link to their author page (`author_url`).

## ideas for v2

- author page detail (bio, photo, follower count) — not currently exposed cleanly: the author page is legacy server-rendered HTML with no structured JSON, and there's no discoverable GraphQL contributor-detail query, so this would require brittle DOM scraping. `author_books` links to the page instead.
- caching layer for repeated lookups (the discovery tools each resolve the book first; a small TTL cache would cut duplicate GraphQL calls)
