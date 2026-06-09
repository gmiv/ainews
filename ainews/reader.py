"""In-app article reader: fetch a story URL and extract its readable text.

Used by the story page's "reader" mode so you can read the full article inside
the TUI instead of leaving for the browser. Extraction prefers ``trafilatura``
(boilerplate-stripping, article-grade), then BeautifulSoup, then a crude stdlib
fallback — so it degrades gracefully if a dependency is missing. Results are
cached by URL. Everything is best-effort and never raises into the UI.
"""
import html as _html
import re

from . import config
from .cache import make_key

try:
    import trafilatura
except Exception:  # noqa: BLE001
    trafilatura = None

try:
    import requests
except Exception:  # noqa: BLE001
    requests = None


def _fetch_html(url):
    """Download raw HTML for ``url`` (requests if available, else urllib)."""
    headers = {"User-Agent": getattr(config, "USER_AGENT", "Mozilla/5.0")}
    timeout = getattr(config, "READER_TIMEOUT", 12)
    if requests is not None:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user-driven
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def _extract(html_text, url):
    """Return readable plain text from ``html_text`` (best extractor available)."""
    if trafilatura is not None:
        # Ask for Markdown so headings/bold/italic/lists/links survive for the
        # colorful renderer. Degrade through looser signatures if needed.
        for kwargs in (
            dict(url=url, include_comments=False, include_tables=True,
                 include_formatting=True, output_format="markdown"),
            dict(output_format="markdown"),
            dict(include_comments=False),
            {},
        ):
            try:
                txt = trafilatura.extract(html_text, **kwargs)
                if txt and txt.strip():
                    return txt.strip()
            except Exception:  # noqa: BLE001
                continue
    # BeautifulSoup fallback: emit light markdown (headings/lists/quotes).
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "form", "noscript", "svg"]):
            tag.decompose()
        blocks = []
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
            t = el.get_text(" ", strip=True)
            if not t:
                continue
            name = el.name
            if name == "h1":
                blocks.append("# " + t)
            elif name == "h2":
                blocks.append("## " + t)
            elif name in ("h3", "h4"):
                blocks.append("### " + t)
            elif name == "li":
                blocks.append("- " + t)
            elif name == "blockquote":
                blocks.append("> " + t)
            elif len(t) > 30:
                blocks.append(t)
        if blocks:
            return "\n\n".join(blocks)
        return soup.get_text("\n", strip=True)
    except Exception:  # noqa: BLE001
        pass
    # Crude stdlib strip.
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def fetch_readable(url, cache=None):
    """Return ``{"ok": bool, "text": str, "error": str}`` for ``url`` (cached)."""
    if not url:
        return {"ok": False, "text": "", "error": "no link"}
    key = make_key("reader", url)
    if cache is not None:
        try:
            cached = cache.get(key, getattr(config, "READER_CACHE_TTL", 86400))
            if cached is not None:
                return cached
        except Exception:  # noqa: BLE001
            pass
    try:
        text = _extract(_fetch_html(url), url)
        if not text or not text.strip():
            return {"ok": False, "text": "", "error": "no readable content found"}
        out = {"ok": True, "text": text.strip(), "error": ""}
        if cache is not None:
            try:
                cache.set(key, out)
            except Exception:  # noqa: BLE001
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "error": str(exc)[:140]}
