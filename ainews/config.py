"""Central configuration for the AI news feed.

Everything tunable lives in one place so the rest of the package stays
declarative. Environment-specific secrets are read lazily via ``get_api_key``.
"""
import os

# --- OpenAI -----------------------------------------------------------------
# Canonical key for this repo's utilities (see CLAUDE.md / project memory),
# with a graceful fallback to the SDK's default variable.
API_KEY_ENV = "OPENAI_API_KEY_UTILS"
API_KEY_FALLBACK_ENV = "OPENAI_API_KEY"


def get_api_key():
    """Return the OpenAI key, preferring the utils-specific variable."""
    return os.environ.get(API_KEY_ENV) or os.environ.get(API_KEY_FALLBACK_ENV)


# Primary model. ``gpt-5.4`` is a reasoning model whose effort levels are
# none | low | medium | high | xhigh ("none" is the fast tier on 5.1+).
MODEL = "gpt-5.4"

# Reasoning effort + verbosity per task. These are small, latency-sensitive
# calls, so we keep effort low. The LLM client degrades gracefully (dropping
# the parameter and retrying) if a particular value/name is rejected by the API.
THEME_EFFORT = "low"
THEME_VERBOSITY = "medium"
THEME_MAX_TOKENS = 700

ONE_WORD_EFFORT = "none"
ONE_WORD_VERBOSITY = "low"
ONE_WORD_MAX_TOKENS = 32

CLASSIFY_EFFORT = "none"
CLASSIFY_VERBOSITY = "low"
CLASSIFY_MAX_TOKENS = 4000

GROUNDING_EFFORT = "low"
GROUNDING_VERBOSITY = "medium"
GROUNDING_MAX_TOKENS = 900

# --- Feature flags ----------------------------------------------------------
ENABLE_THEME_SUMMARY = True
ENABLE_ONE_WORD = True
ENABLE_SENTIMENT = True       # color-code headlines by sentiment
ENABLE_CLUSTERING = True      # group headlines into topic clusters
ENABLE_GROUNDING = True       # live web_search context on the top story
ENABLE_MARQUEE = True         # scrolling marquee of the day's most important story
ENABLE_CHAT = True            # 'a' opens chat-with-your-feed (LLM Q&A over the day)

# "Most important" story selection (drives the marquee + grounding).
IMPORTANCE_EFFORT = "low"
IMPORTANCE_VERBOSITY = "low"
IMPORTANCE_MAX_TOKENS = 400
MARQUEE_TICK_MS = 150         # marquee scroll speed (ms per cell)
LEADERBOARD_SIZE = 5          # how many ranked stories the marquee + overview show

# Chat-with-your-feed (the 'a' overlay). A smaller, cheaper model with live
# web search and balanced reasoning.
CHAT_MODEL = "gpt-5.4-mini"
CHAT_EFFORT = "medium"
CHAT_VERBOSITY = "medium"
# Generous headroom: at medium reasoning effort, reasoning tokens share this
# budget, so too small a cap can leave zero visible answer.
CHAT_MAX_TOKENS = 4000

# Ask-about-this-story (the 'a' overlay INSIDE the story page). Same cheap model
# as feed chat, but scoped to a single article — its summary, the scraped
# full-article text when available, and any grounded context — with live web
# search to fill gaps. Gated by the same ENABLE_CHAT flag as feed chat.
STORY_CHAT_MODEL = "gpt-5.4-mini"
STORY_CHAT_EFFORT = "medium"
STORY_CHAT_VERBOSITY = "medium"
STORY_CHAT_MAX_TOKENS = 4000
# How much of a scraped full article to fold into the prompt (chars) so a long
# read can't blow the token budget; the tail is trimmed with an ellipsis.
STORY_CHAT_MAX_ARTICLE_CHARS = 6000

# Multi-turn chat memory (shared by BOTH the feed-wide and story-scoped 'a'
# overlays). Each overlay keeps a running, on-screen conversation and re-sends
# prior turns to the model so follow-ups understand context; 'x' clears it.
# These caps bound the re-sent context so a long chat can't blow the token
# budget — clearing is the primary lever, these are the safety net.
CHAT_HISTORY_MAX_TURNS = 10        # most-recent Q&A turns re-sent as context
CHAT_HISTORY_ANSWER_CHARS = 1200   # truncate each PRIOR answer in that context

# Hybrid importance score (theme-centrality, corroboration, recency, authority).
IMPORTANCE_WEIGHTS = (0.50, 0.30, 0.12, 0.08)
IMPORTANCE_TOP_K = 6          # shortlist size handed to the LLM judge
IMPORTANCE_VOTES = 3          # judge votes (shuffled order) -> majority pick
# Embedding-centroid theme-centrality. Off by default: this project's key has no
# access to the embeddings model (403), so importance falls back to keyword
# centrality. Flip on if your key gains embeddings access.
ENABLE_EMBEDDINGS = False
EMBED_MODEL = "text-embedding-3-small"
# Small static source-authority prior, keyed by DOMAIN (matched against the
# article link's netloc, suffix-aware) so it can't silently drift when a
# publisher tweaks its feed title. Default 0.8 for anything unlisted.
SOURCE_AUTHORITY = {
    "openai.com": 1.0,
    "deepmind.google": 1.0,
    "technologyreview.com": 1.0,
    "news.mit.edu": 1.0,
    "arstechnica.com": 0.95,
    "theverge.com": 0.95,
    "techcrunch.com": 0.95,
    "spectrum.ieee.org": 0.95,
    "wired.com": 0.95,
    "blog.google": 0.95,
    "huggingface.co": 0.9,
    "microsoft.com": 0.9,
    "importai.substack.com": 0.9,
    "simonwillison.net": 0.9,
}

# --- Fetch / filter ---------------------------------------------------------
LOOKBACK_HOURS = 96
FETCH_WORKERS = 12
FETCH_TIMEOUT = 25           # seconds per feed (best-effort socket timeout)
MAX_ENTRIES_PER_FEED = 30    # cap per source so arXiv/TechNode don't drown the feed
DEDUPE = True
DEDUPE_SIMILARITY = 0.92      # title-similarity threshold for de-duplication

# How many of the most-recent headlines to feed the LLM for theme / one-word /
# classification (keeps token cost predictable on busy news days).
MAX_HEADLINES_FOR_LLM = 80
# Classify (almost) everything so there's no giant "General" bucket. Done in
# batches to keep each structured call bounded and reliable.
MAX_ARTICLES_TO_CLASSIFY = 300
CLASSIFY_BATCH_SIZE = 45

# --- Cache ------------------------------------------------------------------
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "ai_news_feed")
FEED_CACHE_TTL = 30 * 60          # 30 minutes
LLM_CACHE_TTL = 6 * 60 * 60       # 6 hours
ENABLE_CACHE = True

# --- Persistence / export ---------------------------------------------------
# Where bookmarks + read-state live across runs, and where digests are written.
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
EXPORT_DIR = os.getcwd()

# --- In-app reader ----------------------------------------------------------
ENABLE_READER = True          # 'r' in the story page scrapes the full article
READER_TIMEOUT = 12           # seconds to fetch an article
READER_CACHE_TTL = 24 * 60 * 60
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "ai_news_feed/2.0 Safari/537.36"
)

# --- Mastery (deliberate-practice layer: Socratic tutor + knowledge graph) --
# Turns the feed into deliberate practice: stories are tagged to a concept
# knowledge-graph, the Socratic tutor grades your explanations, and per-concept
# "understanding" drives adaptive difficulty + spaced review.
ENABLE_MASTERY = True
MASTERY_FILE = os.path.join(CACHE_DIR, "mastery.json")

# Understanding is an EWMA of graded explanations in [0, 1].
MASTERY_EWMA_ALPHA = 0.4
MASTERY_THRESHOLD = 0.85       # understanding ≥ this (with enough attempts) = mastered
MASTERY_RELAPSE = 0.55         # a graded attempt below this lapses a mastered concept
MASTERY_MIN_ATTEMPTS = 2       # graded attempts before a concept can be "mastered"
MASTERY_MAX_MISCONCEPTIONS = 8 # cap stored misconceptions per concept

# Spaced review of concepts (SM-2-ish), in days.
MASTERY_FIRST_INTERVAL = 1.0
MASTERY_EASE_START = 2.3
MASTERY_EASE_MIN = 1.3
MASTERY_EASE_MAX = 2.8

# Socratic tutor LLM call (explain-back grading). A cheap, capable model.
SOCRATIC_MODEL = "gpt-5.4-mini"
SOCRATIC_EFFORT = "medium"
SOCRATIC_VERBOSITY = "medium"
SOCRATIC_MAX_TOKENS = 3000

# --- UI ---------------------------------------------------------------------
WRAP_LIMIT = 150
# Horizontal breathing room (columns) kept on each side of the home browser so
# the panels don't sit flush against the terminal edges. Dropped to 0 on very
# narrow terminals so the layout still fits.
UI_MARGIN = 2

# --- Feeds ------------------------------------------------------------------
# Curated to be PREDOMINANTLY about AI / machine learning. Every URL below was
# fetch-verified live + AI-specific; general whole-site / mixed-tech feeds
# (Engadget, Slashdot main, the Hacker News frontpage, ZDNet, TechNode, the
# Simon Willison "everything" feed) were dropped in favor of AI-tagged feeds,
# AI-filtered Hacker News searches, lab blogs, and ML newsletters.
AI_NEWS_FEEDS = [
    # AI news publications (AI-tagged sections)
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://arstechnica.com/ai/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://www.theregister.com/software/ai_ml/headlines.atom",
    "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss",
    "https://www.artificialintelligence-news.com/feed/",
    "https://syncedreview.com/feed/",
    "https://www.marktechpost.com/feed/",

    # Community, AI-filtered (Hacker News keyword searches)
    "https://hnrss.org/newest?q=AI",
    "https://hnrss.org/newest?q=LLM",

    # Research labs & big-tech AI blogs
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://blog.research.google/feeds/posts/default",
    "https://huggingface.co/blog/feed.xml",
    "https://mistral.ai/rss.xml",
    "https://www.together.ai/blog/rss.xml",
    "https://blogs.nvidia.com/blog/category/deep-learning/feed/",
    "https://aws.amazon.com/blogs/machine-learning/feed/",
    "https://www.microsoft.com/en-us/research/feed/",

    # Research institutions & academia
    "https://bair.berkeley.edu/blog/feed.xml",
    "https://news.mit.edu/topic/mitmachine-learning-rss.xml",
    "https://www.sciencedaily.com/rss/computers_math/artificial_intelligence.xml",
    "https://thegradient.pub/rss/",
    "http://export.arxiv.org/rss/cs.AI",
    "http://export.arxiv.org/rss/cs.LG",
    "http://export.arxiv.org/rss/cs.CL",

    # Independent analysts & newsletters
    "https://importai.substack.com/feed",
    "https://www.latent.space/feed",
    "https://sebastianraschka.substack.com/feed",
    "https://lastweekin.ai/feed",
    "https://www.interconnects.ai/feed",
    "https://aiweekly.co/feed",

    # Data science & ML practitioners
    "https://www.kdnuggets.com/feed",
    "https://towardsdatascience.com/feed",
    "https://machinelearningmastery.com/feed/",
    "https://www.analyticsvidhya.com/feed/",

    # Optional technical deep dives (uncomment to enable)
    # "http://export.arxiv.org/rss/cs.CV",     # computer vision (high volume)
    # "http://export.arxiv.org/rss/stat.ML",   # ML statistics (high volume)
    # "https://pytorch.org/feed/",
]
