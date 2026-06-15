"""Application orchestration for the AI news feed.

This module wires the package together: it fetches feeds, filters and
de-duplicates them, optionally enriches the result with GPT-5.4 (theme summary,
one-word mood, per-headline sentiment/topic clustering, and a live web-search
grounding of the top story), builds the renderable view model, word-wraps it,
and hands it to the curses UI.

Design goals:
  * Spinners give live feedback during every slow phase.
  * Feature flags (``config.ENABLE_*``) decide which enrichment steps run.
  * Graceful degradation is paramount — every LLM step is individually guarded
    so that one failure (or a missing API key) never aborts the others or the
    UI. The worst case is simply a less-enriched, still-usable feed.
"""
import curses
import locale

from . import config, feeds, analysis, enrich, importance, ui, mastery
from .cache import Cache
from .llm import LLMClient
from .loading import Spinner, note
from .persist import Store
from .state import FeedState


def run():
    """Fetch, enrich, and render the AI news feed.

    Each enrichment step is best-effort: failures are noted and skipped so the
    user always sees the raw feed even if the network or the LLM misbehaves.
    """
    cache = Cache()
    api_key = config.get_api_key()

    # --- Fetch ---------------------------------------------------------------
    with Spinner("Fetching feeds…") as sp:
        articles = feeds.fetch_all_feeds(
            cache=cache,
            progress=lambda done, total, name: sp.update(
                f"Fetching feeds… {done}/{total}"
            ),
        )

    # --- Filter / sort / dedupe ---------------------------------------------
    articles = feeds.filter_by_date(articles)
    articles.sort(key=lambda a: a.published_ts or 0, reverse=True)
    if config.DEDUPE:
        articles = feeds.dedupe(articles)

    if not articles:
        print("No recent articles found.")
        return

    # --- Cheap, local analysis (no network / no LLM) ------------------------
    source_counts = analysis.summarize_sources(articles)
    top_words = analysis.top_words_in_headlines(articles)

    # --- LLM enrichment (all optional, all individually guarded) ------------
    theme = ""
    one_word = ""
    grounding = None
    headline_of_day = None

    client = None
    if api_key:
        try:
            client = LLMClient()
        except Exception as e:  # noqa: BLE001 - degrade, never crash
            client = None
            note(f"LLM disabled: {e}")
    else:
        note(
            "No OPENAI_API_KEY_UTILS set — showing raw feed without GPT "
            "enrichment."
        )

    if client:
        headlines = [a.title for a in articles]

        if config.ENABLE_SENTIMENT or config.ENABLE_CLUSTERING:
            with Spinner("Classifying headlines…"):
                try:
                    enrich.classify_articles(client, articles, cache)
                except Exception as e:  # noqa: BLE001
                    note(f"Headline classification skipped: {e}")

        if config.ENABLE_THEME_SUMMARY:
            with Spinner("Summarizing theme…"):
                try:
                    theme = enrich.theme_summary(client, headlines, cache)
                except Exception as e:  # noqa: BLE001
                    note(f"Theme summary skipped: {e}")

        if config.ENABLE_ONE_WORD:
            with Spinner("Distilling one word…"):
                try:
                    one_word = enrich.one_word(client, headlines, cache)
                except Exception as e:  # noqa: BLE001
                    note(f"One-word summary skipped: {e}")

    # --- Rank the day's most important stories (drives marquee + grounding) --
    # Works even without an API key (deterministic theme-keyword + corroboration
    # score); the LLM, when present, re-ranks the shortlist and writes the reason
    # for the winner. The full ranking becomes the leaderboard.
    top_idx = 0
    leaderboard = []
    if config.ENABLE_MARQUEE or config.ENABLE_GROUNDING:
        with Spinner("Ranking top stories…"):
            try:
                ranked = importance.most_important_ranked(
                    articles, theme, client, cache, k=config.LEADERBOARD_SIZE
                )
            except Exception as e:  # noqa: BLE001
                ranked = []
                note(f"Top-story ranking skipped: {e}")
        for it in ranked:
            i = it.get("index", -1)
            if 0 <= i < len(articles):
                a = articles[i]
                leaderboard.append({
                    "rank": len(leaderboard) + 1,
                    "title": a.title,
                    "reason": it.get("reason", ""),
                    "link": a.link,
                    "topic": a.topic,
                })
        if ranked and 0 <= ranked[0].get("index", -1) < len(articles):
            top_idx = ranked[0]["index"]
        if leaderboard:
            headline_of_day = leaderboard[0]

    # --- Ground the MOST IMPORTANT story with a live web search --------------
    if client and config.ENABLE_GROUNDING:
        with Spinner("Grounding top story (web search)…"):
            try:
                grounding = enrich.ground_article(
                    client, articles[top_idx], cache
                )
            except Exception as e:  # noqa: BLE001
                note(f"Top-story grounding skipped: {e}")

    # --- Tag stories onto the concept knowledge-graph (deliberate practice) --
    # Cheap deterministic alias-matching links each story to concepts and marks
    # them "encountered"; the Socratic tutor + knowledge-graph view read this.
    mastery_store = None
    if getattr(config, "ENABLE_MASTERY", True):
        # Tagging is free + deterministic, so do it unconditionally — the
        # store (persistence) is the only part that can fail, so guard just it.
        seen = set()
        for a in articles:
            cids = mastery.tag_article(a)
            a.concepts = sorted(cids)
            seen |= cids
        try:
            mastery_store = mastery.MasteryStore()
            if seen:
                mastery_store.note_exposure(seen)
        except Exception as e:  # noqa: BLE001 - degrade, never crash
            mastery_store = None
            note(f"Mastery store disabled: {e}")

    # --- Build interactive state + render -----------------------------------
    # The FeedState owns filtering/search/bookmarks/read; the UI queries and
    # mutates it, re-wrapping on change/resize. Bookmarks + read-state persist
    # across runs via the Store.
    store = Store()
    state = FeedState(
        articles,
        source_counts,
        top_words,
        theme=theme,
        one_word=one_word,
        grounding=grounding,
        store=store,
        headline_of_day=headline_of_day,
        cache=cache,
        leaderboard=leaderboard,
        client=client,
        mastery=mastery_store,
    )
    curses.wrapper(ui.curses_main, state)


def main():
    """Entry point: run the app, swallowing a Ctrl-C as a clean exit."""
    # Honor the terminal's locale so emoji / box-drawing glyphs render on any
    # UTF-8 terminal. Best-effort: a misconfigured locale must never abort.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except Exception:  # noqa: BLE001
        pass
    try:
        run()
    except KeyboardInterrupt:
        pass
