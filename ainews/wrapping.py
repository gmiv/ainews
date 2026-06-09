"""Word wrapping of colored ``(text, color_id)`` segments for the TUI.

``word_wrap_line_segments`` breaks a line's segments into multiple lines whose
*display* width (measured with :func:`textwidth.width`, so emoji and wide glyphs
count as two cells) does not exceed ``wrap_limit``, preserving each word's color.
The UI's box/pane renderers call this to flow text inside panels and the reader.
"""

from . import textwidth


def word_wrap_line_segments(segments, wrap_limit=150):
    """Word-wrap a list of ``(text, color_id)`` segments to ``wrap_limit``.

    Wraps on word boundaries while preserving each word's color. Adjacent
    pieces sharing the same color are merged into a single segment so the
    rendered output coalesces runs of identically colored text. Length
    accounting uses :func:`textwidth.width` (terminal display cells, counting
    emoji/wide glyphs as two) so wide content wraps at the correct column.

    Returns a list of wrapped lines, each a list of ``(text, color_id)`` tuples.
    """
    wrapped_lines = []
    current_line = []
    current_length = 0

    for (segment_text, color_id) in segments:
        words = segment_text.split(" ")
        for i, w in enumerate(words):
            piece = ((" " + w) if i > 0 else w)
            if current_length + textwidth.width(piece) > wrap_limit:
                if current_line:
                    wrapped_lines.append(current_line)
                stripped = piece.strip()
                current_line = [(stripped, color_id)]
                current_length = textwidth.width(stripped)
            else:
                if not current_line:
                    current_line = [(piece, color_id)]
                    current_length = textwidth.width(piece)
                else:
                    if color_id == current_line[-1][1]:
                        last_text, last_color = current_line[-1]
                        current_line[-1] = (last_text + piece, last_color)
                    else:
                        current_line.append((piece, color_id))
                    current_length += textwidth.width(piece)

    if current_line:
        wrapped_lines.append(current_line)

    return wrapped_lines
