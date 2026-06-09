"""LLM-backed enrichment of the news feed (cached, fail-soft).

Every public function here turns raw headlines into the small bits of insight
that make the feed feel alive: a one-paragraph theme, a single evocative word,
per-headline sentiment + topic clusters, and a live web-grounded take on the
top story. All of it is best-effort — a failed or rate-limited model call must
never crash the app, so each function degrades to a safe default and (when a
``Cache`` is supplied) memoizes successful results to avoid re-billing.

The functions take an ``LLMClient`` (see :mod:`ainews.llm`) exposing
``.model``, ``.generate``, ``.generate_json`` and ``.web_search``.
"""
import hashlib

from . import config
from .models import SENTIMENTS
from .cache import make_key


def _hash(items) -> str:
    """Stable digest of an ordered list of strings (for cache keys)."""
    joined = "\n".join(str(i) for i in (items or []))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _bulleted(headlines) -> str:
    """Render headlines as a markdown bullet list for prompting."""
    return "\n".join(f"- {h}" for h in headlines)


def theme_summary(client, headlines, cache=None) -> str:
    """Summarize the headlines into one evocative paragraph describing the
    overall atmosphere / emerging trend. Returns ``''`` on any failure."""
    try:
        headlines = list(headlines or [])[: config.MAX_HEADLINES_FOR_LLM]
        if not headlines:
            return ""

        key = make_key("theme", client.model, _hash(headlines))
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if cached is not None:
                return cached

        prompt = (
            "Summarize the following headlines into a theme to help understand "
            "the overall atmosphere and possible trend\noccurring in the style "
            "of Dan Rather but more concise and irresistible (do not mention "
            "the styling only output\nthe summary):\n\n" + _bulleted(headlines)
        )
        result = client.generate(
            prompt,
            effort=config.THEME_EFFORT,
            verbosity=config.THEME_VERBOSITY,
            max_output_tokens=config.THEME_MAX_TOKENS,
        )
        result = (result or "").strip()
        if result and cache is not None:
            cache.set(key, result)
        return result
    except Exception:
        return ""


def one_word(client, headlines, cache=None) -> str:
    """Distill the headlines into ONE thought-provoking word capturing the
    trend. Returns ``''`` on any failure."""
    try:
        headlines = list(headlines or [])[: config.MAX_HEADLINES_FOR_LLM]
        if not headlines:
            return ""

        key = make_key("one_word", client.model, _hash(headlines))
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if cached is not None:
                return cached

        prompt = (
            "Read the following AI news headlines and respond with exactly ONE "
            "thought-provoking word that summarizes the overall trend or mood. "
            "Output only that single word, nothing else:\n\n"
            + _bulleted(headlines)
        )
        result = client.generate(
            prompt,
            effort=config.ONE_WORD_EFFORT,
            verbosity=config.ONE_WORD_VERBOSITY,
            max_output_tokens=config.ONE_WORD_MAX_TOKENS,
        )
        parts = (result or "").split()
        word = parts[0] if parts else ""
        if word and cache is not None:
            cache.set(key, word)
        return word
    except Exception:
        return ""


def _classify_schema():
    """The strict JSON schema for one classification batch."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "sentiment", "topic"],
                    "properties": {
                        "index": {"type": "integer"},
                        "sentiment": {
                            "type": "string",
                            "enum": ["hype", "concern", "neutral"],
                        },
                        "topic": {"type": "string"},
                    },
                },
            }
        },
    }


def _classify_prompt(numbered) -> str:
    """The classification prompt for a numbered (local-index) headline list."""
    return (
        "Classify each of the following AI news headlines. For every "
        "headline, decide its sentiment and assign a short "
        "topic-cluster label.\n\n"
        "Sentiment:\n"
        "- hype: excitement, optimism, breakthrough, funding, launch\n"
        "- concern: risk, harm, regulation, danger, criticism, "
        "layoffs\n"
        "- neutral: anything else\n\n"
        "Topic: a SHORT cluster label (2-3 words). REUSE the same "
        "label across related items so headlines group cleanly. "
        "Suggested labels (use these where they fit): 'New Models', "
        "'Research', 'Funding & Business', 'Regulation & Policy', "
        "'Safety & Ethics', 'Hardware & Chips', 'Tools & Products', "
        "'Industry Moves'.\n\n"
        "Return one item per headline, preserving its index.\n\n"
        "Headlines:\n" + numbered
    )


def _classify_batch(client, batch, offset, cache):
    """Classify a single batch in place, mapping local→global indices.

    ``batch`` is a slice of the full articles list; ``offset`` is the index of
    ``batch[0]`` within that full list. The prompt numbers the batch with LOCAL
    indices ``0..n-1``; returned indices are mapped back to global positions.
    A failed/empty batch leaves its articles' defaults untouched.
    """
    try:
        if not batch:
            return

        titles = [a.title for a in batch]
        lines = []
        for i, a in enumerate(batch):
            title = a.title
            summary = getattr(a, "summary", "") or ""
            # Collapse newlines/whitespace so each item stays one clean line.
            summary = " ".join(summary.split())[:200]
            if summary:
                lines.append(f"{i}. {title} — {summary}")
            else:
                lines.append(f"{i}. {title}")
        numbered = "\n".join(lines)

        key = make_key("classify", client.model, _hash(titles))
        items = None
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if cached is not None:
                items = cached

        if items is None:
            data = client.generate_json(
                _classify_prompt(numbered),
                _classify_schema(),
                effort=config.CLASSIFY_EFFORT,
                verbosity=config.CLASSIFY_VERBOSITY,
                max_output_tokens=config.CLASSIFY_MAX_TOKENS,
            )
            if isinstance(data, dict):
                items = data.get("items")
            else:
                items = data
            if not isinstance(items, list):
                return
            if cache is not None:
                cache.set(key, items)

        if not isinstance(items, list):
            return

        for item in items:
            try:
                local = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= local < len(batch)):
                continue
            article = batch[local]  # local idx -> global via the batch slice
            sentiment = item.get("sentiment")
            article.sentiment = (
                sentiment if sentiment in SENTIMENTS else "neutral"
            )
            topic = item.get("topic")
            topic = topic.strip() if isinstance(topic, str) else ""
            article.topic = topic or "General"
    except Exception:
        # Leave this batch's defaults in place — classification is best-effort.
        return


def classify_articles(client, articles, cache=None) -> None:
    """Classify up to ``MAX_ARTICLES_TO_CLASSIFY`` articles, mutating each
    ``Article`` in place with ``.sentiment`` and ``.topic``.

    Work is split into batches of ``CLASSIFY_BATCH_SIZE`` so each structured
    call stays bounded and reliable. Each batch is cached and applied
    independently: a failed or empty batch leaves only its own articles at
    their defaults and never aborts the others. On any failure, the affected
    articles simply keep their existing defaults."""
    try:
        articles = articles or []
        subset = articles[: config.MAX_ARTICLES_TO_CLASSIFY]
        if not subset:
            return

        batch_size = max(1, int(config.CLASSIFY_BATCH_SIZE))
        for offset in range(0, len(subset), batch_size):
            batch = subset[offset:offset + batch_size]
            # ``batch`` aliases the live Article objects, so mutating them in
            # ``_classify_batch`` updates ``articles`` in place.
            _classify_batch(client, batch, offset, cache)
    except Exception:
        # Leave all defaults in place — classification is best-effort.
        return


def ground_article(client, article, cache=None):
    """Fact-check and add current, web-sourced context to a SPECIFIC article.

    Returns ``{"headline", "markdown", "citations"}`` or ``None`` on failure.
    """
    try:
        if not article:
            return None

        key = make_key(
            "ground", getattr(client, "model", "-"), _hash([article.title])
        )
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if cached is not None:
                return cached

        prompt = (
            "Using live web search, fact-check and add 2-3 sentences of "
            "current context (as of today) to this AI news headline, citing "
            "sources. Output ONLY the concise fact-check and context as plain "
            "prose — no headings, no bullet lists, and do NOT offer follow-ups "
            "or ask the reader questions.\n\n"
            f"Headline: {article.title}\nSource: {article.feed_name}"
        )
        result = client.web_search(
            prompt,
            effort=config.GROUNDING_EFFORT,
            verbosity=config.GROUNDING_VERBOSITY,
            max_output_tokens=config.GROUNDING_MAX_TOKENS,
        )
        if not result:
            return None

        out = {
            "headline": article.title,
            "markdown": (result.get("text") or "").strip(),
            "citations": result.get("citations") or [],
        }
        if out["markdown"] and cache is not None:
            cache.set(key, out)
        return out
    except Exception:
        return None


def ask_feed(client, question, articles, theme="", grounding=None, cache=None):
    """Answer a free-form ``question`` about today's AI news.

    Uses the supplied ``articles`` (up to 40 headlines) as primary context and
    live web search when needed, then returns
    ``{"text": str, "citations": [{"title", "url"}, ...]}``. Best-effort: any
    failure yields ``{"text": "(could not answer: <err>)", "citations": []}``.

    ``grounding`` (optional) is the top-story fact-check dict from
    :func:`ground_article`; its markdown is folded into the prompt for extra
    current context. Successful answers are cached when a ``Cache`` is supplied.
    """
    try:
        question = (question or "").strip()
        if not question:
            return {"text": "", "citations": []}

        model = config.CHAT_MODEL
        key = make_key("ask", model, _hash([question, theme]))
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if isinstance(cached, dict) and "text" in cached:
                return cached

        # Headlines as primary context: "- {title} ({feed_name}) [{topic}]".
        lines = []
        for a in list(articles or [])[:40]:
            try:
                title = getattr(a, "title", "") or ""
                if not title:
                    continue
                feed_name = getattr(a, "feed_name", "") or ""
                topic = getattr(a, "topic", "") or ""
                lines.append(f"- {title} ({feed_name}) [{topic}]")
            except Exception:
                continue
        headlines_block = "\n".join(lines)

        theme_block = f"THEME: {theme}\n\n" if theme else ""
        grounding_block = ""
        if isinstance(grounding, dict):
            md = (grounding.get("markdown") or "").strip()
            if md:
                grounding_block = (
                    "Additional grounded context on the top story:\n"
                    + md + "\n\n"
                )

        prompt = (
            "You are a sharp research assistant answering a question about "
            "TODAY's AI news. Use the provided headlines below as your primary "
            "context, and add live web search ONLY when needed to answer "
            "accurately or with current detail. Be concise, write in markdown, "
            "and cite your sources.\n\n"
            + theme_block
            + grounding_block
            + "Today's headlines:\n" + headlines_block + "\n\n"
            + f"Question: {question}"
        )

        result = client.web_search(
            prompt,
            model=model,
            effort=config.CHAT_EFFORT,
            verbosity=config.CHAT_VERBOSITY,
            max_output_tokens=config.CHAT_MAX_TOKENS,
        )
        if not result:
            return {"text": "(could not answer: no response)", "citations": []}

        out = {
            "text": (result.get("text") or "").strip(),
            "citations": result.get("citations") or [],
        }
        if out["text"] and cache is not None:
            cache.set(key, out)
        return out
    except Exception as exc:  # noqa: BLE001 - never crash the TUI
        return {"text": f"(could not answer: {exc})", "citations": []}
