"""Cross-run persistence for bookmarks and read-state.

A single JSON file (``config.STATE_FILE``) records, by ``Article.key``, which
stories the reader has starred and which they have already seen. The store is
deliberately forgiving: a missing or corrupt file simply yields empty sets, and
every write swallows its errors so persistence can never crash the TUI. Identity
is the article key (link-or-title, lowercased), so state survives feed re-fetches
and re-ordering.
"""
import json
import os

from . import config


class Store:
    """JSON-backed sets of bookmarked / read ``Article.key`` values."""

    def __init__(self, path=None):
        self.path = path or config.STATE_FILE
        self.bookmarks = set()
        self.read = set()
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self) -> None:
        """Populate the sets from disk; tolerate missing/corrupt files."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict):
            return
        # Coerce to str so keys always compare against Article.key cleanly.
        bookmarks = blob.get("bookmarks")
        read = blob.get("read")
        if isinstance(bookmarks, (list, tuple, set)):
            self.bookmarks = {str(k) for k in bookmarks}
        if isinstance(read, (list, tuple, set)):
            self.read = {str(k) for k in read}

    def save(self) -> None:
        """Write the sets to disk as sorted lists; never raise."""
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                "bookmarks": sorted(self.bookmarks),
                "read": sorted(self.read),
            }
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except (OSError, TypeError):
            pass

    # --- queries ------------------------------------------------------------
    def is_bookmarked(self, key) -> bool:
        return key in self.bookmarks

    def is_read(self, key) -> bool:
        return key in self.read

    # --- mutations ----------------------------------------------------------
    def toggle_bookmark(self, key) -> bool:
        """Flip the bookmark for ``key``; persist; return the new state."""
        if key in self.bookmarks:
            self.bookmarks.discard(key)
            state = False
        else:
            self.bookmarks.add(key)
            state = True
        self.save()
        return state

    def toggle_read(self, key) -> bool:
        """Flip the read flag for ``key``; persist; return the new state."""
        if key in self.read:
            self.read.discard(key)
            state = False
        else:
            self.read.add(key)
            state = True
        self.save()
        return state

    def mark_read(self, key) -> None:
        """Idempotently mark ``key`` as read; persist only on change."""
        if key not in self.read:
            self.read.add(key)
            self.save()

    # --- projection ---------------------------------------------------------
    def apply(self, articles) -> None:
        """Stamp each article's ``bookmarked`` / ``read`` from the stored sets."""
        for article in articles:
            key = article.key
            article.bookmarked = key in self.bookmarks
            article.read = key in self.read
