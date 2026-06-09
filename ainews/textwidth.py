"""Display-width helpers.

Terminals render many emoji and CJK glyphs as TWO cells while Python's ``len``
counts them as one. Mixing those into curses output (which advances the cursor
by visible cells) desynchronizes every column to its right. Every module that
positions text — the renderer, the box padder, the word-wrapper — measures with
``width()`` instead of ``len()`` so emoji and geometry stay aligned.
"""
import unicodedata

# Codepoint ranges that terminals almost always render double-width even though
# unicodedata may not flag them 'W'/'F' (emoji, symbols, dingbats, flags…).
_WIDE_RANGES = (
    (0x1100, 0x115F),    # Hangul Jamo
    (0x2300, 0x23FF),    # Misc technical (⏏ ⌛ …)
    (0x2600, 0x27BF),    # Misc symbols + dingbats (☀ ★ ➜ …)
    (0x2B00, 0x2BFF),    # Misc symbols & arrows (⬅ ⭐ …)
    (0x1F000, 0x1FAFF),  # Emoji planes (🚀 🟢 🌍 …)
    (0x3000, 0x303E),    # CJK punctuation
    (0x3041, 0x33FF),    # Kana / circled-CJK (㉑ ㊀ …)
    (0xFF00, 0xFF60),    # Fullwidth forms
)


def char_width(ch: str) -> int:
    """Return the number of terminal cells ``ch`` occupies (0, 1, or 2)."""
    if not ch:
        return 0
    # Zero-width: combining marks, ZWJ, variation selectors.
    if ch in ("‍", "️", "︎") or unicodedata.combining(ch):
        return 0
    o = ord(ch)
    for lo, hi in _WIDE_RANGES:
        if lo <= o <= hi:
            return 2
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def width(s: str) -> int:
    """Total display width of ``s`` in terminal cells."""
    return sum(char_width(c) for c in (s or ""))


def clip(s: str, max_cells: int) -> str:
    """Truncate ``s`` so its display width does not exceed ``max_cells``."""
    if max_cells <= 0:
        return ""
    out = []
    used = 0
    for ch in s or "":
        w = char_width(ch)
        if used + w > max_cells:
            break
        out.append(ch)
        used += w
    return "".join(out)
