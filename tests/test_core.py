"""Network- and API-key-free tests over the deterministic core.

These lock in the behaviors the whole app's "never crashes, always degrades"
design leans on: de-dup corroboration math, date filtering, offline importance
ranking, domain authority, display-width accounting, and the markdown parser.
Run with ``pytest`` from the repo root.
"""
import json
import time

from ainews.models import Article
from ainews import (
    feeds, importance, markdown_render, textwidth, glyphs, analysis, colors,
    enrich, config, mastery,
)
from ainews import concepts_seed as _cs
from ainews.cache import Cache


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


# ── ask-about-this-story (the story-page 'a' overlay) ────────────────────────
class _FakeClient:
    """A ``web_search`` stub: records the prompt + kwargs, returns a cited answer."""
    model = "fake-model"

    def __init__(self):
        self.calls = 0
        self.last_prompt = None
        self.last_kwargs = None

    def web_search(self, prompt, **kwargs):
        self.calls += 1
        self.last_prompt = prompt
        self.last_kwargs = kwargs
        return {"text": "A focused answer.",
                "citations": [{"title": "Src", "url": "http://src/x"}]}


def test_ask_story_scopes_to_the_single_article():
    client = _FakeClient()
    art = _art("Acme ships a new chip", feed="Ars", topic="Hardware & Chips")
    art.summary = "Acme's chip doubles throughput."
    out = enrich.ask_story(client, "How fast is it?", art)
    assert out["text"] == "A focused answer."
    assert out["citations"][0]["url"] == "http://src/x"
    # The prompt carries THIS story's context and the question...
    assert "Acme ships a new chip" in client.last_prompt
    assert "Acme's chip doubles throughput." in client.last_prompt
    assert "How fast is it?" in client.last_prompt
    # ...and uses the scoped story model.
    assert client.last_kwargs.get("model") == config.STORY_CHAT_MODEL


def test_ask_story_prefers_full_text_and_truncates():
    client = _FakeClient()
    art = _art("Long read")
    art.summary = "short summary"
    body = "X" * (config.STORY_CHAT_MAX_ARTICLE_CHARS + 500)
    enrich.ask_story(client, "q?", art, article_text=body)
    assert "Full article text:" in client.last_prompt
    # The RSS summary is dropped in favor of the full text...
    assert "short summary" not in client.last_prompt
    # ...and the body is truncated to the cap (the only X's in the prompt).
    assert client.last_prompt.count("X") == config.STORY_CHAT_MAX_ARTICLE_CHARS


def test_ask_story_includes_grounding_only_for_matching_headline():
    client = _FakeClient()
    art = _art("Top story")
    g = {"headline": "Top story", "markdown": "Grounded note.", "citations": []}
    enrich.ask_story(client, "q?", art, grounding=g)
    assert "Grounded note." in client.last_prompt
    # Grounding for a DIFFERENT headline must not leak into this story's ask.
    other = _FakeClient()
    g2 = {"headline": "Other story", "markdown": "Other note.", "citations": []}
    enrich.ask_story(other, "q?", art, grounding=g2)
    assert "Other note." not in other.last_prompt


def test_ask_story_guards_blank_question_and_missing_article():
    client = _FakeClient()
    assert enrich.ask_story(client, "   ", _art("x")) == {"text": "", "citations": []}
    assert enrich.ask_story(client, "q?", None) == {"text": "", "citations": []}
    assert client.calls == 0  # neither degenerate case bills the model


def test_ask_story_caches_by_question_and_link(tmp_path):
    cache = Cache(directory=str(tmp_path), enabled=True)
    client = _FakeClient()
    art = _art("Cached", link="http://cached/1")
    first = enrich.ask_story(client, "same?", art, cache=cache)
    second = enrich.ask_story(client, "same?", art, cache=cache)
    assert first == second
    assert client.calls == 1                 # the repeat answer came from cache
    enrich.ask_story(client, "different?", art, cache=cache)
    assert client.calls == 2                 # a new question is a fresh call


# ── multi-turn conversation memory (the chat overlay's history) ──────────────
def test_history_block_caps_turns_and_truncates():
    # More turns than the cap -> only the most-recent are re-sent.
    turns = [{"question": f"q{i}", "answer": {"text": f"a{i}"}}
             for i in range(config.CHAT_HISTORY_MAX_TURNS + 5)]
    block = enrich._history_block(turns)
    assert block.startswith("Conversation so far")
    assert "q0" not in block and "a0" not in block            # oldest dropped
    assert f"q{config.CHAT_HISTORY_MAX_TURNS + 4}" in block    # newest kept
    # A long prior answer is truncated to the char cap (+ ellipsis marker).
    long = "Z" * (config.CHAT_HISTORY_ANSWER_CHARS + 300)
    block2 = enrich._history_block([{"question": "q", "answer": {"text": long}}])
    assert block2.count("Z") == config.CHAT_HISTORY_ANSWER_CHARS
    assert " …" in block2
    # No history -> no block at all (callers fold it in unconditionally).
    assert enrich._history_block([]) == ""
    assert enrich._history_block(None) == ""


def test_ask_story_threads_history_into_prompt():
    client = _FakeClient()
    art = _art("Story X")
    history = [{"question": "What is it?",
                "answer": {"text": "It is a chip.", "citations": []}}]
    enrich.ask_story(client, "How fast?", art, history=history)
    assert "Conversation so far" in client.last_prompt
    assert "What is it?" in client.last_prompt
    assert "It is a chip." in client.last_prompt
    assert "Current question: How fast?" in client.last_prompt
    # A bare (history-less) ask carries no conversation block.
    bare = _FakeClient()
    enrich.ask_story(bare, "How fast?", art)
    assert "Conversation so far" not in bare.last_prompt


def test_ask_feed_threads_history_and_keys_on_it(tmp_path):
    cache = Cache(directory=str(tmp_path), enabled=True)
    client = _FakeClient()
    arts = [_art("Headline A"), _art("Headline B")]
    h1 = [{"question": "first?", "answer": {"text": "ans one"}}]
    enrich.ask_feed(client, "follow up?", arts, history=h1, cache=cache)
    assert "Conversation so far" in client.last_prompt
    assert "ans one" in client.last_prompt
    assert "Headline A" in client.last_prompt          # headlines still primary
    assert "Current question: follow up?" in client.last_prompt
    assert client.calls == 1
    # Same question, DIFFERENT history -> different cache key -> a fresh call.
    h2 = [{"question": "first?", "answer": {"text": "ans two"}}]
    enrich.ask_feed(client, "follow up?", arts, history=h2, cache=cache)
    assert client.calls == 2
    # Re-asking with the SAME history hits the cache (no new call).
    enrich.ask_feed(client, "follow up?", arts, history=h2, cache=cache)
    assert client.calls == 2


def test_history_block_handles_malformed_entries():
    # Non-dict entries are skipped; None/missing answers and a None text value
    # must not crash (the app's "never raise into the prompt" contract).
    turns = [
        "not a dict",
        {"question": "ok", "answer": {"text": None}},        # None text value
        {"question": "q2"},                                  # missing answer
        {"question": "q3", "answer": None},                  # None answer
        {"question": "q4", "answer": {"text": "real answer"}},
    ]
    block = enrich._history_block(turns)
    assert "User: ok" in block
    assert "real answer" in block
    assert "None" not in block            # a None text/answer never leaks as text


def test_transcript_rows_placeholder_and_turns():
    from ainews import ui
    # Empty -> a single scope-aware placeholder, no turn starts.
    rows, starts = ui._transcript_rows([], 60, scope="the feed")
    assert starts == []
    assert len(rows) == 1
    assert "the feed" in "".join(t for t, _ in rows[0])
    # Two turns -> starts marks where each question begins (ascending, in range).
    turns = [
        {"question": "first?", "answer": {"text": "one", "citations": []}},
        {"question": "second?", "answer": {"text": "two", "citations": [
            {"title": "T", "url": "http://u"}]}},
    ]
    rows, starts = ui._transcript_rows(turns, 60)
    assert len(starts) == 2
    assert 0 <= starts[0] < starts[1] < len(rows)
    assert "first?" in "".join(t for t, _ in rows[starts[0]])
    assert "second?" in "".join(t for t, _ in rows[starts[1]])


def test_ask_feed_answers_with_no_local_articles():
    # Unlike ask_story (which needs an article), ask_feed still answers via web
    # search when there are no local headlines — it must not short-circuit.
    client = _FakeClient()
    out = enrich.ask_feed(client, "what happened today?", [])
    assert out["text"] == "A focused answer."
    assert client.calls == 1
    none_client = _FakeClient()
    enrich.ask_feed(none_client, "what happened today?", None)
    assert none_client.calls == 1


# ── mastery layer: concept graph + Socratic tutor ───────────────────────────
def test_taxonomy_integrity():
    ids = [c["id"] for c in _cs.CONCEPTS]
    assert len(ids) == len(set(ids))                     # unique ids
    idset = set(ids)
    cats = set(_cs.CATEGORIES)
    for c in _cs.CONCEPTS:
        assert c["category"] in cats                     # valid category
        assert c["definition"].strip()                   # gradable definition
        for e in c["prereqs"] + c["related"]:
            assert e in idset                            # no dangling edges
            assert e != c["id"]                          # no self-edges


def test_tag_article_matches_concepts():
    class A:
        title = "A new Transformer trained with RLHF"
        summary = "It relies on self-attention throughout."
    hits = mastery.tag_article(A())
    assert {"transformer-architecture", "rlhf"} <= hits
    assert "" not in hits


def test_mastery_store_progression_and_persist(tmp_path):
    p = str(tmp_path / "mastery.json")
    ms = mastery.MasteryStore(path=p)
    cid = _cs.CONCEPTS[0]["id"]
    ms.note_exposure([cid])
    assert ms.concept_view(cid)["status"] == "encountered"
    ms.record_attempt(cid, 0.9)
    ms.record_attempt(cid, 0.95)
    v = ms.concept_view(cid)
    assert v["attempts"] == 2 and v["status"] == "mastered" and v["understanding"] >= 0.85
    assert ms.difficulty_for(cid)[0] == "frontier"       # mastery raises the band
    # persistence round-trips across store instances
    ms2 = mastery.MasteryStore(path=p)
    assert ms2.concept_view(cid)["attempts"] == 2
    assert ms2.concept_view(cid)["status"] == "mastered"


def test_mastery_low_score_reviews_soon_and_logs_misconception(tmp_path):
    ms = mastery.MasteryStore(path=str(tmp_path / "m.json"))
    cid = _cs.CONCEPTS[1]["id"]
    st = ms.record_attempt(cid, 0.2, ["got the mechanism backwards"], ts=1000.0)
    assert st["status"] == "reviewing"
    assert st["interval_days"] == 0.0                    # failed -> review immediately
    assert st["due"] <= 1000.0
    assert "got the mechanism backwards" in st["misconceptions"]
    assert cid in ms.due_concepts(now=1001.0)


def test_difficulty_bands_track_understanding(tmp_path):
    low = mastery.MasteryStore(path=str(tmp_path / "lo.json"))
    cid = _cs.CONCEPTS[2]["id"]
    low.record_attempt(cid, 0.1, ts=1.0)
    assert low.difficulty_for(cid)[0] == "foundations"
    hi = mastery.MasteryStore(path=str(tmp_path / "hi.json"))
    hi.record_attempt(cid, 0.7, ts=1.0)
    assert hi.difficulty_for(cid)[0] == "advanced"


class _FakeJSONClient:
    """Stub exposing generate_json for socratic_turn tests."""
    model = "fake"

    def __init__(self, payload):
        self.payload = payload
        self.last_prompt = None

    def generate_json(self, prompt, schema, **kwargs):
        self.last_prompt = prompt
        return dict(self.payload)


def test_socratic_turn_probe_then_grade():
    concept = {"id": "mixture-of-experts", "name": "Mixture of Experts",
               "definition": "Sparse expert routing."}
    probe = {"phase": "probe", "message": "Explain MoE routing.", "score": 0.0,
             "correct_points": [], "misconceptions": [], "ideal_answer": "x",
             "followup": "y"}
    out = enrich.socratic_turn(_FakeJSONClient(probe), concept,
                               {"title": "t", "summary": "s"}, "", [],
                               ("intermediate", "guide"))
    assert out["phase"] == "probe" and out["message"] == "Explain MoE routing."
    assert out["score"] == 0.0

    grade = {"phase": "grade", "message": "Close.", "score": 0.7,
             "correct_points": ["right idea"], "misconceptions": ["missed the gate"],
             "ideal_answer": "...", "followup": "Now explain load balancing."}
    gc = _FakeJSONClient(grade)
    out2 = enrich.socratic_turn(gc, concept, "ctx",
                                "MoE routes tokens to a few experts", [],
                                ("advanced", "guide"))
    assert out2["score"] == 0.7 and "missed the gate" in out2["misconceptions"]
    assert "MoE routes tokens to a few experts" in gc.last_prompt   # answer folded in


def test_socratic_turn_degrades_on_bad_client():
    class Boom:
        model = "x"

        def generate_json(self, *a, **k):
            raise RuntimeError("nope")

    out = enrich.socratic_turn(Boom(), {"name": "X", "definition": "d"}, "",
                               "ans", [], None)
    assert out["score"] == 0.0
    assert out["message"].startswith("(could not run tutor")


# ── mastery hardening (from the adversarial review) ─────────────────────────
def test_mastery_relapse_from_mastered(tmp_path):
    ms = mastery.MasteryStore(path=str(tmp_path / "m.json"))
    cid = _cs.CONCEPTS[0]["id"]
    ms.record_attempt(cid, 0.95)
    ms.record_attempt(cid, 0.95)
    assert ms.concept_view(cid)["status"] == "mastered"
    # sustained low scores pull the EWMA below threshold -> demote, not sticky
    ms.record_attempt(cid, 0.2)
    ms.record_attempt(cid, 0.2)
    v = ms.concept_view(cid)
    assert v["understanding"] < config.MASTERY_THRESHOLD
    assert v["status"] == "reviewing"


def test_due_concepts_ordered_weakest_first(tmp_path):
    ms = mastery.MasteryStore(path=str(tmp_path / "m.json"))
    a, b, c = (_cs.CONCEPTS[0]["id"], _cs.CONCEPTS[1]["id"], _cs.CONCEPTS[2]["id"])
    ms.record_attempt(a, 0.8, ts=1000.0)
    ms.record_attempt(b, 0.2, ts=1000.0)
    ms.record_attempt(c, 0.5, ts=1000.0)
    due = ms.due_concepts(now=10 ** 9)
    assert due.index(b) < due.index(c) < due.index(a)   # weakest first


def test_coverage_empty_store(tmp_path):
    ms = mastery.MasteryStore(path=str(tmp_path / "empty.json"))
    cov = ms.coverage()
    assert cov["total"] == len(_cs.CONCEPTS)
    assert cov["encountered"] == 0 and cov["mastered"] == 0 and cov["due"] == 0
    assert cov["level"] == 0.0
    for b in cov["categories"].values():
        assert b["mean_u"] == 0.0                       # no divide-by-zero


def test_mastery_store_survives_corrupt_json(tmp_path):
    cid = _cs.CONCEPTS[0]["id"]
    p = tmp_path / "m.json"
    # null / wrong-typed fields must not crash any read or write path
    p.write_text(json.dumps({"concepts": {cid: {
        "understanding": None, "attempts": None, "exposures": None,
        "ease": None, "interval_days": None, "due": "soon",
        "status": "bogus", "misconceptions": "oops"}}, "level": None}))
    ms = mastery.MasteryStore(path=str(p))
    assert ms.concept_view(cid)["attempts"] == 0        # sanitized
    ms.note_exposure([cid])
    ms.record_attempt(cid, 0.7)
    ms.coverage(); ms.due_concepts(); ms.difficulty_for(cid)
    assert ms.concept_view(cid)["attempts"] == 1
    # a non-dict JSON root is tolerated too
    p2 = tmp_path / "arr.json"
    p2.write_text("[1, 2, 3]")
    assert mastery.MasteryStore(path=str(p2)).coverage()["encountered"] == 0


def test_alias_pattern_case_and_separators():
    # uppercase single-token acronym -> case-SENSITIVE (no false 'clip' verb)
    clip = mastery._alias_pattern("CLIP")
    assert clip.search("we use CLIP embeddings")
    assert not clip.search("we clip the gradients")
    # lowercase token -> case-insensitive (recall on capitalized prose)
    tr = mastery._alias_pattern("transformer")
    assert tr.search("a Transformer model") and tr.search("transformer")
    # hyphen / space / underscore are interchangeable separators
    ft = mastery._alias_pattern("fine-tuning")
    assert ft.search("fine-tuning") and ft.search("fine tuning") and ft.search("fine_tuning")
    # multiword phrase -> case-insensitive, separator-tolerant
    moe = mastery._alias_pattern("Mixture of Experts")
    assert moe.search("mixture of experts") and moe.search("Mixture-of-Experts")
    # word boundaries: no partial-word matches
    assert not mastery._alias_pattern("rag").search("storage")


def test_socratic_turn_coerces_malformed_fields():
    bad = {"phase": "weird", "message": "ok", "score": 2.0,
           "correct_points": "not a list", "misconceptions": {"a": 1},
           "ideal_answer": "x", "followup": "y"}
    out = enrich.socratic_turn(_FakeJSONClient(bad),
                               {"name": "X", "definition": "d"}, "",
                               "an answer", [], None)
    assert out["phase"] == "grade"            # invalid enum -> derived from answer
    assert out["score"] == 1.0                # clamped to [0,1]
    assert out["correct_points"] == [] and out["misconceptions"] == []
