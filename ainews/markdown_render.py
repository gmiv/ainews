"""Markdown -> color-tagged segment renderer.

Turns markdown into lists of ``(text, color_id)`` segments the wrapping/UI
layers render with curses color pairs. Color IDs come from :mod:`ainews.colors`
so the whole package shares one palette. Used for the GPT theme, the live-context
panel, and the in-app article reader (which scrapes articles as markdown).

Block syntax (line-oriented):
- ``#``…``######`` headings        -> :data:`colors.H1` / ``H2`` / ``H3``
- ``> `` blockquote                -> a ``▏`` rail + text in :data:`colors.QUOTE`
- ``-``/``*``/``+`` bullets        -> a ``•`` in :data:`colors.BULLET`
- ``1.`` ordered list              -> the number in :data:`colors.BULLET`
- ``---`` / ``***`` rule           -> a divider in :data:`colors.MDRULE`
- fenced ``` ``` ``` code blocks   -> body in :data:`colors.CODE`

Inline syntax (anywhere in a body):
- ``**bold**``                     -> :data:`colors.BOLD`
- ``*italic*``                     -> :data:`colors.ITALIC`
- `` `code` ``                     -> :data:`colors.CODE`
- ``[text](http…)``                -> ``text`` + ``↗`` in :data:`colors.LINK`
"""
import re

from . import colors

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*```")

# One pass over inline spans: code, links, bold, italic. Order matters so that
# ``code`` and links win over the looser emphasis patterns.
_INLINE = re.compile(
    r"`([^`]+)`"                                  # 1: `code`
    r"|\[([^\]]+)\]\((https?://[^)\s]+)\)"        # 2: [text](url)  3: url
    r"|\*\*([^*]+)\*\*"                           # 4: **bold**
    r"|\*([^*\n]+)\*"                             # 5: *italic*
)


def _inline(text, base_color):
    """Split a line body into segments, coloring inline spans."""
    out = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], base_color))
        if m.group(1) is not None:
            out.append((m.group(1), colors.CODE))
        elif m.group(2) is not None:
            out.append((m.group(2) + "↗", colors.LINK))
        elif m.group(4) is not None:
            out.append((m.group(4), colors.BOLD))
        elif m.group(5) is not None:
            out.append((m.group(5), colors.ITALIC))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], base_color))
    return out or [("", base_color)]


def parse_markdown_line(line):
    """Parse one markdown line into a list of ``(text, color_id)`` segments."""
    # Horizontal rule.
    if _RULE.match(line):
        return [("─" * 24, colors.MDRULE)]

    m = _HEADING.match(line)
    if m:
        level = len(m.group(1))
        color = colors.H1 if level == 1 else colors.H2 if level == 2 else colors.H3
        prefix = "§ " if level == 1 else "» " if level == 2 else "· "
        return [(prefix, color)] + _inline(m.group(2), color)

    m = _QUOTE.match(line)
    if m:
        return [("▏ ", colors.QUOTE)] + _inline(m.group(1), colors.QUOTE)

    m = _ORDERED.match(line)
    if m:
        indent = " " * len(m.group(1))
        return [(f"{indent}{m.group(2)}. ", colors.BULLET)] + _inline(m.group(3), colors.NORMAL)

    m = _BULLET.match(line)
    if m:
        indent = " " * len(m.group(1))
        return [(f"{indent}• ", colors.BULLET)] + _inline(m.group(2), colors.NORMAL)

    return _inline(line, colors.NORMAL)


def parse_markdown_text_to_segments(md_text):
    """Parse a multi-line markdown block into per-line segment lists.

    Tracks fenced code blocks (```), rendering their bodies verbatim in the code
    color. Blank lines become a single empty ``NORMAL`` segment so they still
    occupy a row when rendered.
    """
    all_rendered = []
    in_fence = False
    for line in (md_text or "").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            all_rendered.append([("", colors.NORMAL)])
            continue
        if in_fence:
            all_rendered.append([("  " + line, colors.CODE)])
        elif not line.strip():
            all_rendered.append([("", colors.NORMAL)])
        else:
            all_rendered.append(parse_markdown_line(line))
    return all_rendered
