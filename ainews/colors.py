"""Single source of truth for curses color-pair IDs.

Both the view-model (which tags text segments) and the UI (which initialises
the pairs and renders them) import these names so the two never drift.
``ui.init_colors()`` is responsible for actually calling ``curses.init_pair``
for each ID below.
"""

# Feed-line component colors
TITLE = 1
DATE = 2
SOURCE = 3

# Markdown rendering
NORMAL = 10
H1 = 11
H2 = 12
H3 = 13
BULLET = 14
BOLD = 15
ACCENT = 16        # one-word theme / highlights

# Sentiment (applied to headline titles)
SENT_HYPE = 20
SENT_CONCERN = 21
SENT_NEUTRAL = 22

# Structure
TOPIC = 23         # topic-cluster section headers
LINK = 24          # citations / links
BANNER = 25        # top banner

# Markdown inline spans (reader + theme + live-context)
CODE = 28          # `inline code` / fenced blocks
QUOTE = 29         # > blockquotes
ITALIC = 30        # *italic*
MDRULE = 31        # --- horizontal rules

SENTIMENT = {
    "hype": SENT_HYPE,
    "concern": SENT_CONCERN,
    "neutral": SENT_NEUTRAL,
}


def sentiment_color(sentiment: str) -> int:
    """Map a sentiment label to its color-pair ID (neutral if unknown)."""
    return SENTIMENT.get(sentiment, SENT_NEUTRAL)


# --- Dynamic topic palette --------------------------------------------------
# Each topic cluster gets its own color (the "color axis" the feed is organized
# by). The view-model maps each topic to ``topic_color_id(index)``; the UI's
# ``init_colors`` installs the matching pairs. The count is FIXED so the two can
# never disagree — topics beyond it simply reuse colors.
TOPIC_PAIR_BASE = 40
TOPIC_COLOR_COUNT = 12

# Vivid, well-separated hues for 256-color terminals.
TOPIC_PALETTE_256 = [39, 208, 46, 201, 51, 226, 141, 118, 214, 45, 213, 82]
# 8-color fallback (ANSI fg numbers; black omitted), cycled to fill the count.
TOPIC_PALETTE_8 = [6, 2, 3, 5, 4, 1, 7]  # cyan green yellow magenta blue red white

# Marquee / hero accents.
MARQUEE = 26
MARQUEE_LABEL = 27


def topic_color_id(index: int) -> int:
    """Color-pair ID for the topic at ordered position ``index`` (wraps)."""
    return TOPIC_PAIR_BASE + (index % TOPIC_COLOR_COUNT)
