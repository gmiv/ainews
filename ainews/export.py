"""Markdown digest export for the AI news feed.

Turns a run's articles plus its LLM-derived metadata (theme, one-word, live
top-story grounding) into a single, shareable Markdown document. This is the
"take it with you" surface: bookmarks float to the top, then everything else is
grouped by topic with read (✓) and bookmark (★) markers so the file mirrors what
you saw in the TUI. Pure string assembly + one file write — no curses, no LLM —
so it can be unit-tested and called from anywhere.
"""
import os
from datetime import datetime

from . import config


def _md_escape(text):
    """Neutralize the few characters that would break a Markdown link label.

    We keep this intentionally light: digests are meant to be read, not parsed,
    so we only guard the brackets/pipes that would visibly mangle a ``[t](u)``
    link or a list line. Anything non-string is coerced defensively.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return text.replace("[", "\\[").replace("]", "\\]")


def _link(title, url):
    """Render a Markdown link, degrading to plain text when there's no URL."""
    label = _md_escape((title or url or "").strip()) or "(untitled)"
    url = (url or "").strip()
    return f"[{label}]({url})" if url else label


def export_markdown(articles, *, theme="", one_word="", grounding=None,
                    source_counts=None, top_words=None, path=None, title=None):
    """Write a Markdown digest of ``articles`` and return its absolute path.

    Args:
        articles: iterable of ``Article`` (caller pre-sorted newest-first).
        theme: optional GPT theme summary (Markdown allowed; emitted verbatim).
        one_word: optional one-word characterization of the day.
        grounding: optional dict with ``markdown`` text and a ``citations`` list
            of ``{"title", "url"}`` for the top story's live context.
        source_counts / top_words: accepted for signature symmetry with the rest
            of the pipeline; not currently surfaced in the digest body.
        path: explicit output path; defaults to a timestamped file under
            ``config.EXPORT_DIR``.
        title: optional document title override (sans the date suffix).

    Returns:
        The absolute path of the written file.

    Raises:
        OSError: if the directory cannot be created or the file cannot be
            written (the caller is expected to guard this).
    """
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    when = now.strftime("%Y-%m-%d %H:%M")

    if path is None:
        path = os.path.join(config.EXPORT_DIR, f"ai_news_digest_{stamp}.md")

    articles = list(articles or [])
    parts = []

    # --- Title ---------------------------------------------------------------
    heading = (title or "AI News Digest").strip() or "AI News Digest"
    parts.append(f"# {heading} — {when}")
    parts.append("")

    # --- One word ------------------------------------------------------------
    one_word = (one_word or "").strip()
    if one_word:
        parts.append(f"_One word: **{one_word}**_")
        parts.append("")

    # --- Theme ---------------------------------------------------------------
    theme = (theme or "").strip()
    if theme:
        parts.append("## Theme")
        parts.append("")
        parts.append(theme)
        parts.append("")

    # --- Top Story · Live Context (grounding) --------------------------------
    if isinstance(grounding, dict):
        gm = (grounding.get("markdown") or "").strip()
        citations = grounding.get("citations") or []
        if gm or citations:
            parts.append("## Top Story · Live Context")
            parts.append("")
            if gm:
                parts.append(gm)
                parts.append("")
            if citations:
                parts.append("**Sources:**")
                parts.append("")
                for c in citations:
                    if not isinstance(c, dict):
                        continue
                    url = (c.get("url") or "").strip()
                    ctitle = (c.get("title") or url).strip()
                    if not (ctitle or url):
                        continue
                    parts.append(f"- {_link(ctitle, url)}")
                parts.append("")

    # --- ★ Bookmarks ---------------------------------------------------------
    bookmarked = [a for a in articles if getattr(a, "bookmarked", False)]
    if bookmarked:
        parts.append("## ★ Bookmarks")
        parts.append("")
        for a in bookmarked:
            feed = getattr(a, "feed_name", "") or ""
            topic = getattr(a, "topic", "") or ""
            meta = " · ".join(p for p in (feed, topic) if p)
            line = f"- {_link(getattr(a, 'title', ''), getattr(a, 'link', ''))}"
            if meta:
                line += f" — {meta}"
            parts.append(line)
        parts.append("")

    # --- All Stories (grouped by topic) --------------------------------------
    parts.append("## All Stories")
    parts.append("")
    if not articles:
        parts.append("_No stories._")
        parts.append("")
    else:
        # Group preserving first-seen topic order (caller already sorted within).
        groups = {}
        order = []
        for a in articles:
            topic = (getattr(a, "topic", "") or "General").strip() or "General"
            if topic not in groups:
                groups[topic] = []
                order.append(topic)
            groups[topic].append(a)

        for topic in order:
            parts.append(f"### {topic}")
            parts.append("")
            for a in groups[topic]:
                feed = getattr(a, "feed_name", "") or ""
                line = f"- {_link(getattr(a, 'title', ''), getattr(a, 'link', ''))}"
                if feed:
                    line += f" — {feed}"
                if getattr(a, "read", False):
                    line += " ✓"
                if getattr(a, "bookmarked", False):
                    line += " ★"
                parts.append(line)
            parts.append("")

    content = "\n".join(parts).rstrip("\n") + "\n"

    # --- Write (let OSError propagate so the caller can surface it) -----------
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)

    return os.path.abspath(path)
