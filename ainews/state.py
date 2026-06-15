"""FeedState: the controller the curses UI talks to.

Architecture v2.1 shifts from "render a static pre-wrapped list" to "a
FeedState the UI queries and mutates, re-wrapping on change/resize". This module
owns the data (articles + the cheap/LLM analysis bundles) together with the four
view options (search, source filter, bookmarks-only, unread-only) and a persist
``Store`` for bookmarks / read-state.

The UI never touches articles or the store directly: it calls ``render_lines``
to get fresh line-items, ``visible_articles`` for the current view, the mutators
to change filters or toggle bookmark/read, and ``export`` to write a digest.
Every method is defensive — a failure in persistence or export must never crash
the TUI.
"""
from . import config
from . import export
from . import colors, glyphs


class FeedState:
    """Holds the feed data + view options and answers the UI's queries.

    The articles are assumed to arrive pre-sorted newest-first; that input order
    is preserved through every filter. ``source_counts``/``top_words``/``theme``/
    ``one_word``/``grounding`` are the analysis bundles passed straight through to
    the view-model (and the Markdown export). ``store`` is an optional
    ``persist.Store`` used to persist bookmarks and read-state by Article key.
    """

    def __init__(self, articles, source_counts, top_words,
                 theme="", one_word="", grounding=None, store=None,
                 headline_of_day=None, cache=None, leaderboard=None,
                 client=None, mastery=None):
        # Defensive normalisation: tolerate None for any of the collections.
        self.articles = list(articles or [])
        self.source_counts = source_counts or {}
        self.top_words = list(top_words or [])
        self.theme = theme or ""
        self.one_word = one_word or ""
        self.grounding = grounding
        self.store = store
        # Shared disk cache (used by the in-app reader to memoize scrapes).
        self.cache = cache
        # The day's single most important story, scrolled in the marquee
        # (None when there is nothing to feature).
        self.headline_of_day = headline_of_day
        # The ranked top-stories leaderboard (list of dicts) used by the
        # marquee/overview, and the optional LLM client used by the chat overlay.
        self.leaderboard = list(leaderboard or [])
        self.client = client
        # The deliberate-practice layer (mastery.MasteryStore) — the Socratic
        # tutor + knowledge-graph read/write it. Optional; None disables them.
        self.mastery = mastery

        # Reflect any persisted bookmarks / read-state onto the in-memory
        # articles so markers and the unread/bookmarks filters are correct on
        # launch (not just after an in-session toggle).
        if self.store is not None:
            try:
                self.store.apply(self.articles)
            except Exception:  # noqa: BLE001 - never crash the TUI
                pass

        # Derive the per-topic ordering / color / emoji maps from the full
        # article set so every view shares one stable, color-coded topic axis.
        self._compute_topics()

        # View options — all start empty / off.
        self.search_query = ""
        self.source_filter = None
        self.bookmarks_only = False
        self.unread_only = False

    # --- Topic axis ---------------------------------------------------------
    def _compute_topics(self):
        """Derive the stable per-topic order + color + emoji maps.

        Counts every article's ``topic`` (falling back to ``"General"`` when an
        article is unclassified) across the FULL article set, then orders the
        topics so the catch-all ``"General"`` bucket sinks to the bottom, more
        populous topics float up, and ties break alphabetically. From that
        ordering we assign each topic a wrapping palette color-pair id and its
        emoji once, so the marquee, mix chart and every feed row share one
        consistent color/emoji axis. Defensive — never crashes the TUI.
        """
        counts = {}
        try:
            for a in self.articles:
                topic = (getattr(a, "topic", None) or "General")
                counts[topic] = counts.get(topic, 0) + 1
        except Exception:  # noqa: BLE001 - never crash the TUI
            counts = {}
        order = sorted(
            counts,
            key=lambda t: (t == "General", -counts[t], t),
        )
        self.topic_counts = counts
        self.topic_order = order
        self.topic_colors = {
            t: colors.topic_color_id(i) for i, t in enumerate(order)
        }
        self.topic_emojis = {t: glyphs.topic_emoji(t) for t in order}

    # --- Queries ------------------------------------------------------------
    def sources(self):
        """Return ``(name, count)`` pairs, most prolific source first.

        Ties keep alphabetical order so the picker is stable across runs.
        """
        return sorted(
            self.source_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )

    def is_filtering(self):
        """True iff any of the four view options is active."""
        return bool(
            self.search_query
            or self.source_filter
            or self.bookmarks_only
            or self.unread_only
        )

    def _matches(self, article):
        """AND-substring search across title + summary + feed_name + topic.

        Lowercases the query, splits on whitespace into terms; the article
        matches iff EVERY term is a substring of the combined haystack. An empty
        query matches everything.
        """
        query = self.search_query.strip().lower()
        if not query:
            return True
        haystack = "{} {} {} {}".format(
            article.title or "",
            article.summary or "",
            article.feed_name or "",
            article.topic or "",
        ).lower()
        return all(term in haystack for term in query.split())

    def visible_articles(self):
        """The full article list filtered by the active view options + search.

        Applies, in order: source_filter (exact feed_name), bookmarks_only
        (article.bookmarked), unread_only (not article.read), then the search
        match. Preserves the input order (caller pre-sorted newest-first).
        """
        out = []
        for a in self.articles:
            if self.source_filter is not None and a.feed_name != self.source_filter:
                continue
            if self.bookmarks_only and not a.bookmarked:
                continue
            if self.unread_only and a.read:
                continue
            if not self._matches(a):
                continue
            out.append(a)
        return out

    def topic_groups(self):
        """Return ``[(topic, [Article, ...]), ...]`` over the VISIBLE articles.

        Topics follow the stable global ``topic_order``; only topics with at
        least one currently-visible article are included, and within each the
        articles keep their newest-first input order. This drives the two-pane
        browser (left = topics, right = that topic's stories).
        """
        groups = {}
        for a in self.visible_articles():
            groups.setdefault(a.topic or "General", []).append(a)
        ordered = [(t, groups[t]) for t in self.topic_order if t in groups]
        # Defensive: include any topic missing from topic_order, at the end.
        for t in groups:
            if t not in self.topic_order:
                ordered.append((t, groups[t]))
        return ordered

    def sentiment_counts(self):
        """Return ``{'hype': n, 'concern': n, 'neutral': n}`` over all articles."""
        counts = {"hype": 0, "concern": 0, "neutral": 0}
        for a in self.articles:
            s = getattr(a, "sentiment", "neutral")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def status_lines(self):
        """One-line summary of active filters + "N/M shown".

        Returns ``[]`` when no filter is active so the banner stays clean.
        """
        if not self.is_filtering():
            return []
        parts = []
        if self.search_query:
            parts.append('search "{}"'.format(self.search_query))
        if self.source_filter:
            parts.append("source {}".format(self.source_filter))
        if self.bookmarks_only:
            parts.append("bookmarks only")
        if self.unread_only:
            parts.append("unread only")
        shown = len(self.visible_articles())
        total = len(self.articles)
        summary = "Filter: {} · {}/{} shown".format(
            " · ".join(parts), shown, total
        )
        return [summary]

    # --- Filter mutators ----------------------------------------------------
    def set_search(self, q):
        """Set the search query; an empty/whitespace value clears it."""
        self.search_query = (q or "").strip()

    def set_source(self, name_or_None):
        """Filter to a single feed by name; ``None`` shows all sources."""
        self.source_filter = name_or_None or None

    def toggle_bookmarks_only(self):
        """Flip the bookmarks-only filter; returns the new state."""
        self.bookmarks_only = not self.bookmarks_only
        return self.bookmarks_only

    def toggle_unread_only(self):
        """Flip the unread-only filter; returns the new state."""
        self.unread_only = not self.unread_only
        return self.unread_only

    def clear_filters(self):
        """Reset all four view options to their empty/off defaults."""
        self.search_query = ""
        self.source_filter = None
        self.bookmarks_only = False
        self.unread_only = False

    # --- Per-article state mutators (persisted) -----------------------------
    def toggle_bookmark(self, article):
        """Flip ``article.bookmarked``, mirror to the store, and save.

        Returns the new bookmarked state. If a ``store`` is present it is the
        source of truth for the flip (keeping the JSON and the in-memory article
        in lock-step); otherwise the article is toggled in memory only.
        """
        if self.store is not None:
            try:
                new_state = self.store.toggle_bookmark(article.key)
            except Exception:  # noqa: BLE001 - never crash the TUI
                new_state = not article.bookmarked
        else:
            new_state = not article.bookmarked
        article.bookmarked = new_state
        return new_state

    def toggle_read(self, article):
        """Flip ``article.read``, mirror to the store, and save.

        Returns the new read state. Mirrors ``toggle_bookmark``'s store-first
        semantics so persistence and memory never drift apart.
        """
        if self.store is not None:
            try:
                new_state = self.store.toggle_read(article.key)
            except Exception:  # noqa: BLE001 - never crash the TUI
                new_state = not article.read
        else:
            new_state = not article.read
        article.read = new_state
        return new_state

    def mark_read(self, article):
        """Idempotently mark ``article`` read, mirror to the store, and save."""
        article.read = True
        if self.store is not None:
            try:
                self.store.mark_read(article.key)
            except Exception:  # noqa: BLE001 - never crash the TUI
                pass

    # --- Export -------------------------------------------------------------
    def export(self, path=None):
        """Write a Markdown digest of ALL articles (+meta) and return its path.

        Always exports the complete article list — never just the current view —
        so a digest captures everything regardless of active filters. Delegates
        to ``export.export_markdown``; the caller guards against OSError.
        """
        return export.export_markdown(
            self.articles,
            theme=self.theme,
            one_word=self.one_word,
            grounding=self.grounding,
            source_counts=self.source_counts,
            top_words=self.top_words,
            path=path,
        )
