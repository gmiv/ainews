"""OpenAI Responses-API client wrapper for the AI news feed.

``LLMClient`` is a thin, *defensive* wrapper around
``openai.OpenAI().responses.create``. gpt-5.4 is a reasoning model whose exact
parameter names drift across the 5.x line, so every call degrades gracefully:
if the API rejects an optional parameter, the offending kwarg is dropped and the
call is retried. Nothing in here should ever crash the surrounding TUI -- callers
get back a best-effort string / dict, or an empty one.
"""
import json
import re

from . import config


# Optional kwargs we are willing to strip, in the order we sacrifice them when
# the API complains. Earlier entries are dropped first.
_DEGRADE_ORDER = ["include", "text", "reasoning", "tools", "max_output_tokens"]


class LLMClient:
    """Minimal Responses-API client with graceful parameter degradation."""

    def __init__(self, api_key=None, model=None):
        api_key = api_key or config.get_api_key()
        if not api_key:
            raise ValueError("No OpenAI API key")
        try:
            from openai import OpenAI
        except ImportError:
            raise ValueError("openai package not installed")
        self.model = model or config.MODEL
        self._client = OpenAI(api_key=api_key)

    # -- low-level call with degradation ------------------------------------
    def _which_key_to_strip(self, error_text, kwargs):
        """Return the first strippable kwarg referenced by ``error_text``.

        ``error_text`` is the lowercased ``str(exception)``. We look for the
        kwarg name itself, plus a few well-known aliases the API tends to use in
        its error messages (e.g. ``verbosity``/``format`` -> ``text``).
        """
        for key in _DEGRADE_ORDER:
            if key not in kwargs:
                continue
            if key in error_text:
                return key
            # Alias detection: the error may name the nested concept rather
            # than the top-level kwarg we actually pass.
            if key == "include" and ("web_search" in error_text or
                                     "include" in error_text):
                return key
            if key == "text" and ("verbosity" in error_text or
                                   "format" in error_text or
                                   "text" in error_text):
                return key
            if key == "reasoning" and ("reasoning" in error_text or
                                       "effort" in error_text):
                return key
        return None

    def _create(self, **kwargs):
        """Call ``responses.create``, degrading optional kwargs on failure.

        On each failure we strip the first present strippable kwarg whose name
        (or a known alias) appears in the error and retry. If the error does not
        reference any strippable kwarg, we make one final attempt with only
        ``model`` + ``input`` (and ``tools`` if present); if that fails too, the
        original exception propagates.
        """
        while True:
            try:
                return self._client.responses.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - API surface is broad
                error_text = str(exc).lower()
                strip_key = self._which_key_to_strip(error_text, kwargs)
                if strip_key is not None:
                    kwargs.pop(strip_key, None)
                    continue
                # Nothing recognizable was referenced: collapse to the minimal
                # request and try exactly once more.
                minimal = {k: kwargs[k] for k in ("model", "input", "tools")
                           if k in kwargs}
                if minimal == kwargs:
                    # Already minimal -- no further degradation possible.
                    raise
                try:
                    return self._client.responses.create(**minimal)
                except Exception:  # noqa: BLE001
                    raise

    # -- text extraction -----------------------------------------------------
    def _extract_text(self, response):
        """Best-effort plain-text extraction from a Responses object."""
        try:
            text = getattr(response, "output_text", None)
            if text:
                return text
        except Exception:  # noqa: BLE001
            pass

        chunks = []
        try:
            for item in getattr(response, "output", None) or []:
                try:
                    for part in getattr(item, "content", None) or []:
                        try:
                            piece = getattr(part, "text", None)
                            if piece:
                                chunks.append(piece)
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            return ""
        return "".join(chunks)

    # -- public API ----------------------------------------------------------
    def generate(self, prompt, *, system=None, effort=None, verbosity=None,
                 max_output_tokens=None, model=None):
        """Return the model's text response to ``prompt``.

        ``system`` is sent as a ``developer`` role message when provided;
        otherwise the prompt is passed as a plain string.
        """
        if system:
            input_payload = [
                {"role": "developer", "content": system},
                {"role": "user", "content": prompt},
            ]
        else:
            input_payload = prompt

        kwargs = {"model": model or self.model, "input": input_payload}
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if verbosity:
            kwargs["text"] = {"verbosity": verbosity}
        if max_output_tokens:
            kwargs["max_output_tokens"] = int(max_output_tokens)

        return self._extract_text(self._create(**kwargs)).strip()

    def generate_json(self, prompt, schema, *, system=None, effort=None,
                      verbosity=None, max_output_tokens=None, model=None):
        """Return structured output parsed from a JSON-schema-constrained call.

        Degradation may strip the structured ``format`` (the ``text`` kwarg), so
        we always fall back to parsing free text: first a direct ``json.loads``,
        then a regex-extracted first JSON object/array, then ``{}``.
        """
        if system:
            input_payload = [
                {"role": "developer", "content": system},
                {"role": "user", "content": prompt},
            ]
        else:
            input_payload = prompt

        text_kwarg = {
            "format": {
                "type": "json_schema",
                "name": "result",
                "strict": True,
                "schema": schema,
            }
        }
        if verbosity:
            text_kwarg["verbosity"] = verbosity

        kwargs = {
            "model": model or self.model,
            "input": input_payload,
            "text": text_kwarg,
        }
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if max_output_tokens:
            kwargs["max_output_tokens"] = int(max_output_tokens)

        raw = self._extract_text(self._create(**kwargs)).strip()
        return self._parse_json(raw)

    def web_search(self, prompt, *, allowed_domains=None, effort=None,
                   verbosity=None, max_output_tokens=None, model=None):
        """Run a web-search-grounded query and return text + citations.

        Returns ``{"text": str, "citations": [{"title", "url"}, ...]}`` with
        citations deduped by URL in first-seen order. All extraction is guarded;
        a degraded call that drops the tool simply yields no citations.
        """
        tool = {"type": "web_search"}
        if allowed_domains:
            tool["filters"] = {"allowed_domains": list(allowed_domains)}

        kwargs = {
            "model": model or self.model,
            "input": prompt,
            "tools": [tool],
            "include": ["web_search_call.action.sources"],
        }
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        if verbosity:
            kwargs["text"] = {"verbosity": verbosity}
        if max_output_tokens:
            kwargs["max_output_tokens"] = int(max_output_tokens)

        resp = self._create(**kwargs)
        text = self._extract_text(resp).strip()

        citations = []
        seen = set()
        try:
            for item in getattr(resp, "output", None) or []:
                try:
                    for part in getattr(item, "content", None) or []:
                        try:
                            for ann in getattr(part, "annotations", None) or []:
                                try:
                                    if getattr(ann, "type", None) != "url_citation":
                                        continue
                                    url = getattr(ann, "url", "") or ""
                                    if url in seen:
                                        continue
                                    seen.add(url)
                                    citations.append({
                                        "title": getattr(ann, "title", "") or "",
                                        "url": url,
                                    })
                                except Exception:  # noqa: BLE001
                                    continue
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        return {"text": text, "citations": citations}

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _parse_json(raw):
        """Parse ``raw`` into a dict/list, tolerating chatter around the JSON."""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
        # Fall back to the first balanced-looking JSON object or array.
        match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:  # noqa: BLE001
                pass
        return {}
