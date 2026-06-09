"""Terminal loading spinner (stderr), TTY-aware.

Provides a small, dependency-free spinner that animates on a single line while
work is in progress. It is intentionally defensive: it NEVER raises, and it
degrades to a no-op (aside from optional one-line notes) when the output stream
is not a TTY (e.g. piped to a file or another process).

Public API:
    Spinner(message='Working', stream=None)  -- context manager
    note(message, stream=None)               -- print a one-line bullet note
"""

import sys
import time
import threading
import itertools

# Braille spinner frames, cycled at ~10 fps.
_FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

# Animation interval in seconds (~10 frames per second).
_INTERVAL = 0.1


class Spinner:
    """A TTY-aware loading spinner usable as a context manager.

    Example:
        with Spinner('Fetching feeds') as sp:
            ...do work...
            sp.update('Classifying')
        # line is cleared automatically on exit

    On a non-TTY stream the spinner does not animate; ``update`` may emit an
    optional one-line note and ``succeed`` still prints a final status line.
    All methods are defensive and must never raise.
    """

    def __init__(self, message='Working', stream=None):
        self.stream = stream or sys.stderr
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def _is_tty(self):
        """Best-effort check for an interactive terminal; never raises."""
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def _write(self, text):
        """Write ``text`` to the stream and flush, swallowing any error."""
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def _animate(self):
        """Thread body: render spinner frames until stopped."""
        try:
            for frame in itertools.cycle(_FRAMES):
                if self._stop.is_set():
                    break
                line = '\r' + frame + ' ' + str(self.message)
                self._write(line)
                # Wait returns early (True) once stop is signaled.
                if self._stop.wait(_INTERVAL):
                    break
        except Exception:
            # Animation must never crash the program.
            pass

    def __enter__(self):
        """Start the animation thread when attached to a TTY."""
        try:
            if self._is_tty():
                self._stop.clear()
                self._thread = threading.Thread(target=self._animate, daemon=True)
                self._thread.start()
        except Exception:
            self._thread = None
        return self

    def update(self, message):
        """Update the spinner message; the animation thread picks it up.

        On a non-TTY stream, optionally emit a one-line note so progress is
        still visible in logs.
        """
        try:
            self.message = message
            if not self._is_tty():
                note(message, stream=self.stream)
        except Exception:
            pass

    def _clear_line(self):
        """Erase the current spinner line on the terminal."""
        try:
            width = len(str(self.message)) + 2
            self._write('\r' + (' ' * width) + '\r')
        except Exception:
            pass

    def __exit__(self, *exc):
        """Stop the animation, clear the line, and never suppress exceptions."""
        try:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1.0)
                self._thread = None
            if self._is_tty():
                self._clear_line()
        except Exception:
            pass
        return False

    def succeed(self, message=None):
        """Print a final success line ('✓ ...') to the stream."""
        try:
            text = message or self.message
            self._write('✓ ' + str(text) + '\n')
        except Exception:
            pass


def note(message, stream=None):
    """Write a one-line bullet note ('• ...') to stderr or ``stream``."""
    out = stream or sys.stderr
    try:
        out.write('• ' + str(message) + '\n')
        out.flush()
    except Exception:
        pass
