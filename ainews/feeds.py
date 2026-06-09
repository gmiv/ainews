"""RSS fetching, date filtering, and de-duplication.

This module turns a list of feed URLs into a flat list of :class:`Article`
objects. Feeds are fetched concurrently (network-bound work), tolerant of
individual feed failures, and optionally backed by the on-disk cache so quick
relaunches don't re-hit the network. Everything degrades rather than crashes:
a broken feed is skipped, a cache miss falls through to a live fetch.
"""
import re
import time
import socket
import difflib
import concurrent.futures
from urllib.parse import urlparse

import feedparser

from . import config
from .models import Article
from .cache import make_key


def strip_html_tags(text) -> str:
    """Remove HTML tags from a string, returning plain text."""
    return re.sub(r"<.*?>", "", text or "")


def _parse_feed(url):
    """Fetch and parse a single feed into ``(feed_name, [Article, ...])``.

    Returns the feed's display name plus its articles. Raises on any failure
    so the caller can decide to skip the feed; never returns partial garbage.
    """
    parsed = feedparser.parse(url)

    feed_title = getattr(parsed.feed, "title", "") if getattr(parsed, "feed", None) else ""
    feed_name = (feed_title or "").strip() or urlparse(url).netloc or "Unknown"

    # Cap entries per feed: some sources return enormous payloads (arXiv ~300,
    # TechNode ~2000) that would both drown the curated feed and slow everything
    # downstream. Keep only the most recent handful per source.
    limit = getattr(config, "MAX_ENTRIES_PER_FEED", None)
    entries = getattr(parsed, "entries", []) or []
    if limit:
        entries = entries[:limit]

    articles = []
    for entry in entries:
        # Some feeds (e.g. Slashdot's RDF) carry only ``updated`` dates, not
        # ``published`` — fall back so they aren't silently dropped by the date
        # filter. Without this, an entire date-less source vanishes.
        parsed_ts = entry.get("published_parsed") or entry.get("updated_parsed")
        try:
            published_ts = time.mktime(parsed_ts) if parsed_ts else None
        except (TypeError, ValueError, OverflowError):
            published_ts = None

        articles.append(Article(
            title=(entry.get("title", "No Title") or "No Title").strip(),
            published=(entry.get("published") or entry.get("updated") or "No Date"),
            published_ts=published_ts,
            feed_name=feed_name,
            summary=strip_html_tags(entry.get("summary", "")),
            link=entry.get("link", ""),
        ))

    return feed_name, articles


def fetch_all_feeds(feed_urls=None, workers=None, cache=None, progress=None):
    """Fetch every feed concurrently and return a combined list of articles.

    Args:
        feed_urls: feeds to fetch (defaults to ``config.AI_NEWS_FEEDS``).
        workers: thread-pool size (defaults to ``config.FETCH_WORKERS``).
        cache: optional :class:`~ainews.cache.Cache`; a hit short-circuits the
            network fetch. A miss is populated after a successful fetch.
        progress: optional callable ``progress(done, total, name)`` invoked once
            per finished feed (and once with ``'(cache)'`` on a cache hit).

    The result is unsorted; per-feed failures are swallowed (the feed is
    skipped) so one dead source never sinks the run.
    """
    if feed_urls is None:
        feed_urls = config.AI_NEWS_FEEDS
    if workers is None:
        workers = config.FETCH_WORKERS

    feed_urls = list(feed_urls)
    total = len(feed_urls)

    # Cache lookup: keyed on the (order-independent) set of feed URLs.
    cache_key = make_key("feeds", *sorted(feed_urls))
    if cache is not None:
        try:
            cached = cache.get(cache_key, config.FEED_CACHE_TTL)
        except Exception:
            cached = None
        if cached is not None:
            articles = []
            for d in cached:
                try:
                    articles.append(Article.from_dict(d))
                except Exception:
                    continue
            if progress is not None:
                try:
                    progress(total, total, "(cache)")
                except Exception:
                    pass
            return articles

    # Best-effort per-feed socket timeout so a hung server can't stall the pool.
    try:
        socket.setdefaulttimeout(config.FETCH_TIMEOUT)
    except Exception:
        pass

    articles = []
    done = 0

    # ThreadPoolExecutor: feed fetching is I/O-bound, so threads parallelize well.
    if total:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_to_url = {executor.submit(_parse_feed, url): url for url in feed_urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                name = url
                try:
                    name, feed_articles = future.result()
                    articles.extend(feed_articles)
                except Exception:
                    # Skip this feed entirely; one bad source must not crash us.
                    pass
                done += 1
                if progress is not None:
                    try:
                        progress(done, total, name)
                    except Exception:
                        pass

    if cache is not None:
        try:
            cache.set(cache_key, [a.to_dict() for a in articles])
        except Exception:
            pass

    return articles


def filter_by_date(articles, hours=None):
    """Keep only articles published within the last ``hours`` (default config).

    Articles lacking a parseable timestamp are dropped, since we can't vouch
    for their recency.
    """
    if hours is None:
        hours = config.LOOKBACK_HOURS
    cutoff = time.time() - hours * 3600
    return [
        a for a in articles
        if a.published_ts is not None and a.published_ts >= cutoff
    ]


def _normalize_title(title) -> str:
    """Lowercase and strip a title down to ``[a-z0-9 ]`` for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower())


def dedupe(articles, threshold=None):
    """Remove exact and near-duplicate articles, keeping the first occurrence.

    First drops exact ``.key`` duplicates (same link/title), then drops
    near-duplicate *titles* whose normalized similarity ratio meets/exceeds
    ``threshold`` (default ``config.DEDUPE_SIMILARITY``). Callers are expected
    to pre-sort newest-first so the surviving copy is the freshest. O(n^2) is
    fine for the few-hundred articles we ever see.
    """
    if threshold is None:
        threshold = config.DEDUPE_SIMILARITY

    # We track the set of DISTINCT outlets (feed names) that ran each surviving
    # story. cluster_size = len(that set) is the corroboration signal — counting
    # distinct outlets (not raw copies) stops one chatty source from inflating a
    # story, mirroring how news aggregators measure prominence.
    outlets = {}  # id(survivor) -> set(feed_name)

    # Pass 1: exact identity de-dup via Article.key.
    by_key = {}
    exact = []
    for a in articles:
        k = a.key
        rep = by_key.get(k)
        if rep is not None:
            outlets[id(rep)].add(a.feed_name)
            continue
        by_key[k] = a
        outlets[id(a)] = {a.feed_name}
        exact.append(a)

    # Pass 2: fuzzy title de-dup against already-kept titles; the kept
    # representative absorbs the merged copy's outlet set.
    kept = []
    kept_titles = []
    for a in exact:
        norm = _normalize_title(a.title)
        dup_of = None
        for i, prev in enumerate(kept_titles):
            if difflib.SequenceMatcher(None, norm, prev).ratio() >= threshold:
                dup_of = i
                break
        if dup_of is None:
            kept.append(a)
            kept_titles.append(norm)
        else:
            outlets[id(kept[dup_of])] |= outlets.get(id(a), {a.feed_name})

    # Finalise: stamp each survivor's distinct-outlet count.
    for a in kept:
        a.cluster_size = max(1, len(outlets.get(id(a), {a.feed_name})))

    return kept
