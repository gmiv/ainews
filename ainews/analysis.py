"""Pure, non-LLM statistics over a batch of articles.

These helpers are deterministic and dependency-free (stdlib only): they never
touch the network or the model, so the UI can lean on them for cheap, instant
summaries (which feeds contributed, what words dominate the headlines) even when
LLM enrichment is disabled or unavailable.
"""
import re
from collections import Counter

# Headline noise: articles, prepositions, pronouns, auxiliaries, and connectors
# that carry no topical signal. ``ai`` is excluded by design — every headline in
# an AI-news feed contains it, so it tells us nothing about what's trending.
STOPWORDS = frozenset({
    "the", "of", "and", "it", "to", "in", "a", "is", "for", "on", "that", "at",
    "with", "by", "be", "this", "an", "are", "as", "from", "or", "not", "have",
    "has", "was", "were", "but", "can", "-", "if", "will", "all", "our", "their",
    "your", "how", "what", "when", "which", "why", "up", "down", "over", "under",
    "out", "into", "etc", "about", "i", "you", "we", "they", "his", "her", "he",
    "she", "me", "too", "ai",
})

# Tokenizer: alphabetic runs only, so punctuation, digits, and symbols drop out.
_WORD_RE = re.compile(r"[A-Za-z]+")


def summarize_sources(articles) -> dict:
    """Count how many articles came from each feed.

    Returns a mapping of ``article.feed_name`` -> count. Articles with a
    missing/empty feed name are tolerated and grouped under whatever value they
    carry (typically the empty string), never raising.
    """
    counts: Counter = Counter()
    for article in articles or []:
        try:
            counts[article.feed_name] += 1
        except Exception:
            # Defensive: a malformed item must not break the whole summary.
            continue
    return dict(counts)


def top_words_in_headlines(articles, top_n: int = 6):
    """Return the ``top_n`` most frequent meaningful words across all titles.

    Words are lowercased and extracted with ``[A-Za-z]+``; stopwords (see
    ``STOPWORDS``) and single-character tokens are discarded. The result is a
    list of ``(word, count)`` tuples ordered most-common first, exactly as
    produced by ``Counter.most_common``.
    """
    counts: Counter = Counter()
    for article in articles or []:
        try:
            title = (article.title or "").lower()
        except Exception:
            # Skip items without a usable title rather than aborting.
            continue
        for word in _WORD_RE.findall(title):
            if len(word) <= 1 or word in STOPWORDS:
                continue
            counts[word] += 1
    return counts.most_common(top_n)
