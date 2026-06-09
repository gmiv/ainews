"""Network- and API-key-free tests over the deterministic core.

These lock in the behaviors the whole app's "never crashes, always degrades"
design leans on: de-dup corroboration math, date filtering, offline importance
ranking, domain authority, display-width accounting, and the markdown parser.
Run with ``pytest`` from the repo root.
"""
import time

from ainews.models import Article
from ainews import (
    feeds, importance, markdown_render, textwidth, glyphs, analysis, colors,
)


def _art(title, feed="Feed", link="http://example.com/x", topic="General",
         sentiment="neutral", ts=None, cluster_size=1):
    a = Article(title, "date", ts, feed, "a summary", link, sentiment, topic)
    a.cluster_size = cluster_size
    return a


def test_article_roundtrip():
    a = _art("Hello", cluster_size=3)
    a.bookmarked = True
    b = Article.from_dict(a.to_dict())
    assert b.title == "Hello" and b.cluster_size == 3 and b.bookmarked is True


def test_dedupe_counts_distinct_outlets():
    now = time.time()
    arts = [
        _art("OpenAI launches a new model", feed="Ars", link="http://a", ts=now),
        _art("OpenAI launches a new model!", feed="Verge", link="http://b", ts=now - 1),
        _art("An entirely unrelated story", feed="Ars", link="http://c", ts=now - 2),
    ]
    kept = feeds.dedupe(arts)
    assert len(kept) == 2
    top = next(k for k in kept if k.title.startswith("OpenAI"))
    assert top.cluster_size == 2  # merged across two distinct outlets


def test_filter_by_date_drops_old_and_undated():
    now = time.time()
    arts = [
        _art("recent", ts=now - 3600),
        _art("ancient", ts=now - 1000 * 3600),
        _art("undated", ts=None),
    ]
    titles = {a.title for a in feeds.filter_by_date(arts, hours=96)}
    assert titles == {"recent"}


def test_importance_ranked_offline():
    now = time.time()
    arts = [_art(f"story {i}", link=f"http://x{i}.com", ts=now - i,
                 cluster_size=(i % 3) + 1) for i in range(8)]
    ranked = importance.most_important_ranked(arts, "a theme", None, None, k=5)
    assert len(ranked) == 5
    assert all(0 <= it["index"] < 8 and it["reason"] for it in ranked)
    idx, reason = importance.most_important(arts, "a theme", None, None)
    assert 0 <= idx < 8 and isinstance(reason, str)


def test_authority_by_domain():
    assert importance._authority(_art("t", link="https://www.openai.com/x")) == 1.0
    assert importance._authority(_art("t", link="https://nope.example/y")) == 0.8


def test_textwidth_counts_emoji_as_two():
    assert textwidth.width("AI") == 2
    assert textwidth.width("🚀") == 2
    assert textwidth.width("a🚀") == 3
    assert textwidth.clip("a🚀b", 2) == "a"  # can't fit the wide glyph in 1 cell


def test_markdown_headings_and_inline():
    segs = markdown_render.parse_markdown_line("# Title")
    assert any(c == colors.H1 for _, c in segs)
    segs = markdown_render.parse_markdown_line("a **bold** b *it* c `code`")
    cids = {c for _, c in segs}
    assert colors.BOLD in cids and colors.ITALIC in cids and colors.CODE in cids


def test_rank_badges():
    assert glyphs.rank_badge(1) == "①"
    assert glyphs.rank_badge(3) == "③"


def test_top_words_ignores_stopwords():
    arts = [_art("model model breakthrough"), _art("model funding round")]
    words = dict(analysis.top_words_in_headlines(arts, top_n=5))
    assert words.get("model", 0) >= 2
    assert "the" not in words
