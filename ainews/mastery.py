"""The deliberate-practice core: a concept knowledge-graph + mastery state.

This is the "long-term memory + metacognition + goals" layer that turns the feed
from passive reading into skill-building. It owns three things:

  * the **taxonomy** — the static concept graph from :mod:`ainews.concepts_seed`
    (concepts, categories, prereq/related edges, gradable definitions);
  * **per-concept mastery state** — an EWMA ``understanding`` in [0, 1], a
    status (unseen → encountered → reviewing → mastered), a spaced-review
    schedule (SM-2-ish), and recorded misconceptions — persisted across runs in
    ``config.MASTERY_FILE`` (mirroring :class:`ainews.persist.Store`);
  * **adaptive difficulty** — the probe difficulty for a concept is a function
    of its current understanding (or, for fresh concepts, the learner's global
    level), so the Socratic tutor self-calibrates over the first couple of weeks.

Everything is fail-soft: a missing/corrupt store yields a fresh one, and every
write swallows its errors so practice tracking can never crash the TUI.
"""
import json
import os
import re
import time

from . import config
from .concepts_seed import CONCEPTS, CATEGORIES, CATEGORY_ORDER

# concept id -> seed dict, built once.
_BY_ID = {c["id"]: c for c in CONCEPTS}

STATUSES = ("unseen", "encountered", "reviewing", "mastered")
STATUS_GLYPH = {
    "unseen": "○", "encountered": "◔", "reviewing": "◑", "mastered": "●",
}


# ── concept tagging (deterministic, free) ────────────────────────────────────
def _alias_pattern(term):
    """Separator-tolerant, case-aware word-boundary regex for one name/alias.

    Hyphen / space / underscore are treated as equivalent separators, so
    ``fine-tuning`` also matches ``fine tuning`` and ``fine_tuning``.
    Acronyms / Title-case single tokens (e.g. ``CLIP``, ``MoE``, ``RLHF``)
    match case-SENSITIVELY so they don't tag the ordinary words "clip"/"moe";
    multiword phrases and all-lowercase tokens match case-insensitively so
    recall on normal prose stays high.
    """
    term = (term or "").strip()
    if not term:
        return None
    parts = [p for p in re.split(r"[\s_-]+", term) if p]
    if not parts:
        return None
    core = r"[\s_-]+".join(re.escape(p) for p in parts)
    # \b doesn't sit well next to non-word edges; guard with lookarounds.
    pat = r"(?<![\w-])" + core + r"(?![\w-])"
    multiword = len(parts) > 1
    flags = 0 if (not multiword and re.search(r"[A-Z]", term)) else re.IGNORECASE
    try:
        return re.compile(pat, flags)
    except re.error:
        return None


def _build_matchers():
    """Map concept id -> list of compiled patterns over its name + aliases."""
    matchers = {}
    for c in CONCEPTS:
        terms = [c.get("name", "")] + list(c.get("aliases") or [])
        pats = [p for p in (_alias_pattern(t) for t in terms) if p is not None]
        if pats:
            matchers[c["id"]] = pats
    return matchers


_MATCHERS = _build_matchers()


def match_concepts(text):
    """Return the set of concept ids whose name/alias appears in ``text``."""
    if not text:
        return set()
    hits = set()
    for cid, pats in _MATCHERS.items():
        for p in pats:
            if p.search(text):
                hits.add(cid)
                break
    return hits


def tag_article(article):
    """Concept ids mentioned in an article's title + summary (best-effort)."""
    try:
        text = f"{getattr(article, 'title', '') or ''}\n{getattr(article, 'summary', '') or ''}"
        return match_concepts(text)
    except Exception:  # noqa: BLE001 - tagging is best-effort
        return set()


# ── taxonomy helpers ─────────────────────────────────────────────────────────
def get_concept(cid):
    """The static seed dict for ``cid`` (or None)."""
    return _BY_ID.get(cid)


def all_concepts():
    return list(CONCEPTS)


def _now():
    return time.time()


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _sanitize_state(st):
    """Coerce a loaded concept-state dict to valid types (fail-soft).

    A hand-edited or partially-written ``mastery.json`` may carry ``null`` or
    wrong-typed numeric fields; without this every ``int(...)``/``float(...)``
    read downstream would raise. We merge over clean defaults and coerce.
    """
    out = {
        "understanding": 0.0, "exposures": 0, "attempts": 0,
        "status": "unseen", "last_seen": None, "last_attempt": None,
        "due": None, "ease": config.MASTERY_EASE_START,
        "interval_days": 0.0, "misconceptions": [],
    }
    if isinstance(st, dict):
        out.update(st)
    for f, default in (("understanding", 0.0), ("ease", config.MASTERY_EASE_START),
                       ("interval_days", 0.0)):
        try:
            out[f] = float(out.get(f))
        except (TypeError, ValueError):
            out[f] = default
    for f in ("exposures", "attempts"):
        try:
            out[f] = int(out.get(f))
        except (TypeError, ValueError):
            out[f] = 0
    if out.get("status") not in STATUSES:
        out["status"] = "unseen"
    if not isinstance(out.get("misconceptions"), list):
        out["misconceptions"] = []
    for f in ("due", "last_seen", "last_attempt"):
        v = out.get(f)
        if v is not None and not isinstance(v, (int, float)):
            out[f] = None
    return out


# ── mastery store ────────────────────────────────────────────────────────────
class MasteryStore:
    """JSON-backed per-concept mastery state + adaptive difficulty.

    State per concept id::

        {understanding, exposures, attempts, status, last_seen, last_attempt,
         due, ease, interval_days, misconceptions: [...]}
    """

    def __init__(self, path=None):
        self.path = path or config.MASTERY_FILE
        self.concepts = {}      # id -> state dict
        self.level = 0.0        # global understanding estimate (for fresh concepts)
        self.sessions = 0
        self._load()

    # --- persistence --------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(blob, dict):
            return
        concepts = blob.get("concepts")
        if isinstance(concepts, dict):
            self.concepts = {str(k): _sanitize_state(v)
                             for k, v in concepts.items() if isinstance(v, dict)}
        try:
            self.level = float(blob.get("level", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.level = 0.0
        try:
            self.sessions = int(blob.get("sessions", 0) or 0)
        except (TypeError, ValueError):
            self.sessions = 0

    def save(self):
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                "concepts": self.concepts,
                "level": round(self.level, 4),
                "sessions": self.sessions,
                "updated": _now(),
            }
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except (OSError, TypeError):
            pass

    # --- state access -------------------------------------------------------
    def _state(self, cid):
        """Get-or-create the mutable state dict for a concept id."""
        st = self.concepts.get(cid)
        if st is None:
            st = {
                "understanding": 0.0, "exposures": 0, "attempts": 0,
                "status": "unseen", "last_seen": None, "last_attempt": None,
                "due": None, "ease": config.MASTERY_EASE_START,
                "interval_days": 0.0, "misconceptions": [],
            }
            self.concepts[cid] = st
        return st

    # --- exposure (a tagged story was seen) ---------------------------------
    def note_exposure(self, concept_ids, ts=None, save=True):
        """Mark concepts as encountered and bump exposure counts.

        Each distinct id in ``concept_ids`` is incremented by 1. The caller
        passes the day's unique tagged concepts once per run, so ``exposures``
        counts the number of runs/days a concept surfaced, not raw mentions.
        """
        ts = ts if ts is not None else _now()
        changed = False
        for cid in concept_ids or ():
            if cid not in _BY_ID:
                continue
            st = self._state(cid)
            st["exposures"] = int(st.get("exposures", 0)) + 1
            st["last_seen"] = ts
            if st.get("status") in (None, "unseen"):
                st["status"] = "encountered"
            changed = True
        if changed and save:
            self.save()
        return changed

    # --- graded attempt (the Socratic tutor scored an explanation) ----------
    def record_attempt(self, cid, score, misconceptions=None, ts=None,
                       save=True):
        """Fold a graded explanation (``score`` in [0,1]) into mastery state.

        Updates the EWMA understanding, the SM-2-ish review schedule, the
        status, the learner's global level, and the stored misconceptions.
        Returns the updated concept state (or None for an unknown id).
        """
        if cid not in _BY_ID:
            return None
        try:
            score = _clamp(float(score), 0.0, 1.0)
        except (TypeError, ValueError):
            return None
        ts = ts if ts is not None else _now()
        st = self._state(cid)

        # EWMA understanding (first attempt seeds it directly).
        alpha = config.MASTERY_EWMA_ALPHA
        if int(st.get("attempts", 0)) == 0:
            understanding = score
        else:
            understanding = alpha * score + (1 - alpha) * float(st.get("understanding", 0.0))
        st["understanding"] = round(_clamp(understanding, 0.0, 1.0), 4)
        st["attempts"] = int(st.get("attempts", 0)) + 1
        st["last_attempt"] = ts

        # SM-2-ish schedule. q in 0..5; <3 (≈score<0.6) is a lapse -> review soon.
        q = score * 5.0
        ease = float(st.get("ease", config.MASTERY_EASE_START))
        if q < 3.0:
            interval = 0.0
            ease = max(config.MASTERY_EASE_MIN, ease - 0.2)
        else:
            prev = float(st.get("interval_days", 0.0))
            interval = config.MASTERY_FIRST_INTERVAL if prev <= 0 else prev * ease
            ease = _clamp(ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)),
                          config.MASTERY_EASE_MIN, config.MASTERY_EASE_MAX)
        st["ease"] = round(ease, 3)
        st["interval_days"] = round(interval, 3)
        st["due"] = ts + interval * 86400.0

        # Status: "mastered" only while understanding stays at/above threshold
        # (with enough attempts); otherwise it's an active review item. Mastery
        # is not sticky — if understanding decays below the bar it drops back to
        # "reviewing" so the map and review queue stay honest.
        if (st["understanding"] >= config.MASTERY_THRESHOLD
                and st["attempts"] >= config.MASTERY_MIN_ATTEMPTS):
            st["status"] = "mastered"
        else:
            st["status"] = "reviewing"

        # Misconceptions (most-recent-first, deduped, capped).
        if misconceptions:
            existing = list(st.get("misconceptions") or [])
            for m in misconceptions:
                m = (m or "").strip()
                if m and m not in existing:
                    existing.insert(0, m)
            st["misconceptions"] = existing[: config.MASTERY_MAX_MISCONCEPTIONS]

        self._recompute_level()
        if save:
            self.save()
        return st

    def _recompute_level(self):
        scored = [float(s.get("understanding", 0.0))
                  for s in self.concepts.values() if int(s.get("attempts", 0)) > 0]
        self.level = round(sum(scored) / len(scored), 4) if scored else 0.0

    # --- adaptive difficulty ------------------------------------------------
    def difficulty_for(self, cid):
        """Return ``(band, guidance)`` for the tutor, adaptive to understanding.

        Uses the concept's own understanding once attempted, else the learner's
        global level (so a brand-new concept starts near where they already are
        rather than always at zero) — this is the self-calibration.
        """
        st = self.concepts.get(cid) or {}
        if int(st.get("attempts", 0)) > 0:
            u = float(st.get("understanding", 0.0))
        else:
            u = self.level if self.level > 0 else 0.4
        if u < 0.30:
            return ("foundations",
                    "Probe the core idea at a foundational level; ask them to "
                    "explain what it is and why it matters, simply and correctly.")
        if u < 0.60:
            return ("intermediate",
                    "Ask them to explain the mechanism and apply it to a concrete "
                    "example; check that the moving parts are correct.")
        if u < config.MASTERY_THRESHOLD:
            return ("advanced",
                    "Probe trade-offs, failure modes, and comparisons to "
                    "alternatives; surface subtle misconceptions.")
        return ("frontier",
                "Push novel synthesis: edge cases, recent advances, and "
                "connections to cognitive architecture and other concepts.")

    # --- queries for the review queue + graph view --------------------------
    def due_concepts(self, now=None, limit=None):
        """Attempted concept ids due for review, weakest/most-overdue first."""
        now = now if now is not None else _now()
        due = []
        for cid, st in self.concepts.items():
            if cid not in _BY_ID or int(st.get("attempts", 0)) <= 0:
                continue
            d = st.get("due")
            if d is None or d <= now:
                due.append((cid, float(st.get("understanding", 0.0)), d or 0))
        due.sort(key=lambda t: (t[1], t[2]))   # low understanding, then most overdue
        ids = [cid for cid, _, _ in due]
        return ids[:limit] if limit else ids

    def concept_view(self, cid):
        """Merged seed + state dict for rendering a single concept."""
        seed = _BY_ID.get(cid)
        if not seed:
            return None
        st = self.concepts.get(cid) or {}
        return {
            **seed,
            "understanding": float(st.get("understanding", 0.0)),
            "status": st.get("status", "unseen"),
            "exposures": int(st.get("exposures", 0)),
            "attempts": int(st.get("attempts", 0)),
            "due": st.get("due"),
            "misconceptions": list(st.get("misconceptions") or []),
        }

    def coverage(self, now=None):
        """Per-category + overall coverage stats for the knowledge-graph view."""
        now = now if now is not None else _now()
        cats = {}
        for cid in CATEGORY_ORDER:
            cats[cid] = {"total": 0, "encountered": 0, "reviewing": 0,
                         "mastered": 0, "due": 0, "sum_u": 0.0}
        for c in CONCEPTS:
            cat = c.get("category")
            bucket = cats.get(cat)
            if bucket is None:
                bucket = cats.setdefault(cat, {"total": 0, "encountered": 0,
                                               "reviewing": 0, "mastered": 0,
                                               "due": 0, "sum_u": 0.0})
            bucket["total"] += 1
            st = self.concepts.get(c["id"]) or {}
            status = st.get("status", "unseen")
            if status in ("encountered", "reviewing", "mastered"):
                bucket["encountered"] += 1
            if status == "reviewing":
                bucket["reviewing"] += 1
            if status == "mastered":
                bucket["mastered"] += 1
            bucket["sum_u"] += float(st.get("understanding", 0.0))
            if int(st.get("attempts", 0)) > 0:
                d = st.get("due")
                if d is None or d <= now:
                    bucket["due"] += 1
        for b in cats.values():
            b["mean_u"] = round(b["sum_u"] / b["total"], 3) if b["total"] else 0.0
        total = sum(b["total"] for b in cats.values())
        mastered = sum(b["mastered"] for b in cats.values())
        encountered = sum(b["encountered"] for b in cats.values())
        return {
            "categories": cats,
            "total": total, "mastered": mastered, "encountered": encountered,
            "level": self.level,
            "due": sum(b["due"] for b in cats.values()),
        }
