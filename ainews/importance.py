"""Pick THE single most important headline of the day (for the marquee).

Hybrid, research-grounded scoring that matches the user's intuition — the most
important story is the one most central to the day's theme — corrected for
centrality's "vanilla" failure mode with cross-source corroboration (the signal
real aggregators lean on because RSS carries no engagement metrics):

    score = 0.50 * theme_centrality   # embedding-centroid (or keyword) closeness
          + 0.30 * corroboration      # distinct outlets that ran the story
          + 0.12 * recency            # 24h half-life decay
          + 0.08 * source_authority   # small static prior

Then gpt-5.4 acts as a JUDGE over only the top-K shortlist (cheap "candidate
generation → LLM re-rank"), with position-bias mitigations (shuffled order +
majority vote) and a one-sentence justification for the ticker. With no API key
it falls back to the deterministic score + a templated reason — fully offline.
"""
import math
import random
import time
from collections import Counter
from urllib.parse import urlparse

from . import config
from .analysis import STOPWORDS, _WORD_RE
from .cache import make_key


def most_important(articles, theme="", client=None, cache=None):
    """Return ``(index, reason)`` for the day's single most important headline.

    Thin wrapper over :func:`most_important_ranked` — keeps the historical
    ``(index, reason)`` signature. ``index`` is into ``articles`` (-1 if empty).
    """
    ranked = most_important_ranked(articles, theme, client, cache)
    if ranked:
        return ranked[0]["index"], ranked[0]["reason"]
    return -1, ""


def most_important_ranked(articles, theme="", client=None, cache=None, k=None):
    """Return up to ``k`` ranked top stories as ``{index, reason, score}`` dicts.

    Rank order follows the hybrid deterministic score (theme-centrality,
    corroboration, recency, authority). When a ``client`` is available the LLM
    judge picks the rank-1 winner from the shortlist and supplies its reason;
    the remaining ranks fall back to templated reasons. Never raises — any
    failure degrades to the deterministic ordering. Returns ``[]`` only when
    there are no articles.
    """
    if k is None:
        k = getattr(config, "LEADERBOARD_SIZE", 5)
    try:
        if not articles:
            return []
        if len(articles) == 1:
            return [{"index": 0, "reason": _template_reason(articles[0]),
                     "score": 1.0}]

        now = time.time()
        w_t, w_c, w_r, w_a = getattr(
            config, "IMPORTANCE_WEIGHTS", (0.50, 0.30, 0.12, 0.08)
        )

        centrality = None
        if client is not None and getattr(config, "ENABLE_EMBEDDINGS", True):
            centrality = _centrality_by_embeddings(articles, client, cache)
        if centrality is None:
            centrality = _centrality_by_theme_text(articles, theme)

        outlets = [max(1, getattr(a, "cluster_size", 1)) for a in articles]
        max_o = max(outlets)
        corr = [math.log1p(o) / math.log1p(max_o) for o in outlets]
        rec = [_recency(a, now) for a in articles]
        auth = [_authority(a) for a in articles]

        scores = [
            w_t * centrality[i] + w_c * corr[i] + w_r * rec[i] + w_a * auth[i]
            for i in range(len(articles))
        ]
        order = sorted(range(len(articles)), key=lambda i: scores[i], reverse=True)

        # LLM judge over the shortlist only (bias-mitigated). Falls back to the
        # deterministic winner if the judge is unavailable or fails.
        winner_idx = None
        winner_reason = None
        if client is not None:
            top_k = getattr(config, "IMPORTANCE_TOP_K", 6)
            picked = _llm_judge(articles, theme, order[:top_k], client)
            if picked is not None:
                winner_idx, winner_reason = picked

        ranked = []
        if winner_idx is not None:
            ranked.append({
                "index": winner_idx,
                "reason": winner_reason or _template_reason(articles[winner_idx]),
                "score": scores[winner_idx],
            })
            for i in order:
                if len(ranked) >= k:
                    break
                if i == winner_idx:
                    continue
                ranked.append({
                    "index": i,
                    "reason": _template_reason(articles[i]),
                    "score": scores[i],
                })
        else:
            for i in order[:k]:
                ranked.append({
                    "index": i,
                    "reason": _template_reason(articles[i]),
                    "score": scores[i],
                })
        return ranked
    except Exception:  # noqa: BLE001 - importance is best-effort
        if articles:
            return [{"index": 0, "reason": _template_reason(articles[0]),
                     "score": 0.0}]
        return []


# --- Theme centrality -------------------------------------------------------
def _centrality_by_embeddings(articles, client, cache=None):
    """Cosine of each article to the embedding centroid; None on any failure.

    Embeddings are cached per ``Article.key`` so re-runs across the day only
    pay for the new arrivals: cached vectors are read first, only the misses are
    embedded in a single batch, and each fresh vector is written back.
    """
    try:
        oai = getattr(client, "_client", None)
        if oai is None:
            return None
        model = getattr(config, "EMBED_MODEL", "text-embedding-3-small")

        if cache is None:
            texts = [f"{a.title}. {a.summary}"[:2000] for a in articles]
            resp = oai.embeddings.create(model=model, input=texts)
            vecs = [d.embedding for d in resp.data]
            if not vecs or len(vecs) != len(articles):
                return None
            return _centroid_cosine(vecs)

        ttl = getattr(config, "LLM_CACHE_TTL", None)
        cached = [None] * len(articles)
        miss_idx = []
        miss_texts = []
        for i, a in enumerate(articles):
            ck = make_key("embed", model, a.key)
            hit = cache.get(ck, ttl)
            if isinstance(hit, list) and hit:
                cached[i] = hit
            else:
                miss_idx.append(i)
                miss_texts.append(f"{a.title}. {a.summary}"[:2000])

        if miss_texts:
            resp = oai.embeddings.create(model=model, input=miss_texts)
            data = list(resp.data)
            if len(data) != len(miss_idx):
                return None
            for pos, d in zip(miss_idx, data):
                vec = list(d.embedding)
                cached[pos] = vec
                cache.set(make_key("embed", model, articles[pos].key), vec)

        if any(v is None for v in cached):
            return None
        return _centroid_cosine(cached)
    except Exception:  # noqa: BLE001
        return None


def _centroid_cosine(vecs):
    """Min-maxed cosine of each (unit-norm) vector to the centroid."""
    if not vecs:
        return None
    dim = len(vecs[0])
    centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    cn = math.sqrt(sum(c * c for c in centroid)) or 1.0
    raw = [
        sum(v[i] * centroid[i] for i in range(dim)) / cn  # vecs are unit-norm
        for v in vecs
    ]
    return _minmax(raw)


def _keywords(text):
    return {
        w for w in _WORD_RE.findall((text or "").lower())
        if len(w) > 1 and w not in STOPWORDS
    }


def _centrality_by_theme_text(articles, theme):
    """Offline fallback: keyword overlap of each headline with the day's theme."""
    theme_kw = _keywords(theme)
    if not theme_kw:
        freq = Counter(w for a in articles for w in _keywords(a.title))
        theme_kw = {w for w, _ in freq.most_common(25)}
    raw = []
    for a in articles:
        kw = _keywords("{} {}".format(a.title, a.summary))
        raw.append(len(kw & theme_kw) / (len(kw) or 1))
    return _minmax(raw)


# --- LLM judge (shuffled, majority vote) ------------------------------------
def _llm_judge(articles, theme, shortlist_idx, client, votes=None):
    """Majority-vote, shuffled-order judge over the shortlist; None on failure."""
    if not shortlist_idx:
        return None
    votes = votes or getattr(config, "IMPORTANCE_VOTES", 3)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["choice", "reason"],
        "properties": {
            "choice": {"type": "integer"},
            "reason": {"type": "string"},
        },
    }
    system = (
        "You are a senior AI-news editor choosing the single MOST IMPORTANT "
        "story of the day for a scrolling ticker. Judge newsworthiness on: "
        "(1) impact/significance to AI, (2) how central it is to the day's "
        "theme, (3) breadth of coverage across outlets, (4) timeliness. Ignore "
        "the order items are listed in and ignore text length. Pick exactly one "
        "by its label."
    )
    tally = Counter()
    reasons = {}
    for _ in range(votes):
        order = list(shortlist_idx)
        random.shuffle(order)  # de-bias list position
        label_to_real = {n: real for n, real in enumerate(order)}
        lines = []
        for n, real in label_to_real.items():
            a = articles[real]
            outlets = max(1, getattr(a, "cluster_size", 1))
            lines.append(
                "[{}] {}  (source: {}; outlets: {})".format(
                    n, a.title, a.feed_name, outlets
                )
            )
        prompt = (
            "Day's theme: {}\n\nCandidates:\n{}\n\n"
            "Return the label of the single most important story and a one-"
            "sentence reason (<= 22 words) suitable for a news ticker.".format(
                theme or "(none)", "\n".join(lines)
            )
        )
        try:
            data = client.generate_json(
                prompt,
                schema,
                system=system,
                effort=getattr(config, "IMPORTANCE_EFFORT", "low"),
                verbosity=getattr(config, "IMPORTANCE_VERBOSITY", "low"),
                max_output_tokens=getattr(config, "IMPORTANCE_MAX_TOKENS", 400),
            )
            label = int(data.get("choice"))
            if label in label_to_real:
                real = label_to_real[label]
                tally[real] += 1
                reasons[real] = (data.get("reason") or "").strip()
        except Exception:  # noqa: BLE001
            continue

    if not tally:
        return None
    winner = tally.most_common(1)[0][0]
    reason = reasons.get(winner) or _template_reason(articles[winner])
    return winner, reason


# --- helpers ----------------------------------------------------------------
def _minmax(xs):
    lo, hi = min(xs), max(xs)
    if hi - lo < 1e-9:
        return [0.5] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def _recency(a, now):
    ts = getattr(a, "published_ts", None)
    if not ts:
        return 0.3
    age_h = max(0.0, (now - ts) / 3600.0)
    return min(1.0, 0.5 ** (age_h / 24.0))


def _authority(article):
    """Static authority prior keyed by the article link's DOMAIN (suffix-aware).

    Matches the netloc of ``article.link`` (sans a leading ``www.``) against
    ``config.SOURCE_AUTHORITY``; a key matches when it equals the domain or the
    domain is a sub-domain of it. Unlisted domains default to 0.8.
    """
    table = getattr(config, "SOURCE_AUTHORITY", {})
    domain = urlparse(getattr(article, "link", "") or "").netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    for key, value in table.items():
        if domain == key or domain.endswith("." + key):
            return value
    return 0.8


def _template_reason(a):
    n = max(1, getattr(a, "cluster_size", 1))
    if n > 1:
        return "Covered by {} outlets and central to today's theme.".format(n)
    return "Today's story most central to the overall theme."
