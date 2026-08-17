"""Anchor location and directional value windows.

This is the mechanism that replaces fixed rectangles. A template stores "the
value sits to the right of the text 'Invoice No.'", not "the value sits at
(412, 96)". The former survives a 30-line invoice; the latter does not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..models import Page, Word

DEFAULT_MIN_RATIO = 85.0


@dataclass(slots=True)
class AnchorHit:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int
    score: float

    @property
    def cy(self) -> float: return (self.y0 + self.y1) / 2.0
    @property
    def height(self) -> float: return max(self.y1 - self.y0, 1.0)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def find_anchors(page: Page, anchor_text: str, *, mode: str = "fuzzy",
                 min_ratio: float = DEFAULT_MIN_RATIO,
                 region_norm=None) -> list[AnchorHit]:
    """Locate every occurrence of an anchor label on a page.

    Matching runs over word n-grams so multi-word anchors ("Invoice No.",
    "Total Amount Due") are found even when the PDF splits them into separate
    word objects. Fuzzy matching is on by default because OCR turns
    "Invoice No." into "lnvoice N0." often enough to matter.
    """
    target = _norm(anchor_text)
    if not target:
        return []
    n_target = len(target.split())
    hits: list[AnchorHit] = []

    words = page.words
    if region_norm:
        words = page.words_in(page.to_abs(region_norm))
    if not words:
        return []

    by_line: dict[int, list[Word]] = {}
    for w in words:
        by_line.setdefault(w.line_id, []).append(w)

    for _, group in by_line.items():
        group = sorted(group, key=lambda w: w.x0)
        span = max(1, min(n_target + 2, len(group)))
        for start in range(len(group)):
            for size in range(1, span + 1):
                chunk = group[start:start + size]
                if not chunk:
                    continue
                candidate = _norm(" ".join(w.text for w in chunk))
                if not candidate:
                    continue
                if mode == "exact":
                    score = 100.0 if candidate == target else 0.0
                elif mode == "contains":
                    score = 100.0 if target in candidate else 0.0
                else:
                    score = float(fuzz.ratio(candidate, target))
                if score >= min_ratio:
                    hits.append(AnchorHit(
                        text=" ".join(w.text for w in chunk),
                        x0=min(w.x0 for w in chunk), y0=min(w.y0 for w in chunk),
                        x1=max(w.x1 for w in chunk), y1=max(w.y1 for w in chunk),
                        page=page.number, score=score))

    # Collapse overlapping hits, keeping the best-scoring one per position.
    hits.sort(key=lambda h: (-h.score, h.y0, h.x0))
    kept: list[AnchorHit] = []
    for h in hits:
        if not any(_overlaps(h, k) for k in kept):
            kept.append(h)
    kept.sort(key=lambda h: (h.y0, h.x0))
    return kept


def _overlaps(a: AnchorHit, b: AnchorHit) -> bool:
    return not (a.x1 <= b.x0 or b.x1 <= a.x0 or a.y1 <= b.y0 or b.y1 <= a.y0)


def value_window(anchor: AnchorHit, page: Page, *, direction: str = "right",
                 max_distance: float = 220.0,
                 vertical_tolerance: float = 6.0,
                 max_below: float = 40.0) -> tuple[float, float, float, float]:
    """Build the search bbox for a value, relative to its anchor."""
    d = (direction or "right").lower()
    band = max(vertical_tolerance, anchor.height * 0.75)

    if d == "right":
        return (anchor.x1, anchor.cy - band, min(page.width, anchor.x1 + max_distance),
                anchor.cy + band)
    if d == "left":
        return (max(0.0, anchor.x0 - max_distance), anchor.cy - band,
                anchor.x0, anchor.cy + band)
    if d == "below":
        return (anchor.x0 - band * 2, anchor.y1,
                min(page.width, anchor.x1 + max_distance),
                min(page.height, anchor.y1 + max_below))
    if d == "above":
        return (anchor.x0 - band * 2, max(0.0, anchor.y0 - max_below),
                min(page.width, anchor.x1 + max_distance), anchor.y0)
    if d == "same_line":
        return (anchor.x1, anchor.cy - band, page.width, anchor.cy + band)
    return (anchor.x1, anchor.cy - band, min(page.width, anchor.x1 + max_distance),
            anchor.cy + band)


def text_in_window(page: Page, bbox, *, stop_at: list[str] | None = None,
                   strip_label_chars: bool = True) -> tuple[str, tuple | None, list[Word]]:
    """Collect text inside a window, truncating at a competing label.

    `stop_at` prevents a value window from swallowing the next field: an
    "Invoice No." window extending right must stop before "Invoice Date".
    """
    words = sorted(page.words_in(bbox), key=lambda w: (round(w.cy, 1), w.x0))
    if not words:
        return "", None, []

    if stop_at:
        stops = [_norm(s) for s in stop_at if s]
        cut = len(words)
        for i, w in enumerate(words):
            wn = _norm(w.text)
            if not wn:
                continue
            for s in stops:
                first = s.split()[0] if s else ""
                if first and (wn == first or fuzz.ratio(wn, first) >= 92):
                    cut = min(cut, i)
                    break
        words = words[:cut]
    if not words:
        return "", None, []

    text = " ".join(w.text for w in words)
    if strip_label_chars:
        text = text.lstrip(":;-–—= \t").strip()
    text = re.sub(r"\s+", " ", text).strip()
    bb = (min(w.x0 for w in words), min(w.y0 for w in words),
          max(w.x1 for w in words), max(w.y1 for w in words))
    return text, bb, words


def infer_anchor(page: Page, bbox, *, exclude_words: set[str] | None = None
                 ) -> tuple[str, str] | None:
    """Given a user-drawn box, propose (anchor_text, direction).

    Used by the template editor to turn a drag gesture into a durable rule.
    Candidates that look like values rather than labels are rejected.
    """
    x0, y0, x1, y1 = bbox
    cy = (y0 + y1) / 2.0
    height = max(y1 - y0, 8.0)
    exclude = exclude_words or set()

    inside = {w.text for w in page.words_in(bbox)}
    best: tuple[float, str, str] | None = None

    for line in page.lines:
        for w in line.words:
            if w.text in inside or w.text in exclude:
                continue
            label = w.text.strip()
            if not label or len(label) < 2:
                continue
            if _looks_like_value(label):
                continue
            occurrences = sum(1 for ww in page.words if ww.text == w.text)
            if occurrences > 3:
                continue   # not discriminative

            # to the left, same line band
            if abs(w.cy - cy) <= height * 0.75 and w.x1 <= x0 + 2:
                dist = x0 - w.x1
                if 0 <= dist <= 260:
                    score = 100 - dist * 0.25
                    if label.endswith(":"):
                        score += 25
                    phrase = _grow_label(line, w, direction="left")
                    if best is None or score > best[0]:
                        best = (score, phrase, "right")
            # directly above
            if w.y1 <= y0 + 2 and (y0 - w.y1) <= height * 2.5:
                if not (w.x1 < x0 - 30 or w.x0 > x1 + 30):
                    score = 80 - (y0 - w.y1) * 0.4
                    phrase = _grow_label(line, w, direction="whole")
                    if best is None or score > best[0]:
                        best = (score, phrase, "below")
    if best is None:
        return None
    return best[1], best[2]


def _grow_label(line, word: Word, direction: str = "left") -> str:
    """Expand a single word into the full label phrase on its line."""
    words = sorted(line.words, key=lambda w: w.x0)
    idx = next((i for i, w in enumerate(words) if w is word), None)
    if idx is None:
        return word.text.rstrip(":")
    if direction == "whole":
        parts = [w.text for w in words]
    else:
        start = idx
        while start > 0:
            prev = words[start - 1]
            if words[start].x0 - prev.x1 > 18 or _looks_like_value(prev.text):
                break
            start -= 1
        parts = [w.text for w in words[start:idx + 1]]
    return re.sub(r"\s+", " ", " ".join(parts)).strip().rstrip(":").strip()


_VALUE_LIKE = re.compile(
    r"^[\d.,:/\-₹$€£]+$|^\d{1,4}[/-]\d{1,4}([/-]\d{1,4})?$", re.IGNORECASE)


def _looks_like_value(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if _VALUE_LIKE.match(t):
        return True
    digits = sum(c.isdigit() for c in t)
    return digits > len(t) * 0.5


def generalise_pattern(samples: list[str]) -> str:
    """Infer a conservative regex from one or more observed values."""
    values = [s.strip() for s in samples if s and s.strip()]
    if not values:
        return ".+"

    def tokenise(s: str):
        out, i = [], 0
        while i < len(s):
            ch = s[i]
            if ch.isdigit():
                j = i
                while j < len(s) and s[j].isdigit():
                    j += 1
                out.append(("d", j - i))
                i = j
            elif ch.isalpha():
                j = i
                while j < len(s) and s[j].isalpha():
                    j += 1
                out.append(("a", j - i, s[i:j].isupper()))
                i = j
            else:
                out.append(("lit", ch))
                i += 1
        return out

    tokenised = [tokenise(v) for v in values]
    shapes = {tuple(t[:2] if t[0] != "lit" else t for t in tk) for tk in tokenised}
    if len(shapes) > 1 or len({len(t) for t in tokenised}) > 1:
        return ".+"

    parts = []
    for pos, tok in enumerate(tokenised[0]):
        if tok[0] == "lit":
            parts.append(re.escape(tok[1]))
        elif tok[0] == "d":
            lengths = {tk[pos][1] for tk in tokenised}
            lo, hi = min(lengths), max(lengths)
            parts.append(rf"\d{{{lo}}}" if lo == hi else rf"\d{{{lo},{hi}}}")
        else:
            lengths = {tk[pos][1] for tk in tokenised}
            upper = all(tk[pos][2] for tk in tokenised)
            cls = "[A-Z]" if upper else "[A-Za-z]"
            lo, hi = min(lengths), max(lengths)
            parts.append(f"{cls}{{{lo}}}" if lo == hi else f"{cls}{{{lo},{hi}}}")
    return "^" + "".join(parts) + "$"
