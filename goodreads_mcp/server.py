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
    popular_books       most popular books by release year/month (GraphQL)
    compare_books       rank several books by rating + polarization
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

from .cache import ttl_cache
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

# Cache only stable, read-only lookup results. Five minutes is long enough to
# collapse repeated tool chains without making changing Goodreads stats feel
# stale for the lifetime of an MCP session.
_CACHE_TTL_SECONDS = 300
_BOOK_CACHE_SIZE = 128


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


@ttl_cache(maxsize=_BOOK_CACHE_SIZE, ttl_seconds=_CACHE_TTL_SECONDS)
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
query($id: ID!, $pagination: PaginationInput){
  getSimilarBooks(id: $id, pagination: $pagination){
    pageInfo{ hasNextPage nextPageToken }
    edges{ node{
      legacyId title webUrl imageUrl
      work{ stats{ averageRating ratingsCount } }
      primaryContributorEdge{ node{ name } }
    }}
  }
}"""
_Q_EDITIONS = """
query($id: ID!, $pagination: PaginationInput){
  getEditions(id: $id, pagination: $pagination){
    totalCount
    pageInfo{ hasNextPage nextPageToken }
    edges{ node{
      legacyId title webUrl imageUrl
      details{ format publicationTime publisher isbn13 numPages language{ name } }
    }}
  }
}"""
_Q_SERIES = """
query($input: GetWorksForSeriesInput!, $pagination: PaginationInput){
  getWorksForSeries(getWorksForSeriesInput: $input, pagination: $pagination){
    pageInfo{ hasNextPage nextPageToken }
    edges{
      seriesPlacement isPrimary
      node{ stats{ averageRating ratingsCount } bestBook{
        legacyId title webUrl imageUrl primaryContributorEdge{ node{ name } } } }
    }
  }
}"""
_Q_AUTHOR = """
query($input: GetWorksByContributorInput!, $pagination: PaginationInput){
  getWorksByContributor(getWorksByContributorInput: $input, pagination: $pagination){
    totalCount
    pageInfo{ hasNextPage nextPageToken }
    edges{ node{ stats{ averageRating ratingsCount } bestBook{
      legacyId title webUrl imageUrl primaryContributorEdge{ node{ name } } } }
    }
  }
}"""
_Q_BOOK_LISTS = """
query($id: ID!, $pagination: PaginationInput){
  getBookListsOfBook(id: $id, paginationInput: $pagination){
    pageInfo{ hasNextPage nextPageToken }
    edges{ node{ legacyId title webUrl userListVotesCount listBooksCount } }
  }
}"""
_Q_TOP_LIST = """
query($name: String!, $period: String!, $location: String!,
      $after: String, $limit: Int){
  getTopList(
    getTopListInput: { name: $name, period: $period, location: $location },
    pagination: { after: $after, limit: $limit }
  ){
    pageInfo{ hasNextPage nextPageToken }
    edges{
      ... on TopListBookEdge {
        rank count
        node{ __typename legacyId title webUrl imageUrl
          work{ stats{ averageRating ratingsCount } }
          primaryContributorEdge{ node{ name } } }
      }
      ... on TopListWorkEdge {
        rank count
        node{ __typename stats{ averageRating ratingsCount }
          details{ bestBook{ legacyId title webUrl imageUrl
            primaryContributorEdge{ node{ name } } } } }
      }
    }
  }
}"""

# Discovery connections are paginated in small requests and capped in total.
_MAX_DISCOVERY = 100
_DISCOVERY_PAGE_SIZE = 20
# popular_books paginates; cap total and page size.
_MAX_POPULAR = 50
_POPULAR_PAGE_SIZE = 30
# compare_books fetches one book page per id; cap the fan-out.
_MAX_COMPARE = 10


def _validate_discovery_limit(limit: int) -> int:
    if limit < 0:
        raise ValueError("limit must be zero or greater.")
    return min(limit, _MAX_DISCOVERY)


def _paginated_graphql_edges(
    query: str,
    connection_name: str,
    variables: dict[str, Any],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, int | None]:
    """Collect a bounded number of edges from a GraphQL connection.

    Returns ``(edges, has_more, total_count)``. Every supported discovery
    connection uses Goodreads' standard PaginationInput and PageInfo shapes.
    """
    want = _validate_discovery_limit(limit)
    if want == 0:
        return [], False, None

    collected: list[dict[str, Any]] = []
    token: str | None = None
    total_count: int | None = None
    has_more = False
    seen_tokens: set[str] = set()

    while len(collected) < want:
        pagination: dict[str, Any] = {
            "limit": min(_DISCOVERY_PAGE_SIZE, want - len(collected))
        }
        if token:
            pagination["after"] = token
        page_variables = {**variables, "pagination": pagination}
        connection = gr.graphql(query, page_variables).get(connection_name) or {}
        if total_count is None:
            total_count = connection.get("totalCount")

        page_edges = [e for e in (connection.get("edges") or []) if e]
        remaining = want - len(collected)
        collected.extend(page_edges[:remaining])

        info = connection.get("pageInfo") or {}
        next_token = info.get("nextPageToken")
        has_more = bool(info.get("hasNextPage") and next_token)
        if len(page_edges) > remaining:
            has_more = True
        if len(collected) >= want or not page_edges or not has_more:
            break
        if next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        token = next_token

    if total_count is not None and len(collected) < total_count:
        has_more = True
    return collected, has_more, total_count


@ttl_cache(maxsize=_BOOK_CACHE_SIZE, ttl_seconds=_CACHE_TTL_SECONDS)
def _resolve_book_ids(book_id: str) -> dict[str, Any]:
    """Resolve a book_id to its book/work/contributor/series identifiers,
    legacyId, title, and every series membership in one GraphQL call."""
    book = gr.graphql(_Q_BOOK_IDS, {"id": _legacy_id(book_id)}).get(
        "getBookByLegacyId"
    )
    if not book:
        raise ValueError(f"No book found for id {book_id!r}.")
    contributor = (book.get("primaryContributorEdge") or {}).get("node") or {}
    series_memberships = []
    for membership in book.get("bookSeries") or []:
        series = membership.get("series") or {}
        series_memberships.append(
            {
                "id": series.get("id"),
                "title": series.get("title"),
                "position": membership.get("userPosition"),
            }
        )
    first_series = series_memberships[0] if series_memberships else {}
    return {
        "legacy_id": book.get("legacyId"),
        "title": book.get("titleComplete") or book.get("title"),
        "book_kca": book.get("id"),
        "work_kca": (book.get("work") or {}).get("id"),
        "contributor_kca": contributor.get("id"),
        "contributor_name": contributor.get("name"),
        "contributor_url": contributor.get("webUrl"),
        "series_kca": first_series.get("id"),
        "series_title": first_series.get("title"),
        "series_memberships": series_memberships,
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
        "cover": node.get("imageUrl"),
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
        "cover": best.get("imageUrl"),
        "url": best.get("webUrl"),
    }


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a node that may be a Book or a Work to a compact summary.

    Work nodes expose their representative book at details.bestBook (the
    top-list shape) or directly at bestBook.
    """
    if node.get("__typename") == "Work":
        best = (node.get("details") or {}).get("bestBook") or node.get("bestBook") or {}
        stats = node.get("stats") or {}
        author = (best.get("primaryContributorEdge") or {}).get("node") or {}
        return {
            "book_id": best.get("legacyId"),
            "title": best.get("title"),
            "author": author.get("name"),
            "average_rating": stats.get("averageRating"),
            "ratings_count": stats.get("ratingsCount"),
            "cover": best.get("imageUrl"),
            "url": best.get("webUrl"),
        }
    return _book_summary(node)


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
def get_book(book_id: str, review_language_limit: int = 5) -> dict[str, Any]:
    """Get full details for a book by its Goodreads id (numeric, or numeric-slug
    like '11870085-the-fault-in-our-stars').

    Parses the page's embedded __NEXT_DATA__ JSON (Apollo state) rather than
    scraping the DOM, which survives markup changes. Includes the full
    ratings histogram, all series memberships, and review-language breakdown
    — use get_reviews for the actual review text. review_language_limit controls
    how many languages are returned (default 5, maximum 25).

    When you cite details or ratings from this book, link to its 'url'.
    """
    if review_language_limit < 0:
        raise ValueError("review_language_limit must be zero or greater.")
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

    # Preserve the original first-series fields for compatibility, while also
    # exposing every series membership in the embedded Apollo state.
    series_memberships = []
    book_series = book.get("bookSeries") or []
    for membership in book_series:
        series_node = deref(membership.get("series"))
        series_memberships.append(
            {
                "series": series_node.get("title"),
                "position": membership.get("userPosition"),
            }
        )
    series = series_memberships[0]["series"] if series_memberships else None
    series_position = (
        series_memberships[0]["position"] if series_memberships else None
    )

    language_limit = min(review_language_limit, 25)
    # Review-language breakdown, ordered by text-review count.
    langs = stats.get("textReviewsLanguageCounts") or []
    review_languages = {
        lang.get("isoLanguageCode"): lang.get("count")
        for lang in sorted(langs, key=lambda x: -(x.get("count") or 0))[
            :language_limit
        ]
        if lang.get("isoLanguageCode")
    } or None

    return {
        "book_id": book.get("legacyId"),
        "title": book.get("titleComplete") or book.get("title"),
        "author": author.get("name"),
        "cover": book.get("imageUrl"),
        "description": _clean_text(book.get("description")),
        "average_rating": stats.get("averageRating"),
        "ratings_count": stats.get("ratingsCount"),
        "ratings_histogram": histogram,
        "text_reviews_count": stats.get("textReviewsCount"),
        "review_languages": review_languages,
        "series": series,
        "series_position": series_position,
        "series_memberships": series_memberships,
        "pages": details.get("numPages"),
        "format": details.get("format"),
        "publisher": details.get("publisher"),
        "publication_time": details.get("publicationTime"),
        "publication_date": _ms_to_iso(details.get("publicationTime")),
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
    get_book or get_reviews. Results paginate in batches and limit is capped
    at 100.
    """
    _validate_discovery_limit(limit)
    ids = _resolve_book_ids(book_id)
    edges, has_more, _ = _paginated_graphql_edges(
        _Q_SIMILAR, "getSimilarBooks", {"id": ids["book_kca"]}, limit
    )
    books = [_book_summary(e.get("node") or {}) for e in edges]
    return {
        "book_id": ids["legacy_id"],
        "title": ids["title"],
        "returned": len(books),
        "has_more": has_more,
        "similar": books,
    }


@mcp.tool()
def author_books(book_id: str, limit: int = 20) -> dict[str, Any]:
    """List an author's works (bibliography), given any of their books.

    Resolves the book's primary author, then returns their works ranked by
    popularity. Each result has book_id/title/author/rating/url. Results
    paginate in batches and limit is capped at 100.
    """
    _validate_discovery_limit(limit)
    ids = _resolve_book_ids(book_id)
    if not ids["contributor_kca"]:
        raise ValueError(f"Could not resolve an author for book {book_id!r}.")
    edges, has_more, total_count = _paginated_graphql_edges(
        _Q_AUTHOR,
        "getWorksByContributor",
        {
            "input": {"id": ids["contributor_kca"]},
        },
        limit,
    )
    works = [_work_summary(e.get("node") or {}) for e in edges]
    return {
        "author": ids["contributor_name"],
        "author_url": ids["contributor_url"],
        "total_works": total_count,
        "returned": len(works),
        "has_more": has_more,
        "works": works,
    }


@mcp.tool()
def series_books(
    book_id: str, limit: int = 20, series_index: int = 0
) -> dict[str, Any]:
    """List the books in a series (with reading-order placement), given any
    book in that series. When a book belongs to multiple series, pass the
    zero-based series_index from get_book's series_memberships order.

    Each entry has the series 'placement' (e.g. '1', '0.5' for a prequel),
    'is_primary' (a main-sequence entry vs companion), and the usual
    book_id/title/author/rating/url. Results paginate in batches and limit is
    capped at 100.
    """
    _validate_discovery_limit(limit)
    if series_index < 0:
        raise ValueError("series_index must be zero or greater.")
    ids = _resolve_book_ids(book_id)
    memberships = ids["series_memberships"]
    if not memberships:
        return {
            "book_id": ids["legacy_id"],
            "title": ids["title"],
            "series": None,
            "note": "This book isn't part of a Goodreads series.",
            "returned": 0,
            "has_more": False,
            "books": [],
        }
    if series_index >= len(memberships):
        raise ValueError(
            f"series_index {series_index} is out of range; this book has "
            f"{len(memberships)} series membership(s)."
        )
    selected_series = memberships[series_index]
    edges, has_more, _ = _paginated_graphql_edges(
        _Q_SERIES,
        "getWorksForSeries",
        {
            "input": {"id": selected_series["id"]},
        },
        limit,
    )
    books = []
    for e in edges:
        summary = _work_summary(e.get("node") or {})
        summary["placement"] = e.get("seriesPlacement")
        summary["is_primary"] = e.get("isPrimary")
        books.append(summary)
    return {
        "series": selected_series["title"],
        "series_index": series_index,
        "returned": len(books),
        "has_more": has_more,
        "books": books,
    }


@mcp.tool()
def get_editions(book_id: str, limit: int = 20) -> dict[str, Any]:
    """List published editions of a book (formats, ISBNs, publishers, dates).

    Useful for "which edition / format / ISBN" questions. Results paginate in
    batches and limit is capped at 100.
    """
    _validate_discovery_limit(limit)
    ids = _resolve_book_ids(book_id)
    edges, has_more, total_count = _paginated_graphql_edges(
        _Q_EDITIONS, "getEditions", {"id": ids["work_kca"]}, limit
    )
    editions = []
    for e in edges:
        node = e.get("node") or {}
        details = node.get("details") or {}
        editions.append(
            {
                "book_id": node.get("legacyId"),
                "title": node.get("title"),
                "cover": node.get("imageUrl"),
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
        "total_editions": total_count,
        "returned": len(editions),
        "has_more": has_more,
        "editions": editions,
    }


@mcp.tool()
def book_lists(book_id: str, limit: int = 10) -> dict[str, Any]:
    """List the Listopia lists a book appears on (e.g. "Best Dystopian
    Fiction"), ordered by popularity.

    Each list has its title, total member votes, how many books it contains,
    and a 'url'. Good for "what kind of book is this / what's it grouped with"
    and for discovery. Results paginate in batches and limit is capped at 100.
    """
    _validate_discovery_limit(limit)
    ids = _resolve_book_ids(book_id)
    edges, has_more, _ = _paginated_graphql_edges(
        _Q_BOOK_LISTS, "getBookListsOfBook", {"id": ids["book_kca"]}, limit
    )
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
        "has_more": has_more,
        "lists": lists,
    }


@mcp.tool()
def popular_books(
    year: int,
    month: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Most popular books by release date — Goodreads' "Popular by date" chart.

    Ranks the books/works released in a given year (or a specific month of a
    year) by how many Goodreads members have added them. Mirrors the
    goodreads.com/book/popular_by_date/<year>[/<month>] page.

    year: 4-digit release year.
    month: optional 1-12 for a single month; omit for the whole year.
    limit: how many to return (capped at 50).

    Each entry has rank, count (members who added it), and the usual
    book_id/title/author/rating/url so you can chain into get_book/get_reviews.
    """
    if month is not None and not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12.")
    name = (
        f"books-by-release-date-{year}-{month}"
        if month is not None
        else f"works-by-release-date-{year}"
    )
    want = max(0, min(limit, _MAX_POPULAR))

    entries: list[dict[str, Any]] = []
    token: str | None = None
    while len(entries) < want:
        page = gr.graphql(
            _Q_TOP_LIST,
            {
                "name": name,
                "period": "A",
                "location": "ALL",
                "after": token,
                "limit": _POPULAR_PAGE_SIZE,
            },
        ).get("getTopList") or {}
        edges = page.get("edges") or []
        for edge in edges:
            if not edge or not edge.get("node"):
                continue
            entry = {"rank": edge.get("rank"), "count": edge.get("count")}
            entry.update(_node_summary(edge["node"]))
            entries.append(entry)
            if len(entries) >= want:
                break
        info = page.get("pageInfo") or {}
        token = info.get("nextPageToken")
        if not token or not edges or not info.get("hasNextPage"):
            break

    return {
        "year": year,
        "month": month,
        "returned": len(entries),
        "books": entries,
    }


@mcp.tool()
def compare_books(book_ids: list[str]) -> dict[str, Any]:
    """Compare several books side by side by rating and rating distribution.

    Fetches each book and returns them ranked best-to-worst by average rating,
    with the ratings_histogram plus 'pct_positive' (share of 4-5 star) and
    'pct_critical' (share of 1-2 star) so you can judge not just the average
    but how divisive each book is. Pass 2-10 book ids (from search_books etc.).
    """
    if not book_ids:
        raise ValueError("Provide at least one book_id to compare.")

    results: list[dict[str, Any]] = []
    for bid in book_ids[:_MAX_COMPARE]:
        try:
            b = get_book(bid)
        except Exception as e:  # noqa: BLE001 — report per-book, don't abort all
            results.append({"book_id": bid, "error": str(e)})
            continue
        hist = b.get("ratings_histogram") or {}
        total = sum(v for v in hist.values() if isinstance(v, int))
        crit = (hist.get("1") or 0) + (hist.get("2") or 0)
        pos = (hist.get("4") or 0) + (hist.get("5") or 0)
        results.append(
            {
                "book_id": b.get("book_id"),
                "title": b.get("title"),
                "author": b.get("author"),
                "average_rating": b.get("average_rating"),
                "ratings_count": b.get("ratings_count"),
                "text_reviews_count": b.get("text_reviews_count"),
                "ratings_histogram": hist or None,
                "pct_positive": round(100 * pos / total, 1) if total else None,
                "pct_critical": round(100 * crit / total, 1) if total else None,
                "url": b.get("url"),
            }
        )

    rated = [r for r in results if r.get("average_rating") is not None]
    errored = [r for r in results if "error" in r]
    rated.sort(key=lambda r: r["average_rating"], reverse=True)
    return {
        "compared": len(rated),
        "ranked_by": "average_rating (desc)",
        "books": rated + errored,
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
