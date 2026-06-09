"""Curses rendering and the interactive two-pane browser (v4 layout).

The screen is a stacked composite, top to bottom:

  • a scrolling MARQUEE of the day's most important story,
  • the ⚡ BANNER / summarizer (story + topic counts, one-word mood, sentiment mix),
  • side-by-side 🌐 THEME and 🔎 LIVE CONTEXT panels,
  • the two-pane BROWSER — TOPICS on the left, that topic's STORIES on the right,
  • a persistent status/help bar.

Navigation is master/detail: ←/→ move focus between the topics and stories panes
(the focused pane has a highlighted border), ↑/↓ move within it, and Space/Enter
on a story opens the framed-card STORY PAGE (from which ``o``/Enter opens the
article in the browser). The header region degrades gracefully on short
terminals (panels drop in order) so the browser always has room.

Everything is drawn through one ``_draw_box`` primitive and ``textwidth`` for all
column math, so geometry, color, and double-width emoji stay aligned. The loop
is animated: ``getch`` is non-blocking (``MARQUEE_TICK_MS``) so the marquee
scrolls one cell per idle tick.
"""
import curses
import time

from . import colors, config, glyphs, textwidth, markdown_render, openurl, reader, enrich
from .wrapping import word_wrap_line_segments

# Minimum height (rows) the two-pane browser must keep; header panels drop to
# protect it on short terminals.
MIN_BROWSER_H = 6
THEME_PANEL_MAX = 9


# ── low-level paint helpers ─────────────────────────────────────────────────
def _addstr(stdscr, y, x, s, attr=0):
    """Guarded ``addstr`` — writing the last cell raises ``curses.error``."""
    if not s or y < 0 or x < 0:
        return
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass


def _attr_for(cid):
    """Resolve a color-pair id to a curses attribute (0 if id is falsy)."""
    if not cid:
        return 0
    try:
        return curses.color_pair(cid)
    except curses.error:
        return 0


def _draw_segments(stdscr, y, x, w, segments, attr=0):
    """Paint ``(text, color_id)`` segments starting at column ``x``, clipped to ``w``."""
    cx = 0
    for (txt, cid) in segments:
        if cx >= w:
            break
        trimmed = textwidth.clip(txt or "", w - cx)
        if not trimmed:
            continue
        _addstr(stdscr, y, x + cx, trimmed, _attr_for(cid) | attr)
        cx += textwidth.width(trimmed)


def display_colored_line(stdscr, row, segments, max_x, attr=0):
    """Back-compat wrapper: paint segments from column 0."""
    _draw_segments(stdscr, row, 0, max_x, segments, attr)


def init_colors():
    """Install static color pairs, the per-topic palette, and marquee accents."""
    try:
        curses.start_color()
    except curses.error:
        return
    bg = curses.COLOR_BLACK
    pairs = [
        (colors.TITLE, curses.COLOR_CYAN),
        (colors.DATE, curses.COLOR_GREEN),
        (colors.SOURCE, curses.COLOR_MAGENTA),
        (colors.NORMAL, curses.COLOR_WHITE),
        (colors.H1, curses.COLOR_YELLOW),
        (colors.H2, curses.COLOR_YELLOW),
        (colors.H3, curses.COLOR_YELLOW),
        (colors.BULLET, curses.COLOR_CYAN),
        (colors.BOLD, curses.COLOR_RED),
        (colors.ACCENT, curses.COLOR_RED),
        (colors.SENT_HYPE, curses.COLOR_GREEN),
        (colors.SENT_CONCERN, curses.COLOR_RED),
        (colors.SENT_NEUTRAL, curses.COLOR_WHITE),
        (colors.TOPIC, curses.COLOR_YELLOW),
        (colors.LINK, curses.COLOR_BLUE),
        (colors.BANNER, curses.COLOR_GREEN),
        (colors.MARQUEE, curses.COLOR_CYAN),
        (colors.MARQUEE_LABEL, curses.COLOR_YELLOW),
    ]
    for pair_id, fg in pairs:
        try:
            curses.init_pair(pair_id, fg, bg)
        except curses.error:
            continue
    try:
        has_256 = curses.COLORS >= 256
    except Exception:
        has_256 = False
    palette = colors.TOPIC_PALETTE_256 if has_256 else colors.TOPIC_PALETTE_8
    if palette:
        for i in range(colors.TOPIC_COLOR_COUNT):
            try:
                curses.init_pair(colors.TOPIC_PAIR_BASE + i,
                                 palette[i % len(palette)], bg)
            except curses.error:
                continue

    # Markdown palette (reader / theme / live-context): vivid 256-color hues
    # when available, base-8 fallback otherwise. These OVERRIDE the static pairs
    # so headings/bold/italic/code/links read as distinct colors.
    if has_256:
        md_pairs = [
            (colors.H1, 39), (colors.H2, 213), (colors.H3, 208),
            (colors.BOLD, 203), (colors.ITALIC, 147), (colors.CODE, 84),
            (colors.QUOTE, 109), (colors.MDRULE, 240), (colors.LINK, 81),
            (colors.BULLET, 220), (colors.ACCENT, 214),
        ]
    else:
        md_pairs = [
            (colors.H1, curses.COLOR_YELLOW), (colors.H2, curses.COLOR_CYAN),
            (colors.H3, curses.COLOR_MAGENTA), (colors.ITALIC, curses.COLOR_CYAN),
            (colors.CODE, curses.COLOR_GREEN), (colors.QUOTE, curses.COLOR_BLUE),
            (colors.MDRULE, curses.COLOR_MAGENTA),
        ]
    for pid, fg in md_pairs:
        try:
            curses.init_pair(pid, fg, bg)
        except curses.error:
            continue


def _age(ts):
    """Compact relative age: ``Xm`` / ``Xh`` / ``Xd`` (``""`` if unknown)."""
    if ts is None:
        return ""
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return ""
    if delta < 0:
        delta = 0
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _box(style):
    return glyphs.BOX.get(style) or glyphs.BOX["light"]


# ── box primitive ───────────────────────────────────────────────────────────
def _draw_box(stdscr, y, x, w, h, title=None, title_color=None,
              border_color=colors.NORMAL, style="round"):
    """Draw a bordered rectangle; return the inner ``(iy, ix, iw, ih)`` region."""
    if w < 2 or h < 2:
        return (y, x, max(0, w), max(0, h))
    g = _box(style)
    ba = _attr_for(border_color)
    hh = g["h"]
    if title:
        tt = " " + str(title) + " "
        fixed = textwidth.width(g["tl"] + hh + tt + g["tr"])
        fill = max(0, w - fixed)
        top = g["tl"] + hh + tt + hh * fill + g["tr"]
    else:
        top = g["tl"] + hh * (w - 2) + g["tr"]
    _addstr(stdscr, y, x, textwidth.clip(top, w), ba)
    if title and title_color:
        _addstr(stdscr, y, x + textwidth.width(g["tl"] + hh + " "),
                str(title), _attr_for(title_color) | curses.A_BOLD)
    for r in range(1, h - 1):
        _addstr(stdscr, y + r, x, g["v"], ba)
        _addstr(stdscr, y + r, x + w - 1, g["v"], ba)
    bot = g["bl"] + hh * (w - 2) + g["br"]
    _addstr(stdscr, y + h - 1, x, textwidth.clip(bot, w), ba)
    return (y + 1, x + 1, w - 2, h - 2)


def _fit_scroll(sel, scroll, view_h, total):
    """Adjust ``scroll`` so row ``sel`` stays visible within ``view_h`` rows."""
    if total <= 0 or view_h <= 0:
        return 0
    if sel < scroll:
        scroll = sel
    elif sel >= scroll + view_h:
        scroll = sel - view_h + 1
    return max(0, min(scroll, max(0, total - view_h)))


def _draw_rows(stdscr, iy, ix, iw, ih, rows, sel=None, scroll=0, dim_flags=None):
    """Render selectable ``rows`` (each a segment list) inside an inner region."""
    for i in range(ih):
        ri = scroll + i
        if ri >= len(rows):
            break
        segs = rows[ri]
        if sel is not None and ri == sel:
            # Selected row: keep the per-segment color axis under reverse video,
            # then pad the remainder of the row with reverse-video spaces.
            _draw_segments(stdscr, iy + i, ix, iw, segs, attr=curses.A_REVERSE)
            used = min(iw, textwidth.width(textwidth.clip(
                "".join(t for t, _ in segs), iw)))
            pad = max(0, iw - used)
            if pad:
                _addstr(stdscr, iy + i, ix + used, " " * pad, curses.A_REVERSE)
        else:
            attr = curses.A_DIM if (dim_flags and ri < len(dim_flags) and dim_flags[ri]) else 0
            _draw_segments(stdscr, iy + i, ix, iw, segs, attr=attr)


# ── text → wrapped segment lines ────────────────────────────────────────────
def _wrap_plain(text, width, cid=colors.NORMAL):
    if width <= 0:
        return []
    return word_wrap_line_segments([(text or "", cid)], width) or [[("", cid)]]


def _wrap_md(text, width):
    if width <= 0:
        return []
    out = []
    try:
        for seg in markdown_render.parse_markdown_text_to_segments(text or ""):
            for wl in (word_wrap_line_segments(seg, width) or [seg]):
                out.append(wl)
    except Exception:
        out = _wrap_plain(text, width)
    return out


# ── marquee ─────────────────────────────────────────────────────────────────
def _slice_by_width(s, start_cells, span_cells):
    if span_cells <= 0:
        return ""
    out, pos, collected = [], 0, 0
    for ch in s:
        w = textwidth.char_width(ch)
        if pos + w <= start_cells:
            pos += w
            continue
        if pos < start_cells:
            half = min((pos + w) - start_cells, span_cells - collected)
            out.append(" " * half)
            collected += half
            pos += w
            if collected >= span_cells:
                break
            continue
        if collected + w > span_cells:
            out.append(" " * (span_cells - collected))
            collected = span_cells
            break
        out.append(ch)
        collected += w
        pos += w
        if collected >= span_cells:
            break
    if collected < span_cells:
        out.append(" " * (span_cells - collected))
    return "".join(out)


def _draw_marquee(stdscr, row, leaderboard, W, offset):
    """Static label + right-to-left scrolling ribbon of ALL ranked top stories."""
    if W <= 0 or not leaderboard:
        return
    label = "📡 TOP STORIES ▸ "
    parts = []
    for it in leaderboard:
        if not it:
            continue
        badge = glyphs.rank_badge(it.get("rank") or 0)
        title = (it.get("title") or "")
        reason = (it.get("reason") or "")
        seg = f"{badge} {title} — {reason}" if reason else f"{badge} {title}"
        parts.append(seg.strip())
    text = "   ◆   ".join(p for p in parts if p)
    label_shown = textwidth.clip(label, W)
    label_w = textwidth.width(label_shown)
    _addstr(stdscr, row, 0, label_shown,
            _attr_for(colors.MARQUEE_LABEL) | curses.A_BOLD)
    avail = W - label_w
    if avail <= 0 or not text.strip():
        return
    ribbon = text + "      •      "
    rw = textwidth.width(ribbon)
    if rw <= 0:
        return
    start = int(offset) % rw
    reps = ((start + avail) // rw) + 2
    window = _slice_by_width(ribbon * reps, start, avail)
    _addstr(stdscr, row, label_w, window, _attr_for(colors.MARQUEE))


# ── header regions ──────────────────────────────────────────────────────────
def _draw_banner(stdscr, y, max_x, state):
    """The heavy ⚡ banner box: counts + one-word mood + sentiment mix."""
    n_stories = len(state.articles)
    n_topics = len(state.topic_order)
    title = f"{glyphs.SEC_TITLE} AI NEWS  ·  {n_stories} stories  ·  {n_topics} topics"
    iy, ix, iw, ih = _draw_box(stdscr, y, 0, max_x, 3, title=title,
                               title_color=colors.BANNER,
                               border_color=colors.BANNER, style="heavy")
    sc = state.sentiment_counts()
    mood = (f"{glyphs.sentiment_emoji('hype')} {sc.get('hype', 0)}   "
            f"{glyphs.sentiment_emoji('concern')} {sc.get('concern', 0)}   "
            f"{glyphs.sentiment_emoji('neutral')} {sc.get('neutral', 0)}")
    segs = [("✨ ", colors.ACCENT),
            ((state.one_word or "—"), colors.ACCENT),
            ("     ", 0),
            (mood, colors.NORMAL)]
    _draw_segments(stdscr, iy, ix, iw, segs)


def _draw_theme_context(stdscr, y, max_x, h, state):
    """Side-by-side 🌐 THEME and 🔎 LIVE CONTEXT panels."""
    half = max_x // 2
    # THEME (left)
    iy, ix, iw, ih = _draw_box(stdscr, y, 0, half, h, title=f"{glyphs.SEC_THEME} THEME",
                               title_color=colors.H2, border_color=colors.H2,
                               style="round")
    theme_lines = _wrap_md(state.theme or "(no theme yet)", iw)
    _draw_rows(stdscr, iy, ix, iw, ih, theme_lines)
    # LIVE CONTEXT (right)
    rx = half
    rw = max_x - half
    iy, ix, iw, ih = _draw_box(stdscr, y, rx, rw, h,
                               title=f"{glyphs.SEC_CONTEXT} LIVE CONTEXT",
                               title_color=colors.LINK, border_color=colors.LINK,
                               style="round")
    rows = _live_context_lines(state, iw)
    _draw_rows(stdscr, iy, ix, iw, ih, rows)


def _live_context_lines(state, width):
    g = state.grounding or {}
    rows = []
    head = g.get("headline")
    if head:
        rows += _wrap_plain(head, width, colors.H3)
    md = g.get("markdown")
    if md:
        rows += _wrap_md(md, width)
    cites = g.get("citations") or []
    if cites:
        rows.append([(f"{glyphs.SEC_SOURCES} Sources:", colors.H2)])
        for c in cites:
            label = (c.get("title") or c.get("url") or "")
            rows += _wrap_plain("➜ " + label, width, colors.LINK)
    if not rows:
        rows = [[("(no live context)", colors.NORMAL)]]
    return rows


# ── two-pane browser ────────────────────────────────────────────────────────
def _topic_rows(state, groups):
    rows = []
    for topic, arts in groups:
        cid = state.topic_colors.get(topic, colors.TOPIC)
        emoji = state.topic_emojis.get(topic) or glyphs.topic_emoji(topic)
        rows.append([(f"{emoji} ", cid), (topic, cid),
                     (f"  {len(arts)}", colors.NORMAL)])
    return rows


def _story_rows(state, arts, width):
    rows, dims = [], []
    for a in arts:
        star = "★ " if getattr(a, "bookmarked", False) else "  "
        glyph = glyphs.sentiment_glyph(a.sentiment, getattr(a, "read", False))
        title = textwidth.clip(a.title or "", max(16, width - 22))
        rows.append([
            (star, colors.ACCENT if getattr(a, "bookmarked", False) else 0),
            (glyph + " ", colors.sentiment_color(a.sentiment)),
            (title, colors.NORMAL),
            ("  ", 0),
            (a.feed_name or "", colors.SOURCE),
            (" ", 0),
            (_age(getattr(a, "published_ts", None)), colors.DATE),
        ])
        dims.append(getattr(a, "read", False))
    return rows, dims


# ── status bar ──────────────────────────────────────────────────────────────
_KEY_HINT = ("←/→ panes  ↑/↓ move  Space/Enter open  a ask  t overview  / search  "
             "f source  b ★  m read  B/u views  e export  ? help  q quit")


def _draw_status_bar(stdscr, row, max_x, text):
    if max_x <= 0 or row < 0:
        return
    text = textwidth.clip(text or "", max_x - 1) if max_x > 1 else ""
    pad = max(0, (max_x - 1) - textwidth.width(text))
    _addstr(stdscr, row, 0, text + " " * pad, curses.A_REVERSE)


# ── overlays ────────────────────────────────────────────────────────────────
def _restore_timeout(stdscr):
    try:
        stdscr.timeout(config.MARQUEE_TICK_MS)
    except Exception:
        pass


def _story_header_lines(article, state, width):
    """Title + meta block shared by the summary and reader views."""
    rows = []
    rows += _wrap_plain(article.title or "", width, colors.H1)
    rows.append([("", 0)])
    meta = f"{article.feed_name or ''} · {_age(getattr(article,'published_ts',None))} ago"
    if getattr(article, "bookmarked", False):
        meta += " · ★ bookmarked"
    n = max(1, getattr(article, "cluster_size", 1))
    if n > 1:
        meta += f" · {n} outlets"
    rows.append([(meta, colors.DATE)])
    rows.append([("", 0)])
    return rows


def _story_summary_lines(article, state, width):
    """Story card body: header + RSS summary (+ live context if it's the top story)."""
    rows = _story_header_lines(article, state, width)
    summary = (article.summary or "").strip() or "(no summary available)"
    rows += _wrap_plain(summary, width, colors.NORMAL)
    g = state.grounding or {}
    if g and g.get("headline") == article.title and g.get("markdown"):
        rows.append([("", 0)])
        rows.append([(f"{glyphs.SEC_CONTEXT} LIVE CONTEXT", colors.LINK)])
        rows += _wrap_md(g.get("markdown", ""), width)
        for c in (g.get("citations") or []):
            rows += _wrap_plain("➜ " + (c.get("title") or c.get("url") or ""),
                                width, colors.LINK)
    rows.append([("", 0)])
    rows.append([(f"🔗 {article.link or '(no link)'}", colors.LINK)])
    return rows


def _story_reader_lines(article, state, width, reader_data):
    """Story card body in reader mode: header + scraped full-article text."""
    rows = _story_header_lines(article, state, width)
    if reader_data is None:
        rows.append([("⏳ Fetching article…", colors.H2)])
        return rows
    if not reader_data.get("ok"):
        rows.append([("⚠ Could not load article: "
                      + (reader_data.get("error") or "unknown"), colors.SENT_CONCERN)])
        rows.append([("", 0)])
        rows.append([("Showing the RSS summary instead:", colors.NORMAL)])
        rows.append([("", 0)])
        rows += _wrap_plain((article.summary or "").strip() or "(no summary)",
                            width, colors.NORMAL)
        return rows
    rows.append([("📖 Full article", colors.H2)])
    rows.append([("", 0)])
    rows += _wrap_md(reader_data.get("text", ""), width)
    rows.append([("", 0)])
    rows.append([(f"🔗 {article.link or ''}", colors.LINK)])
    return rows


def _show_story_page(stdscr, article, state):
    """Framed-card story page: scrollable, with an ``r`` reader mode.

    ``o``/Enter open the article in the real browser (WSL-aware), ``r`` scrapes
    and shows the full article inline, ↑/↓ scroll, ``b`` bookmarks, ←/Esc back.
    """
    try:
        state.mark_read(article)
    except Exception:
        pass
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    mode = "summary"          # or "reader"
    reader_data = None
    reader_loaded = False
    scroll = 0
    msg = None
    reader_on = bool(getattr(config, "ENABLE_READER", True))
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()
            cw = min(max_x - 2, 100)
            cx = max(0, (max_x - cw) // 2)
            ch = max(6, min(max_y - 2, 40))
            cy = max(0, (max_y - ch) // 2)

            topic = article.topic or "General"
            tcid = state.topic_colors.get(topic, colors.TOPIC)
            emoji = state.topic_emojis.get(topic) or glyphs.topic_emoji(topic)
            iy, ix, iw, ih = _draw_box(stdscr, cy, cx, cw, ch,
                                       title=f"{emoji} {topic.upper()}",
                                       title_color=tcid, border_color=tcid,
                                       style="round")
            sent_lbl = f"{glyphs.sentiment_emoji(article.sentiment)} {article.sentiment}"
            badge = textwidth.clip(sent_lbl, max(0, iw))
            _addstr(stdscr, cy, cx + cw - 1 - textwidth.width(badge) - 1,
                    badge, _attr_for(colors.sentiment_color(article.sentiment)))

            if mode == "reader":
                body = _story_reader_lines(article, state, iw, reader_data)
            else:
                body = _story_summary_lines(article, state, iw)

            view_h = max(1, ih - 1)              # reserve last inner row for hints
            maxscroll = max(0, len(body) - view_h)
            scroll = max(0, min(scroll, maxscroll))
            _draw_rows(stdscr, iy, ix, iw, view_h, body, scroll=scroll)

            if msg:
                hint = msg
            else:
                read_lbl = ""
                if reader_on:
                    read_lbl = "  r summary" if mode == "reader" else "  r read full"
                more = "  ▾" if scroll < maxscroll else ""
                hint = f"o/Enter browser{read_lbl}  ↑/↓ scroll  b ★  ←/Esc back{more}"
            _addstr(stdscr, iy + ih - 1, ix, textwidth.clip(hint, iw), curses.A_BOLD)
            stdscr.refresh()

            # Lazily fetch the article after the ⏳ frame is on screen.
            if mode == "reader" and not reader_loaded:
                reader_data = reader.fetch_readable(
                    getattr(article, "link", ""), getattr(state, "cache", None))
                reader_loaded = True
                scroll = 0
                continue

            k = stdscr.getch()
            msg = None
            if k in (ord('o'), 10, 13, curses.KEY_ENTER):
                ok = openurl.open_url(getattr(article, "link", None))
                msg = ("Opening in browser…" if ok
                       else "Couldn't open a browser — try Ctrl+click the link")
            elif reader_on and k == ord('r'):
                mode = "summary" if mode == "reader" else "reader"
                scroll = 0
            elif k in (curses.KEY_DOWN, ord('j')):
                scroll += 1
            elif k in (curses.KEY_UP, ord('k')):
                scroll = max(0, scroll - 1)
            elif k == curses.KEY_NPAGE:
                scroll += view_h
            elif k == curses.KEY_PPAGE:
                scroll = max(0, scroll - view_h)
            elif k in (curses.KEY_HOME, ord('g')):
                scroll = 0
            elif k in (curses.KEY_END, ord('G')):
                scroll = maxscroll
            elif k == ord('b'):
                try:
                    state.toggle_bookmark(article)
                except Exception:
                    pass
            elif k in (ord('q'), 27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8):
                return
    finally:
        _restore_timeout(stdscr)


def _show_overview(stdscr, state):
    """Scrollable reader: top stories + full theme + live context + topic-mix bars."""
    rows = []
    max_y, max_x = stdscr.getmaxyx()
    w = max(10, min(max_x, config.WRAP_LIMIT) - 2)
    leaderboard = list(getattr(state, "leaderboard", None) or [])
    if leaderboard:
        rows.append([("🏆 TOP STORIES", colors.H1)])
        rows.append([("", 0)])
        for it in leaderboard:
            if not it:
                continue
            badge = glyphs.rank_badge(it.get("rank") or 0)
            tcid = state.topic_colors.get(it.get("topic"), colors.H1)
            rows.append([(badge + " ", colors.ACCENT),
                         ((it.get("title") or ""), tcid)])
            reason = (it.get("reason") or "").strip()
            if reason:
                rows.append([("   " + reason, colors.QUOTE)])
        rows.append([("", 0)])
    rows.append([(f"{glyphs.SEC_THEME} THEME", colors.H1)])
    rows.append([("", 0)])
    rows += _wrap_md(state.theme or "(no theme)", w)
    rows.append([("", 0)])
    rows.append([(f"{glyphs.SEC_MIX} TOPIC MIX", colors.H1)])
    counts = state.topic_counts or {}
    mx = max(counts.values()) if counts else 1
    for t in state.topic_order:
        cid = state.topic_colors.get(t, colors.TOPIC)
        emoji = state.topic_emojis.get(t) or glyphs.topic_emoji(t)
        name = t + " " * max(0, 20 - textwidth.width(t))
        rows.append([(f"{emoji} ", cid), (name, cid),
                     (glyphs.bar(counts.get(t, 0), mx, 30), cid),
                     (f" {counts.get(t, 0)}", colors.NORMAL)])
    if state.grounding:
        rows.append([("", 0)])
        rows.append([(f"{glyphs.SEC_CONTEXT} LIVE CONTEXT", colors.H1)])
        rows += _live_context_lines(state, w)

    scroll = 0
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()
            view = max_y - 1
            for i in range(view):
                ri = scroll + i
                if ri >= len(rows):
                    break
                _draw_segments(stdscr, i, 0, max_x, rows[ri])
            _draw_status_bar(stdscr, max_y - 1, max_x,
                             "↑/↓ scroll   q/Esc back")
            stdscr.refresh()
            k = stdscr.getch()
            if k in (curses.KEY_DOWN, ord('j')):
                scroll = min(max(0, len(rows) - view), scroll + 1)
            elif k in (curses.KEY_UP, ord('k')):
                scroll = max(0, scroll - 1)
            elif k == curses.KEY_NPAGE:
                scroll = min(max(0, len(rows) - view), scroll + view)
            elif k == curses.KEY_PPAGE:
                scroll = max(0, scroll - view)
            elif k in (ord('q'), 27):
                return
    finally:
        _restore_timeout(stdscr)


def _draw_centered_frame(stdscr, text, color=colors.H2):
    """Erase + draw a single centered line (used for the ⏳ thinking frame)."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    shown = textwidth.clip(text or "", max(0, max_x - 1))
    x = max(0, (max_x - textwidth.width(shown)) // 2)
    _addstr(stdscr, max(0, max_y // 2), x, shown, _attr_for(color) | curses.A_BOLD)
    stdscr.refresh()


def _show_chat(stdscr, state):
    """Chat-with-your-feed overlay: ask a question, get a cited answer.

    ``a``/``/`` ask again, ↑/↓ PgUp/PgDn scroll, ``q``/Esc exit. Defensive:
    needs ``state.client``; otherwise the caller surfaces a transient hint.
    """
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    try:
        while True:
            question = _prompt_input(stdscr, "Ask the feed: ")
            if not question:
                return
            _draw_centered_frame(stdscr, "⏳ thinking…")
            try:
                ans = enrich.ask_feed(
                    state.client, question, state.visible_articles(),
                    getattr(state, "theme", ""), getattr(state, "grounding", None),
                    getattr(state, "cache", None))
            except Exception as exc:
                ans = {"text": f"(could not answer: {exc})", "citations": []}
            if not isinstance(ans, dict):
                ans = {"text": str(ans), "citations": []}

            # Build the scrollable result view.
            max_y, max_x = stdscr.getmaxyx()
            w = max(10, min(max_x, config.WRAP_LIMIT) - 2)
            rows = []
            rows += _wrap_plain(question, w, colors.H1)
            rows.append([("", 0)])
            rows += _wrap_md(ans.get("text", ""), w)
            cites = ans.get("citations") or []
            if cites:
                rows.append([("", 0)])
                rows.append([("🔗 Sources:", colors.H2)])
                for c in cites:
                    title = (c.get("title") or "") if isinstance(c, dict) else ""
                    url = (c.get("url") or "") if isinstance(c, dict) else str(c)
                    label = f"{title} — {url}" if title else url
                    rows += _wrap_plain("➜ " + label, w, colors.LINK)

            scroll = 0
            reask = False
            while True:
                max_y, max_x = stdscr.getmaxyx()
                stdscr.erase()
                view = max_y - 1
                for i in range(view):
                    ri = scroll + i
                    if ri >= len(rows):
                        break
                    _draw_segments(stdscr, i, 0, max_x, rows[ri])
                _draw_status_bar(stdscr, max_y - 1, max_x,
                                 "↑/↓ scroll   a/ ask again   q/Esc back")
                stdscr.refresh()
                k = stdscr.getch()
                if k in (curses.KEY_DOWN, ord('j')):
                    scroll = min(max(0, len(rows) - view), scroll + 1)
                elif k in (curses.KEY_UP, ord('k')):
                    scroll = max(0, scroll - 1)
                elif k == curses.KEY_NPAGE:
                    scroll = min(max(0, len(rows) - view), scroll + view)
                elif k == curses.KEY_PPAGE:
                    scroll = max(0, scroll - view)
                elif k in (curses.KEY_HOME, ord('g')):
                    scroll = 0
                elif k in (curses.KEY_END, ord('G')):
                    scroll = max(0, len(rows) - view)
                elif k in (ord('a'), ord('/')):
                    reask = True
                    break
                elif k in (ord('q'), 27):
                    return
            if not reask:
                return
    finally:
        _restore_timeout(stdscr)


# ── main loop ────────────────────────────────────────────────────────────────
def curses_main(stdscr, state):
    """Render ``state`` as the animated two-pane browser and handle input."""
    curses.curs_set(0)
    init_colors()
    stdscr.keypad(True)
    try:
        stdscr.timeout(config.MARQUEE_TICK_MS)
    except Exception:
        pass

    focus = "left"          # "left" (topics) or "right" (stories)
    topic_idx = 0
    topic_scroll = 0
    story_idx = 0
    story_scroll = 0
    marquee_offset = 0
    transient = None
    dirty = True
    groups = []

    while True:
        max_y, max_x = stdscr.getmaxyx()
        if dirty:
            try:
                groups = state.topic_groups()
            except Exception:
                groups = []
            if topic_idx >= len(groups):
                topic_idx = max(0, len(groups) - 1)
            story_idx = 0 if not groups else min(story_idx, max(0, len(groups[topic_idx][1]) - 1))
            dirty = False

        cur_stories = groups[topic_idx][1] if groups else []

        stdscr.erase()  # fresh frame (also wipes any returned-from modal remnants)

        # --- layout: marquee, banner, theme/context, then the two panes -------
        content_h = max_y - 1            # reserve bottom row for status bar
        y = 0
        ribbon = list(getattr(state, "leaderboard", None) or [])
        if not ribbon and state.headline_of_day:
            ribbon = [state.headline_of_day]
        if ribbon and content_h - y > MIN_BROWSER_H + 1:
            _draw_marquee(stdscr, y, ribbon,
                          min(max_x, config.WRAP_LIMIT), marquee_offset)
            y += 1
        if content_h - y >= MIN_BROWSER_H + 3:
            _draw_banner(stdscr, y, max_x, state)
            y += 3
        if content_h - y >= MIN_BROWSER_H + 6:
            th = min(THEME_PANEL_MAX, content_h - y - MIN_BROWSER_H)
            th = max(4, th)
            _draw_theme_context(stdscr, y, max_x, th, state)
            y += th

        browser_y = y
        browser_h = max(2, content_h - y)

        # left (topics) + right (stories)
        lw = min(30, max(18, max_x // 4))
        rw = max_x - lw
        left_focus = focus == "left"
        topic_rows = _topic_rows(state, groups)
        ti_y, ti_x, ti_w, ti_h = _draw_box(
            stdscr, browser_y, 0, lw, browser_h, title="TOPICS",
            title_color=colors.BANNER,
            border_color=colors.MARQUEE_LABEL if left_focus else colors.NORMAL,
            style="round")
        topic_scroll = _fit_scroll(topic_idx, topic_scroll, ti_h, len(topic_rows))
        _draw_rows(stdscr, ti_y, ti_x, ti_w, ti_h, topic_rows,
                   sel=topic_idx if (left_focus and topic_rows) else None,
                   scroll=topic_scroll)

        cur_topic = groups[topic_idx][0] if groups else "—"
        cur_cid = state.topic_colors.get(cur_topic, colors.TOPIC)
        cur_emoji = state.topic_emojis.get(cur_topic) or glyphs.topic_emoji(cur_topic)
        s_title = f"{cur_emoji} {cur_topic} ({len(cur_stories)})"
        si_y, si_x, si_w, si_h = _draw_box(
            stdscr, browser_y, lw, rw, browser_h, title=s_title,
            title_color=cur_cid,
            border_color=colors.MARQUEE_LABEL if not left_focus else colors.NORMAL,
            style="round")
        story_rows, dims = _story_rows(state, cur_stories, si_w)
        if not story_rows:
            _addstr(stdscr, si_y, si_x, "(no stories)", _attr_for(colors.NORMAL))
        else:
            story_scroll = _fit_scroll(story_idx, story_scroll, si_h, len(story_rows))
            _draw_rows(stdscr, si_y, si_x, si_w, si_h, story_rows,
                       sel=story_idx if not left_focus else None,
                       scroll=story_scroll, dim_flags=dims)

        if transient is not None:
            bar_text = transient
        elif state.is_filtering():
            sl = state.status_lines()
            bar_text = (sl[0] + "  ·  c clear") if sl else _KEY_HINT
        else:
            bar_text = _KEY_HINT
        _draw_status_bar(stdscr, max_y - 1, max_x, bar_text)
        stdscr.refresh()

        key = stdscr.getch()
        if key == -1:
            marquee_offset += 1
            continue
        if transient is not None:
            transient = None

        # --- focus movement --------------------------------------------------
        if key in (curses.KEY_LEFT, ord('h')):
            focus = "left"
        elif key in (curses.KEY_RIGHT, ord('l')):
            if cur_stories:
                focus = "right"
        elif key == 9:  # Tab
            focus = "right" if (focus == "left" and cur_stories) else "left"

        # --- vertical movement within the focused pane -----------------------
        elif key in (curses.KEY_UP, ord('k')):
            if focus == "left":
                if topic_idx > 0:
                    topic_idx -= 1
                    story_idx = story_scroll = 0
            else:
                story_idx = max(0, story_idx - 1)
        elif key in (curses.KEY_DOWN, ord('j')):
            if focus == "left":
                if topic_idx < len(groups) - 1:
                    topic_idx += 1
                    story_idx = story_scroll = 0
            else:
                story_idx = min(max(0, len(cur_stories) - 1), story_idx + 1)
        elif key == curses.KEY_NPAGE:
            if focus == "left":
                topic_idx = min(max(0, len(groups) - 1), topic_idx + browser_h)
                story_idx = story_scroll = 0
            else:
                story_idx = min(max(0, len(cur_stories) - 1), story_idx + browser_h)
        elif key == curses.KEY_PPAGE:
            if focus == "left":
                topic_idx = max(0, topic_idx - browser_h)
                story_idx = story_scroll = 0
            else:
                story_idx = max(0, story_idx - browser_h)
        elif key in (curses.KEY_HOME, ord('g')):
            if focus == "left":
                topic_idx = 0
                story_idx = story_scroll = 0
            else:
                story_idx = 0
        elif key in (curses.KEY_END, ord('G')):
            if focus == "left":
                topic_idx = max(0, len(groups) - 1)
                story_idx = story_scroll = 0
            else:
                story_idx = max(0, len(cur_stories) - 1)

        # --- open story page (Space / Enter / i) -----------------------------
        elif key in (ord(' '), 10, 13, curses.KEY_ENTER, ord('i')):
            if focus == "left":
                if cur_stories:
                    focus = "right"
            elif cur_stories and 0 <= story_idx < len(cur_stories):
                _show_story_page(stdscr, cur_stories[story_idx], state)
                dirty = True

        # --- overview overlay ------------------------------------------------
        elif key == ord('t'):
            _show_overview(stdscr, state)

        # --- chat-with-your-feed ---------------------------------------------
        elif key == ord('a') and getattr(config, "ENABLE_CHAT", True):
            if getattr(state, "client", None) is None:
                transient = "Chat needs an API key"
            else:
                try:
                    _show_chat(stdscr, state)
                except Exception:
                    pass
                finally:
                    _restore_timeout(stdscr)
                dirty = True

        # --- bookmark / read on selected story -------------------------------
        elif key == ord('b'):
            if cur_stories and 0 <= story_idx < len(cur_stories):
                try:
                    state.toggle_bookmark(cur_stories[story_idx])
                except Exception:
                    pass
                dirty = True
        elif key == ord('m'):
            if cur_stories and 0 <= story_idx < len(cur_stories):
                try:
                    state.toggle_read(cur_stories[story_idx])
                except Exception:
                    pass
                dirty = True

        # --- views -----------------------------------------------------------
        elif key == ord('B'):
            try:
                state.toggle_bookmarks_only()
            except Exception:
                pass
            topic_idx = story_idx = topic_scroll = story_scroll = 0
            dirty = True
        elif key == ord('u'):
            try:
                state.toggle_unread_only()
            except Exception:
                pass
            topic_idx = story_idx = topic_scroll = story_scroll = 0
            dirty = True

        # --- search ----------------------------------------------------------
        elif key == ord('/'):
            try:
                stdscr.timeout(-1)
                q = _prompt_input(stdscr, "/")
                state.set_search(q)
            except Exception:
                pass
            finally:
                _restore_timeout(stdscr)
            topic_idx = story_idx = topic_scroll = story_scroll = 0
            focus = "left"
            dirty = True

        # --- source filter ---------------------------------------------------
        elif key == ord('f'):
            try:
                stdscr.timeout(-1)
                choice = _pick_source(stdscr, state.sources())
            except Exception:
                choice = None
            finally:
                _restore_timeout(stdscr)
            if choice is not None:
                try:
                    state.set_source(None if choice == '__ALL__' else choice)
                except Exception:
                    pass
                topic_idx = story_idx = topic_scroll = story_scroll = 0
                focus = "left"
                dirty = True

        # --- export ----------------------------------------------------------
        elif key == ord('e'):
            try:
                path = state.export()
                transient = f"Exported → {path}"
            except Exception as exc:
                transient = f"Export failed: {exc}"

        # --- clear filters / help / quit -------------------------------------
        elif key in (ord('c'), 27):
            try:
                state.clear_filters()
            except Exception:
                pass
            topic_idx = story_idx = topic_scroll = story_scroll = 0
            focus = "left"
            dirty = True
        elif key == ord('?'):
            try:
                stdscr.timeout(-1)
                _show_help(stdscr)
            except Exception:
                pass
            finally:
                _restore_timeout(stdscr)
        elif key == ord('q'):
            break


# ── small modal helpers (search prompt, source picker, help) ────────────────
def _prompt_input(stdscr, label):
    """Bottom-row line editor; returns stripped text (``""`` on cancel)."""
    label = label or ""
    buf = []
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            row = max_y - 1
            text = label + "".join(buf)
            shown = textwidth.clip(text, max(0, max_x - 1))
            pad = max(0, (max_x - 1) - textwidth.width(shown))
            _addstr(stdscr, row, 0, shown + " " * pad, curses.A_REVERSE)
            cursor_x = min(textwidth.width(text), max(0, max_x - 1))
            try:
                stdscr.move(row, cursor_x)
            except curses.error:
                pass
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                break
            if ch == 27:
                buf = []
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if 32 <= ch < 127 or 160 <= ch < 256:
                buf.append(chr(ch))
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        _restore_timeout(stdscr)
    return "".join(buf).strip()


def _pick_source(stdscr, sources):
    """Centered picker; returns a source name, ``'__ALL__'``, or ``None``."""
    sources = list(sources or [])
    entries = [("All sources", "__ALL__")]
    for item in sources:
        try:
            name, count = item
        except Exception:
            name, count = str(item), 0
        entries.append((f"{name} ({count})", name))
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    sel = 0
    try:
        while True:
            max_y, max_x = stdscr.getmaxyx()
            stdscr.erase()
            _addstr(stdscr, 0, 0, textwidth.clip(
                "Select a source (Enter to choose, q/Esc to cancel):",
                max(0, max_x - 1)), curses.A_BOLD)
            avail = max(1, max_y - 2)
            top = sel - avail + 1 if sel >= avail else 0
            for i in range(avail):
                ei = top + i
                if ei >= len(entries):
                    break
                shown = textwidth.clip(entries[ei][0], max(0, max_x - 1))
                if ei == sel:
                    pad = max(0, (max_x - 1) - textwidth.width(shown))
                    _addstr(stdscr, i + 2, 0, shown + " " * pad, curses.A_REVERSE)
                else:
                    _addstr(stdscr, i + 2, 0, shown)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord('k')):
                sel = (sel - 1) % len(entries)
            elif ch in (curses.KEY_DOWN, ord('j')):
                sel = (sel + 1) % len(entries)
            elif ch == curses.KEY_PPAGE:
                sel = max(0, sel - avail)
            elif ch == curses.KEY_NPAGE:
                sel = min(len(entries) - 1, sel + avail)
            elif ch in (curses.KEY_HOME, ord('g')):
                sel = 0
            elif ch in (curses.KEY_END, ord('G')):
                sel = len(entries) - 1
            elif ch in (10, 13, curses.KEY_ENTER):
                return entries[sel][1]
            elif ch in (ord('q'), 27):
                return None
    finally:
        _restore_timeout(stdscr)


def _show_help(stdscr):
    """Full-screen keybinding overlay; any key returns."""
    help_lines = [
        "AI NEWS FEED — KEYBINDINGS",
        "-" * 40,
        "Browser",
        "  ← / h            focus TOPICS pane",
        "  → / l            focus STORIES pane",
        "  Tab              toggle pane focus",
        "  ↑/↓  j/k         move within the focused pane",
        "  PgUp/PgDn g/G    page / jump",
        "",
        "Story",
        "  Space / Enter    open the story page",
        "    in page:  o/Enter open in browser · r read full article",
        "              ↑/↓ scroll · b ★ · ←/Esc back",
        "  b                toggle ★ bookmark on selected story",
        "  m                toggle read/unread on selected story",
        "",
        "Views & filters",
        "  a                chat-with-your-feed (ask a question, cited answer)",
        "  t                overview (full theme / live context / mix)",
        "  /                fuzzy search (space-separated terms)",
        "  f                filter by source",
        "  B                bookmarks only (toggle)",
        "  u                unread only (toggle)",
        "  c / Esc          clear all filters & search",
        "",
        "Other",
        "  e                export Markdown digest",
        "  ?                this help     q  quit",
        "",
        "Press any key to return...",
    ]
    try:
        stdscr.timeout(-1)
    except Exception:
        pass
    try:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        for y, text in enumerate(help_lines):
            if y >= max_y:
                break
            _addstr(stdscr, y, 0, textwidth.clip(text, max(0, max_x - 1)),
                    curses.A_BOLD if y == 0 else 0)
        stdscr.refresh()
        stdscr.getch()
    finally:
        _restore_timeout(stdscr)
