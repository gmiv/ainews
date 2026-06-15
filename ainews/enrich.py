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


def _history_block(history) -> str:
    """Render prior Q&A turns as compact conversation context for follow-ups.

    ``history`` is the chat overlay's transcript: a list of
    ``{"question": str, "answer": {"text", "citations"}}`` turns, oldest first,
    EXCLUDING the question now being asked. Bounded by
    ``config.CHAT_HISTORY_MAX_TURNS`` (recent turns kept) and
    ``config.CHAT_HISTORY_ANSWER_CHARS`` (each prior answer truncated) so a long
    conversation can't blow the token budget. Returns ``""`` when there's no
    usable history, so callers can fold it in unconditionally.
    """
    turns = [t for t in (history or []) if isinstance(t, dict)]
    if not turns:
        return ""
    max_turns = getattr(config, "CHAT_HISTORY_MAX_TURNS", 10)
    if max_turns and len(turns) > max_turns:
        turns = turns[-max_turns:]
    ans_cap = getattr(config, "CHAT_HISTORY_ANSWER_CHARS", 1200)

    lines = []
    for t in turns:
        q = (t.get("question") or "").strip()
        answer = t.get("answer") or {}
        # ``answer.get("text", "")`` would still yield None for {"text": None},
        # so coerce explicitly before stripping.
        a = ((answer.get("text") or "") if isinstance(answer, dict)
             else str(answer)).strip()
        if ans_cap and len(a) > ans_cap:
            a = a[:ans_cap].rstrip() + " …"
        if q:
            lines.append("User: " + q)
        if a:
            lines.append("Assistant: " + a)
    if not lines:
        return ""
    return "Conversation so far (oldest first):\n" + "\n".join(lines) + "\n\n"


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


def ask_feed(client, question, articles, theme="", grounding=None, cache=None,
             history=None):
    """Answer a free-form ``question`` about today's AI news.

    Uses the supplied ``articles`` (up to 40 headlines) as primary context and
    live web search when needed, then returns
    ``{"text": str, "citations": [{"title", "url"}, ...]}``. Best-effort: any
    failure yields ``{"text": "(could not answer: <err>)", "citations": []}``.

    ``grounding`` (optional) is the top-story fact-check dict from
    :func:`ground_article`; its markdown is folded into the prompt for extra
    current context. ``history`` (optional) is the chat overlay's prior turns,
    folded in so follow-up questions are answered in context (see
    :func:`_history_block`). Successful answers are cached when a ``Cache`` is
    supplied; the cache key includes the conversation so follow-ups never
    collide with a different conversation's answer.
    """
    try:
        question = (question or "").strip()
        if not question:
            return {"text": "", "citations": []}

        model = config.CHAT_MODEL
        history_block = _history_block(history)
        key = make_key("ask", model, _hash([question, theme, history_block]))
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
            "You are a sharp research assistant answering questions about "
            "TODAY's AI news in an ongoing conversation. Use the provided "
            "headlines below as your primary context, draw on the conversation "
            "so far when the user asks a follow-up, and add live web search "
            "ONLY when needed to answer accurately or with current detail. Be "
            "concise, write in markdown, and cite your sources.\n\n"
            + theme_block
            + grounding_block
            + "Today's headlines:\n" + headlines_block + "\n\n"
            + history_block
            + f"Current question: {question}"
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


def ask_story(client, question, article, article_text="", grounding=None,
              cache=None, history=None):
    """Answer a free-form ``question`` about ONE specific story.

    Unlike :func:`ask_feed` (which reasons over the whole day's headlines), this
    is scoped to a single ``article``: its title / source / topic, the RSS
    summary, the scraped full-article ``article_text`` when the caller has it,
    and any grounded web context (only if this article IS the grounded top
    story). ``history`` (optional) is the chat overlay's prior turns, folded in
    so follow-ups stay in context (see :func:`_history_block`). Live web search
    fills gaps but the model is told to stay on THIS story. Returns
    ``{"text": str, "citations": [{"title", "url"}, ...]}``; best-effort,
    degrading to a safe error dict and caching successes (the cache key includes
    the conversation so follow-ups never collide).
    """
    try:
        question = (question or "").strip()
        if not question or article is None:
            return {"text": "", "citations": []}

        model = config.STORY_CHAT_MODEL
        title = getattr(article, "title", "") or ""
        link = getattr(article, "link", "") or ""
        history_block = _history_block(history)
        key = make_key("ask_story", model,
                       _hash([question, title, link, history_block]))
        if cache is not None:
            cached = cache.get(key, config.LLM_CACHE_TTL)
            if isinstance(cached, dict) and "text" in cached:
                return cached

        feed_name = getattr(article, "feed_name", "") or ""
        topic = getattr(article, "topic", "") or ""
        summary = (getattr(article, "summary", "") or "").strip()

        # Prefer the full scraped article; fall back to the RSS summary.
        body = (article_text or "").strip()
        if body:
            limit = getattr(config, "STORY_CHAT_MAX_ARTICLE_CHARS", 6000)
            if limit and len(body) > limit:
                body = body[:limit].rstrip() + " …"
            body_block = "Full article text:\n" + body + "\n\n"
        elif summary:
            body_block = "Article summary:\n" + summary + "\n\n"
        else:
            body_block = ""

        # Only fold in grounding when it actually describes THIS story.
        grounding_block = ""
        if isinstance(grounding, dict) and grounding.get("headline") == title:
            md = (grounding.get("markdown") or "").strip()
            if md:
                grounding_block = (
                    "Grounded web context on this story:\n" + md + "\n\n"
                )

        meta_block = (
            f"Headline: {title}\n"
            f"Source: {feed_name}\n"
            f"Topic: {topic}\n"
            f"Link: {link}\n\n"
        )

        prompt = (
            "You are a sharp research assistant answering questions about ONE "
            "specific AI news story in an ongoing conversation. Ground your "
            "answer in the story context below, draw on the conversation so far "
            "when the user asks a follow-up, and use live web search ONLY when "
            "needed for accuracy or current detail. Stay focused on THIS story "
            "— do not drift into unrelated news. Be concise, write in markdown, "
            "and cite your sources.\n\n"
            + meta_block
            + body_block
            + grounding_block
            + history_block
            + f"Current question: {question}"
        )

        result = client.web_search(
            prompt,
            model=model,
            effort=config.STORY_CHAT_EFFORT,
            verbosity=config.STORY_CHAT_VERBOSITY,
            max_output_tokens=config.STORY_CHAT_MAX_TOKENS,
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


# ── Socratic tutor (explain-back grading for the mastery layer) ──────────────
def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _socratic_schema():
    """Strict JSON schema for one Socratic turn (a probe or a graded reply)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["phase", "message", "score", "correct_points",
                     "misconceptions", "ideal_answer", "followup"],
        "properties": {
            "phase": {"type": "string", "enum": ["probe", "grade"]},
            "message": {"type": "string"},
            "score": {"type": "number"},
            "correct_points": {"type": "array", "items": {"type": "string"}},
            "misconceptions": {"type": "array", "items": {"type": "string"}},
            "ideal_answer": {"type": "string"},
            "followup": {"type": "string"},
        },
    }


def _socratic_fallback(phase):
    return {"phase": phase, "message": "(tutor unavailable — try again)",
            "score": 0.0, "correct_points": [], "misconceptions": [],
            "ideal_answer": "", "followup": ""}


def socratic_turn(client, concept, story_context="", user_answer="",
                  history=None, difficulty=None):
    """One turn of Socratic explain-back tutoring on a single ``concept``.

    With an empty ``user_answer`` this OPENS the session (``phase="probe"``):
    one focused question that makes the learner explain the concept at the
    adaptive ``difficulty`` band, grounded in ``story_context`` when relevant.
    With an answer it GRADES it (``phase="grade"``): a ``score`` in [0, 1] vs
    the concept's canonical definition, the points they got right, their
    misconceptions, a model ``ideal_answer``, and a deeper ``followup`` probe.

    ``difficulty`` is the ``(band, guidance)`` tuple from
    :meth:`ainews.mastery.MasteryStore.difficulty_for`. Best-effort: any failure
    returns a safe fallback dict so the TUI never crashes.
    """
    try:
        if not isinstance(concept, dict):
            return _socratic_fallback("probe")
        name = concept.get("name") or concept.get("id") or "this concept"
        definition = (concept.get("definition") or "").strip()
        band, guidance = (difficulty or ("intermediate", ""))

        ctx = ""
        if isinstance(story_context, str):
            ctx = story_context
        elif isinstance(story_context, dict):
            title = story_context.get("title", "") or ""
            body = (story_context.get("text") or story_context.get("summary") or "")
            ctx = f"{title}\n{body}".strip()
        ctx = ctx[:4000]

        hlines = []
        for t in (history or []):
            if not isinstance(t, dict):
                continue
            if t.get("probe"):
                hlines.append("TUTOR: " + str(t["probe"]))
            if t.get("answer"):
                hlines.append("LEARNER: " + str(t["answer"]))
            if t.get("feedback"):
                hlines.append("ASSESSMENT: " + str(t["feedback"]))
        hblock = ("Session so far:\n" + "\n".join(hlines) + "\n\n") if hlines else ""

        ua = (user_answer or "").strip()
        if ua:
            task = (
                "The learner just answered your probe. GRADE their explanation.\n"
                f"LEARNER'S ANSWER:\n{ua}\n\n"
                'Set phase="grade". score = your calibrated 0.0-1.0 judgment of '
                "correctness AND depth against the canonical definition and the "
                "difficulty band. correct_points = what they genuinely got right. "
                "misconceptions = specific errors or gaps (empty list if none). "
                "ideal_answer = a crisp model explanation at this band. message = "
                "warm but precise feedback, no fluff. followup = the NEXT probe "
                "that pushes exactly one step deeper (their zone of proximal "
                "development)."
            )
        else:
            task = (
                'Open the session. Set phase="probe". Ask ONE focused question '
                "that makes the learner EXPLAIN this concept at the difficulty "
                "band — grounded in the story when relevant. message = that "
                "question. score = 0. correct_points = []. misconceptions = []. "
                "ideal_answer = a private reference answer at this band. followup "
                "= a tentative deeper probe."
            )

        system = (
            "You are a sharp Socratic tutor helping a cognitive-architecture + ML "
            "engineer reach MASTER level. You probe and grade rather than lecture; "
            "you are precise, calibrated, and never sycophantic."
        )
        prompt = (
            f"CONCEPT: {name}\n"
            f"CANONICAL DEFINITION: {definition}\n"
            f"DIFFICULTY BAND: {band} — {guidance}\n\n"
            + (f"STORY CONTEXT:\n{ctx}\n\n" if ctx else "")
            + hblock
            + task
        )

        data = client.generate_json(
            prompt, _socratic_schema(), system=system,
            effort=config.SOCRATIC_EFFORT, verbosity=config.SOCRATIC_VERBOSITY,
            max_output_tokens=config.SOCRATIC_MAX_TOKENS,
            model=config.SOCRATIC_MODEL,
        )
        if not isinstance(data, dict) or not (data.get("message")):
            return _socratic_fallback("grade" if ua else "probe")

        # Coerce every field defensively — the model can violate the schema.
        phase = data.get("phase")
        if phase not in ("probe", "grade"):
            phase = "grade" if ua else "probe"
        cps = data.get("correct_points")
        cps = cps if isinstance(cps, list) else []
        mcs = data.get("misconceptions")
        mcs = mcs if isinstance(mcs, list) else []
        return {
            "phase": phase,
            "message": (data.get("message") or "").strip(),
            "score": _clamp01(data.get("score", 0.0)),
            "correct_points": [s for s in cps if isinstance(s, str) and s.strip()],
            "misconceptions": [s for s in mcs if isinstance(s, str) and s.strip()],
            "ideal_answer": (data.get("ideal_answer") or "").strip(),
            "followup": (data.get("followup") or "").strip(),
        }
    except Exception as exc:  # noqa: BLE001 - never crash the TUI
        phase = "grade" if (user_answer or "").strip() else "probe"
        return {"phase": phase, "message": f"(could not run tutor: {exc})",
                "score": 0.0, "correct_points": [], "misconceptions": [],
                "ideal_answer": "", "followup": ""}
