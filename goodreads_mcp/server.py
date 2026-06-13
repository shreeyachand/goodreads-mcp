"""goodreads-mcp — a read-only MCP server for Goodreads, sans API.

Tools (all public data, no auth):

    search_books        JSON autocomplete endpoint
    get_book            __NEXT_DATA__ / Apollo state on book pages (.xml path)
    get_reviews         paginated reviews via the AppSync GraphQL backend
    similar_books       "readers also enjoyed" (GraphQL)
    author_books        an author's bibliography (GraphQL)
    series_books        books in a series, with reading order (GraphQL)
    get_editions        published editions: formats/ISBNs (GraphQL)
    book_lists          Listopia lists a book appears on (GraphQL)
    get_shelf           shelf RSS feed
    list_shelves        scraped from the review list page (best effort)

NOTE: book HTML pages now sit behind an AWS WAF JS challenge (HTTP 202).
get_book routes around it via the .xml path. The client raises WAFChallenge
if it ever gets a challenge body so failures are obvious, not silent.

get_reviews uses Goodreads' AppSync GraphQL endpoint; the client resolves the
public api key from the web bundle at runtime (see client.graphql_config).
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import BASE, GoodreadsClient
from .config import load_user_id

SERVER_INSTRUCTIONS = """\
This server returns public Goodreads data (books, reviews, shelves) for research.

Citations by default: every result includes source 'url' fields. When you use
this data in a response, cite it with those links rather than stating facts
unsourced:
  * Books — link the title to the book's 'url' (from get_book / search_books /
    similar_books / etc.).
  * Reviews — when you quote or paraphrase a review, link it to that review's
    'url' and attribute it to the reviewer (by name, optionally their
    'reviewer_url'). Note each review's star 'rating'.
  * Ratings/stats — when citing an average rating or the ratings_histogram,
    point to the book's 'url'.
  * Shelves — link books to their 'link' field.

Prefer markdown links. If a result's url field is null, say so rather than
inventing a link.
"""

mcp = FastMCP("goodreads", instructions=SERVER_INSTRUCTIONS)
gr = GoodreadsClient()
DEFAULT_USER_ID = load_user_id()


def _user_id(user_id: str | None) -> str:
    uid = user_id or DEFAULT_USER_ID
    if not uid:
        raise ValueError(
            "No user_id given and GOODREADS_USER_ID is not configured. "
            "It's the number in goodreads.com/user/show/<ID>-name."
        )
    return str(uid)


def _clean_text(s: str | None) -> str:
    """Strip HTML tags, unescape entities, and collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html_mod.unescape(s)).strip()


def _ms_to_iso(ms: Any) -> str | None:
    """Epoch-milliseconds -> YYYY-MM-DD (UTC), or None."""
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _fetch_book_apollo(book_id: str) -> dict[str, Any]:
    """Fetch a book page and return its Apollo state.

    The plain /book/show/{id} HTML page now sits behind an AWS WAF JS
    challenge (HTTP 202). The .xml-suffixed path serves the identical
    Next.js page with __NEXT_DATA__ intact and is not challenged.
    """
    bid = str(book_id)
    if not bid.endswith(".xml"):
        bid += ".xml"
    page = gr.get(f"/book/show/{bid}")
    return gr.parse_next_data(page.text)["props"]["pageProps"]["apolloState"]


def _make_deref(apollo: dict[str, Any]):
    def deref(ref_obj: Any) -> dict:
        if isinstance(ref_obj, dict) and "__ref" in ref_obj:
            return apollo.get(ref_obj["__ref"], {})
        return ref_obj or {}

    return deref


def _find_book(apollo: dict[str, Any], book_id: str) -> dict[str, Any]:
    book = next(
        (v for k, v in apollo.items() if k.startswith("Book:") and v.get("title")),
        None,
    )
    if not book:
        raise ValueError(f"No Book object in Apollo state for '{book_id}'.")
    return book


def _legacy_id(book_id: str) -> int:
    """Extract the numeric legacy id from '54493401' or '54493401-slug'."""
    m = re.match(r"\d+", str(book_id))
    if not m:
        raise ValueError(
            f"book_id must start with the numeric Goodreads id, got {book_id!r}."
        )
    return int(m.group(0))


# --- GraphQL query documents (recovered from the web app's JS bundles) ----
_Q_BOOK_BY_LEGACY = (
    "query($id: Int!){ getBookByLegacyId(legacyId:$id)"
    "{ legacyId titleComplete title work{ id } } }"
)
_Q_REVIEWS = """
query($filters: BookReviewsFilterInput!, $pagination: PaginationInput){
  getReviews(filters: $filters, pagination: $pagination){
    totalCount
    edges{ node{
      rating text spoilerStatus likeCount commentCount createdAt
      creator{ name webUrl }
      shelving{ webUrl }
    }}
    pageInfo{ nextPageToken }
  }
}"""

# Be a polite guest: cap how many reviews one call will page through.
_MAX_REVIEWS = 100
_REVIEW_PAGE_SIZE = 30

# Resolve a book to the kca ids the discovery queries need.
_Q_BOOK_IDS = (
    "query($id: Int!){ getBookByLegacyId(legacyId:$id){"
    " id legacyId titleComplete title"
    " work{ id }"
    " primaryContributorEdge{ node{ id name webUrl } }"
    " bookSeries{ userPosition series{ id title } } } }"
)
_Q_SIMILAR = """
query($id: ID!, $limit: Int!){
  getSimilarBooks(id: $id, pagination: { limit: $limit }){
    edges{ node{
      legacyId title webUrl imageUrl
      work{ stats{ averageRating ratingsCount } }
      primaryContributorEdge{ node{ name } }
    }}
  }
}"""
_Q_EDITIONS = """
query($id: ID!, $limit: Int!){
  getEditions(id: $id, pagination: { limit: $limit }){
    totalCount
    edges{ node{
      legacyId title webUrl
      details{ format publicationTime publisher isbn13 numPages language{ name } }
    }}
  }
}"""
_Q_SERIES = """
query($input: GetWorksForSeriesInput!, $pagination: PaginationInput){
  getWorksForSeries(getWorksForSeriesInput: $input, pagination: $pagination){
    edges{
      seriesPlacement isPrimary
      node{ stats{ averageRating ratingsCount } bestBook{
        legacyId title webUrl primaryContributorEdge{ node{ name } } } }
    }
  }
}"""
_Q_AUTHOR = """
query($input: GetWorksByContributorInput!, $pagination: PaginationInput){
  getWorksByContributor(getWorksByContributorInput: $input, pagination: $pagination){
    totalCount
    edges{ node{ stats{ averageRating ratingsCount } bestBook{
      legacyId title webUrl primaryContributorEdge{ node{ name } } } }
    }
  }
}"""
_Q_BOOK_LISTS = """
query($id: ID!, $limit: Int!){
  getBookListsOfBook(id: $id, paginationInput: { limit: $limit }){
    edges{ node{ legacyId title webUrl userListVotesCount listBooksCount } }
  }
}"""

# Cap discovery result sizes (single-page queries).
_MAX_DISCOVERY = 40


def _resolve_book_ids(book_id: str) -> dict[str, Any]:
    """Resolve a book_id to its kca ids (book/work/contributor/series) plus
    legacyId and title, via one getBookByLegacyId call."""
    book = gr.graphql(_Q_BOOK_IDS, {"id": _legacy_id(book_id)}).get(
        "getBookByLegacyId"
    )
    if not book:
        raise ValueError(f"No book found for id {book_id!r}.")
    contributor = (book.get("primaryContributorEdge") or {}).get("node") or {}
    series_list = book.get("bookSeries") or []
    series = (series_list[0].get("series") or {}) if series_list else {}
    return {
        "legacy_id": book.get("legacyId"),
        "title": book.get("titleComplete") or book.get("title"),
        "book_kca": book.get("id"),
        "work_kca": (book.get("work") or {}).get("id"),
        "contributor_kca": contributor.get("id"),
        "contributor_name": contributor.get("name"),
        "contributor_url": contributor.get("webUrl"),
        "series_kca": series.get("id"),
        "series_title": series.get("title"),
    }


def _book_summary(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Book node (similar-books shape) to a compact summary."""
    stats = (node.get("work") or {}).get("stats") or {}
    author = (node.get("primaryContributorEdge") or {}).get("node") or {}
    return {
        "book_id": node.get("legacyId"),
        "title": node.get("title"),
        "author": author.get("name"),
        "average_rating": stats.get("averageRating"),
        "ratings_count": stats.get("ratingsCount"),
        "url": node.get("webUrl"),
    }


def _work_summary(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Work node (series/contributor shape) via its bestBook."""
    best = node.get("bestBook") or {}
    stats = node.get("stats") or {}
    author = (best.get("primaryContributorEdge") or {}).get("node") or {}
    return {
        "book_id": best.get("legacyId"),
        "title": best.get("title"),
        "author": author.get("name"),
        "average_rating": stats.get("averageRating"),
        "ratings_count": stats.get("ratingsCount"),
        "url": best.get("webUrl"),
    }


# ===================================================================== READ


@mcp.tool()
def search_books(query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Search Goodreads for books by title/author/ISBN.

    Uses the JSON autocomplete endpoint (no auth, no HTML parsing).
    Returns book_id, title, author, rating info, and a cover URL.
    """
    resp = gr.get("/book/auto_complete", params={"format": "json", "q": query})
    results = []
    for b in resp.json()[:max_results]:
        results.append(
            {
                "book_id": b.get("bookId"),
                "title": b.get("title"),
                "author": (b.get("author") or {}).get("name"),
                "average_rating": b.get("avgRating"),
                "ratings_count": b.get("ratingsCount"),
                "pages": b.get("numPages"),
                "cover": b.get("imageUrl"),
                "url": BASE + b.get("bookUrl", ""),
                "description": html_mod.unescape(
                    re.sub(r"<[^>]+>", "", (b.get("description") or {}).get("html", ""))
                )[:400],
            }
        )
    return results


@mcp.tool()
def get_book(book_id: str) -> dict[str, Any]:
    """Get full details for a book by its Goodreads id (numeric, or numeric-slug
    like '11870085-the-fault-in-our-stars').

    Parses the page's embedded __NEXT_DATA__ JSON (Apollo state) rather than
    scraping the DOM, which survives markup changes. Includes the full
    ratings histogram, series/position, and review-language breakdown — use
    get_reviews for the actual review text.

    When you cite details or ratings from this book, link to its 'url'.
    """
    apollo = _fetch_book_apollo(book_id)
    deref = _make_deref(apollo)
    book = _find_book(apollo, book_id)

    author = deref(deref(book.get("primaryContributorEdge")).get("node"))
    details = book.get("details") or {}
    stats = deref(book.get("work")).get("stats") or book.get("stats") or {}
    genres = [
        (deref(g.get("genre")) or g.get("genre") or {}).get("name")
        for g in (book.get("bookGenres") or [])
    ]

    # Ratings histogram: ratingsCountDist is [1-star, 2-star, ... 5-star].
    dist = stats.get("ratingsCountDist") or []
    histogram = (
        {str(stars): dist[stars - 1] for stars in range(5, 0, -1)}
        if len(dist) == 5
        else None
    )

    # Series + reading position (first series only; most books have one).
    series = series_position = None
    book_series = book.get("bookSeries") or []
    if book_series:
        series = deref(book_series[0].get("series")).get("title")
        series_position = book_series[0].get("userPosition")

    # Review-language breakdown (top languages by text-review count).
    langs = stats.get("textReviewsLanguageCounts") or []
    review_languages = {
        lang.get("isoLanguageCode"): lang.get("count")
        for lang in sorted(langs, key=lambda x: -(x.get("count") or 0))[:5]
    } or None

    return {
        "book_id": book.get("legacyId"),
        "title": book.get("titleComplete") or book.get("title"),
        "author": author.get("name"),
        "description": _clean_text(book.get("description")),
        "average_rating": stats.get("averageRating"),
        "ratings_count": stats.get("ratingsCount"),
        "ratings_histogram": histogram,
        "text_reviews_count": stats.get("textReviewsCount"),
        "review_languages": review_languages,
        "series": series,
        "series_position": series_position,
        "pages": details.get("numPages"),
        "format": details.get("format"),
        "publisher": details.get("publisher"),
        "publication_time": details.get("publicationTime"),
        "isbn13": details.get("isbn13"),
        "genres": [g for g in genres if g],
        "url": book.get("webUrl"),
    }


@mcp.tool()
def get_reviews(
    book_id: str,
    limit: int = 10,
    min_rating: int | None = None,
    max_rating: int | None = None,
    exclude_spoilers: bool = False,
) -> dict[str, Any]:
    """Get reader reviews for a book — the actual review text, not just a score.

    Fetches from Goodreads' GraphQL backend with true pagination, so limit
    can exceed the ~30 shown on a page. Reviews come in "most relevant"
    order and aggregate across all editions of the work. Each review has the
    reviewer name, star rating (1-5), full text, like/comment counts, date, a
    spoiler flag, a 'url' permalink (use it to cite/link), and the reviewer's
    profile url.

    limit: max reviews to return (capped at 100 to stay polite).
    min_rating / max_rating: server-side star filters, e.g. min_rating=4 for
        positive reviews, max_rating=2 for the critical ones.
    exclude_spoilers: drop reviews flagged as spoilers.
    """
    want = max(0, min(limit, _MAX_REVIEWS))
    book = gr.graphql(_Q_BOOK_BY_LEGACY, {"id": _legacy_id(book_id)}).get(
        "getBookByLegacyId"
    )
    if not book:
        raise ValueError(f"No book found for id {book_id!r}.")
    work_id = (book.get("work") or {}).get("id")
    if not work_id:
        raise ValueError(f"Could not resolve work id for book {book_id!r}.")

    filters: dict[str, Any] = {"resourceType": "WORK", "resourceId": work_id}
    if min_rating is not None:
        filters["ratingMin"] = min_rating
    if max_rating is not None:
        filters["ratingMax"] = max_rating

    reviews: list[dict[str, Any]] = []
    total: int | None = None
    token: str | None = None
    while len(reviews) < want:
        pagination: dict[str, Any] = {"limit": _REVIEW_PAGE_SIZE}
        if token:
            pagination["after"] = token
        conn = gr.graphql(
            _Q_REVIEWS, {"filters": filters, "pagination": pagination}
        ).get("getReviews") or {}
        if total is None:
            total = conn.get("totalCount")
        edges = conn.get("edges") or []
        for edge in edges:
            rev = edge.get("node") or {}
            spoiler = bool(rev.get("spoilerStatus"))
            if exclude_spoilers and spoiler:
                continue
            creator = rev.get("creator") or {}
            reviews.append(
                {
                    "reviewer": creator.get("name"),
                    "rating": rev.get("rating"),
                    "text": _clean_text(rev.get("text")),
                    "likes": rev.get("likeCount"),
                    "comments": rev.get("commentCount"),
                    "date": _ms_to_iso(rev.get("createdAt")),
                    "spoiler": spoiler,
                    "url": (rev.get("shelving") or {}).get("webUrl"),
                    "reviewer_url": creator.get("webUrl"),
                }
            )
            if len(reviews) >= want:
                break
        token = (conn.get("pageInfo") or {}).get("nextPageToken")
        if not token or not edges:
            break

    return {
        "book_id": book.get("legacyId"),
        "title": book.get("titleComplete") or book.get("title"),
        "total_text_reviews": total,
        "returned": len(reviews),
        "reviews": reviews,
    }


@mcp.tool()
def similar_books(book_id: str, limit: int = 10) -> dict[str, Any]:
    """"Readers also enjoyed" — books similar to the given one.

    Goodreads' own recommendation graph (hard to reproduce with web search).
    Each result has book_id/title/author/rating/url so you can chain into
    get_book or get_reviews. limit capped at 40.
    """
    ids = _resolve_book_ids(book_id)
    data = gr.graphql(
        _Q_SIMILAR, {"id": ids["book_kca"], "limit": min(limit, _MAX_DISCOVERY)}
    )
    edges = ((data.get("getSimilarBooks") or {}).get("edges")) or []
    books = [_book_summary(e.get("node") or {}) for e in edges]
    return {"book_id": ids["legacy_id"], "title": ids["title"], "similar": books}


@mcp.tool()
def author_books(book_id: str, limit: int = 20) -> dict[str, Any]:
    """List an author's works (bibliography), given any of their books.

    Resolves the book's primary author, then returns their works ranked by
    popularity. Each result has book_id/title/author/rating/url. limit
    capped at 40.
    """
    ids = _resolve_book_ids(book_id)
    if not ids["contributor_kca"]:
        raise ValueError(f"Could not resolve an author for book {book_id!r}.")
    data = gr.graphql(
        _Q_AUTHOR,
        {
            "input": {"id": ids["contributor_kca"]},
            "pagination": {"limit": min(limit, _MAX_DISCOVERY)},
        },
    )
    conn = data.get("getWorksByContributor") or {}
    works = [_work_summary(e.get("node") or {}) for e in (conn.get("edges") or [])]
    return {
        "author": ids["contributor_name"],
        "author_url": ids["contributor_url"],
        "total_works": conn.get("totalCount"),
        "returned": len(works),
        "works": works,
    }


@mcp.tool()
def series_books(book_id: str, limit: int = 20) -> dict[str, Any]:
    """List the books in a series (with reading-order placement), given any
    book in that series.

    Each entry has the series 'placement' (e.g. '1', '0.5' for a prequel),
    'is_primary' (a main-sequence entry vs companion), and the usual
    book_id/title/author/rating/url. limit capped at 40.
    """
    ids = _resolve_book_ids(book_id)
    if not ids["series_kca"]:
        return {
            "book_id": ids["legacy_id"],
            "title": ids["title"],
            "series": None,
            "note": "This book isn't part of a Goodreads series.",
            "books": [],
        }
    data = gr.graphql(
        _Q_SERIES,
        {
            "input": {"id": ids["series_kca"]},
            "pagination": {"limit": min(limit, _MAX_DISCOVERY)},
        },
    )
    edges = ((data.get("getWorksForSeries") or {}).get("edges")) or []
    books = []
    for e in edges:
        summary = _work_summary(e.get("node") or {})
        summary["placement"] = e.get("seriesPlacement")
        summary["is_primary"] = e.get("isPrimary")
        books.append(summary)
    return {
        "series": ids["series_title"],
        "returned": len(books),
        "books": books,
    }


@mcp.tool()
def get_editions(book_id: str, limit: int = 20) -> dict[str, Any]:
    """List published editions of a book (formats, ISBNs, publishers, dates).

    Useful for "which edition / format / ISBN" questions. limit capped at 40.
    """
    ids = _resolve_book_ids(book_id)
    data = gr.graphql(
        _Q_EDITIONS, {"id": ids["work_kca"], "limit": min(limit, _MAX_DISCOVERY)}
    )
    conn = data.get("getEditions") or {}
    editions = []
    for e in conn.get("edges") or []:
        node = e.get("node") or {}
        details = node.get("details") or {}
        editions.append(
            {
                "book_id": node.get("legacyId"),
                "title": node.get("title"),
                "format": details.get("format"),
                "publisher": details.get("publisher"),
                "publication_time": details.get("publicationTime"),
                "isbn13": details.get("isbn13"),
                "pages": details.get("numPages"),
                "language": (details.get("language") or {}).get("name"),
                "url": node.get("webUrl"),
            }
        )
    return {
        "book_id": ids["legacy_id"],
        "title": ids["title"],
        "total_editions": conn.get("totalCount"),
        "returned": len(editions),
        "editions": editions,
    }


@mcp.tool()
def book_lists(book_id: str, limit: int = 10) -> dict[str, Any]:
    """List the Listopia lists a book appears on (e.g. "Best Dystopian
    Fiction"), ordered by popularity.

    Each list has its title, total member votes, how many books it contains,
    and a 'url'. Good for "what kind of book is this / what's it grouped with"
    and for discovery. limit capped at 40.
    """
    ids = _resolve_book_ids(book_id)
    data = gr.graphql(
        _Q_BOOK_LISTS, {"id": ids["book_kca"], "limit": min(limit, _MAX_DISCOVERY)}
    )
    edges = ((data.get("getBookListsOfBook") or {}).get("edges")) or []
    lists = [
        {
            "list_id": (n := e.get("node") or {}).get("legacyId"),
            "title": n.get("title"),
            "votes": n.get("userListVotesCount"),
            "books_count": n.get("listBooksCount"),
            "url": n.get("webUrl"),
        }
        for e in edges
    ]
    return {
        "book_id": ids["legacy_id"],
        "title": ids["title"],
        "returned": len(lists),
        "lists": lists,
    }


@mcp.tool()
def get_shelf(
    shelf: str = "to-read",
    user_id: str | None = None,
    page: int = 1,
) -> list[dict[str, Any]]:
    """List books on a shelf via its RSS feed (public shelves; no auth).

    Common shelves: 'read', 'currently-reading', 'to-read', plus any custom
    shelf name. RSS pages hold ~100 items; pass page=2,3,... for more.
    Defaults to the configured GOODREADS_USER_ID.

    When you cite a book from a shelf, link it to its 'link' field.
    """
    uid = _user_id(user_id)
    resp = gr.get(f"/review/list_rss/{uid}", params={"shelf": shelf, "page": page})
    return gr.parse_shelf_rss(resp.text)


@mcp.tool()
def list_shelves(user_id: str | None = None) -> list[str]:
    """List a user's shelf names (scraped from their review-list page; best
    effort). Defaults to the configured user."""
    uid = _user_id(user_id)
    page = gr.get(f"/review/list/{uid}").text
    names = re.findall(r'[?&]shelf=([A-Za-z0-9_%\-]+)', page)
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(html_mod.unescape(n), None)
    return list(seen)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
