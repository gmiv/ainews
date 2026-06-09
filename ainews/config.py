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

# --- UI ---------------------------------------------------------------------
WRAP_LIMIT = 150

# --- Feeds ------------------------------------------------------------------
AI_NEWS_FEEDS = [
    # Core Technology Publications
    "https://www.technologyreview.com/topic/artificial-intelligence/feed/",
    "https://techcrunch.com/tag/artificial-intelligence/feed/",
    "https://arstechnica.com/ai/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://www.wired.com/feed/tag/ai/latest/rss",
    "https://www.theregister.com/software/ai_ml/headlines.atom",
    "https://www.engadget.com/rss.xml",
    "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss",

    # General Tech & Community (broad coverage, AI surfaces often)
    "https://rss.slashdot.org/Slashdot/slashdotMain",
    "https://hnrss.org/frontpage",
    "https://www.zdnet.com/topic/artificial-intelligence/rss.xml",

    # Research Institutions & Corporate Blogs
    "https://deepmind.google/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://bair.berkeley.edu/blog/feed.xml",
    "https://openai.com/news/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    "https://blogs.nvidia.com/blog/category/deep-learning/feed/",
    "https://www.microsoft.com/en-us/research/feed/",

    # Industry Analysis & Market Trends
    "https://www.marktechpost.com/feed/",
    "https://www.kdnuggets.com/feed",
    "https://towardsdatascience.com/feed",
    "https://syncedreview.com/feed/",

    # Independent Analysts & Newsletters
    "https://simonwillison.net/atom/everything/",
    "https://thegradient.pub/rss/",
    "https://importai.substack.com/feed",
    "https://www.latent.space/feed",

    # Academic & Technical Resources
    "http://export.arxiv.org/rss/cs.AI",
    "http://export.arxiv.org/rss/cs.LG",
    "https://www.sciencedaily.com/rss/computers_math/artificial_intelligence.xml",
    "https://news.mit.edu/topic/mitmachine-learning-rss.xml",

    # Regional Coverage
    "https://technode.com/feed/",

    # Optional Technical Deep Dives
    # "https://blog.tensorflow.org/feed.xml",
    # "https://pytorch.org/feed/",
]
