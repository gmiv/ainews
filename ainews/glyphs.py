"""Geometry + emoji glyph kit — the visual vocabulary of the TUI.

All presentation glyphs live here so the look can be retuned in one place. Glyphs
are chosen so that the alignment-critical ones (gutters, bars, dots, box edges)
are single display-width; emoji (double-width) are used where the width-aware
renderer can account for them. See :mod:`ainews.textwidth`.
"""

# ── Box drawing ────────────────────────────────────────────────────────────
# style -> dict of corner/edge glyphs, consumed by the UI's rule/box renderer.
BOX = {
    "round": {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
              "h": "─", "v": "│", "tl_tee": "├", "tr_tee": "┤"},
    "heavy": {"tl": "┏", "tr": "┓", "bl": "┗", "br": "┛",
              "h": "━", "v": "┃", "tl_tee": "┣", "tr_tee": "┫"},
    "light": {"tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
              "h": "─", "v": "│", "tl_tee": "├", "tr_tee": "┤"},
}
H_LIGHT = "─"
H_HEAVY = "━"

# ── Wedges / diamonds / accents (single-width) ─────────────────────────────
WEDGE_TL, WEDGE_TR, WEDGE_BL, WEDGE_BR = "◤", "◥", "◣", "◢"
DIAMOND, DIAMOND_O = "◆", "◇"
NODE = "◈"
POINTER = "▸"          # selection / fold pointer
POINTER_OPEN = "▾"
ARROW = "➜"
ARROW_THIN = "⟶"

# Topic chip rails (frame a short label run).
CHIP_L, CHIP_R = "⟦", "⟧"

# ── Blocks & bars ──────────────────────────────────────────────────────────
GUTTER = "▌"                       # left colored gutter on every feed row
BAR_FULL = "█"
BAR_EIGHTHS = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
SHADE = ["░", "▒", "▓", "█"]
SPARK = "▁▂▃▄▅▆▇█"                 # vertical sparkline ramp
BRAILLE_RAMP = "⣀⣄⣆⣇⣧⣷⣿"          # density ramp (sub-pixel)

# ── Sentiment indicators ───────────────────────────────────────────────────
# Single-width geometric dots (colored by the caller) — alignment-safe.
SENTIMENT_GLYPH = {"hype": "▲", "concern": "▼", "neutral": "●"}
SENTIMENT_GLYPH_READ = {"hype": "△", "concern": "▽", "neutral": "○"}
# Double-width emoji variant (used in panels / where width is accounted for).
SENTIMENT_EMOJI = {"hype": "🟢", "concern": "🔴", "neutral": "⚪"}


def sentiment_glyph(sentiment: str, read: bool = False) -> str:
    table = SENTIMENT_GLYPH_READ if read else SENTIMENT_GLYPH
    return table.get(sentiment, table["neutral"])


def sentiment_emoji(sentiment: str) -> str:
    return SENTIMENT_EMOJI.get(sentiment, SENTIMENT_EMOJI["neutral"])


# ── Topic emoji ────────────────────────────────────────────────────────────
TOPIC_EMOJI = {
    "New Models": "🚀",
    "Research": "🧠",
    "Funding & Business": "💰",
    "Regulation & Policy": "⚖️",
    "Safety & Ethics": "🛡️",
    "Hardware & Chips": "🔧",
    "Tools & Products": "🛠️",
    "Industry Moves": "🏢",
    "Open Source": "🔓",
    "Datasets & Benchmarks": "📊",
    "Robotics": "🤖",
    "Healthcare": "🩺",
    "Creative & Media": "🎨",
    "General": "📌",
}
DEFAULT_TOPIC_EMOJI = "🔹"


def topic_emoji(topic: str) -> str:
    return TOPIC_EMOJI.get(topic, DEFAULT_TOPIC_EMOJI)


# ── Section emoji (panel titles) ───────────────────────────────────────────
SEC_TITLE = "⚡"
SEC_MIX = "📊"
SEC_THEME = "🌐"
SEC_WORD = "✨"
SEC_CONTEXT = "🔎"
SEC_HEADLINES = "📰"
SEC_SOURCES = "🔗"
SEC_SRC = "📡"
SEC_WORDS = "🔤"


# ── Circled rank badges ①②③ … (bounded use; ≤ ~50) ────────────────────────
def rank_badge(n: int) -> str:
    """Return a circled number for 1..50, else the plain number string."""
    if 1 <= n <= 20:
        return chr(0x2460 + n - 1)      # ①..⑳
    if 21 <= n <= 35:
        return chr(0x3251 + n - 21)     # ㉑..㉟
    if 36 <= n <= 50:
        return chr(0x32B1 + n - 36)     # ㊱..㊿
    return str(n)


# ── Fractional block bar ───────────────────────────────────────────────────
def bar(value, maxv, cells: int) -> str:
    """A smooth block bar (using eighth-blocks) of up to ``cells`` wide.

    Width is proportional to ``value/maxv``; a tiny non-zero value still shows a
    sliver so no active topic looks empty.
    """
    if cells <= 0:
        return ""
    if not maxv or maxv <= 0:
        return ""
    frac = max(0.0, min(1.0, float(value) / float(maxv)))
    eighths = int(round(frac * cells * 8))
    full, rem = divmod(eighths, 8)
    s = BAR_FULL * full
    if rem:
        s += BAR_EIGHTHS[rem]
    if not s and value:
        s = BAR_EIGHTHS[1]
    return s


def sparkline(values, ramp=SPARK) -> str:
    """Render a sequence of numbers as a one-line sparkline."""
    values = [v for v in (values or []) if isinstance(v, (int, float))]
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    n = len(ramp) - 1
    return "".join(ramp[int(round((v - lo) / span * n))] for v in values)
