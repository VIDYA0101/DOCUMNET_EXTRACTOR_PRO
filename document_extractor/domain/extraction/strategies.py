"""Extraction strategies.

Every strategy returns candidates only. It never returns a best guess: if the
anchor is absent the answer is "no candidate", and if two values match the
answer is "ambiguous". Resolving either by picking the first is how wrong
invoice numbers reach a payment run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..models import Candidate, Page, PdfDocument
from . import anchors as A

log = logging.getLogger(__name__)

MAX_PATTERN_LEN = 500
_REGEX_CACHE: dict[tuple[str, int], re.Pattern | None] = {}


def compile_pattern(pattern: str, ignore_case: bool = False) -> re.Pattern | None:
    """Compile with guards. A template can arrive by email; treat it as input."""
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN_LEN:
        log.warning("pattern rejected: too long")
        return None
    if re.search(r"\([^)]*[+*]\)[+*]", pattern):
        # Nested unbounded quantifiers: catastrophic backtracking risk.
        log.warning("pattern rejected: nested unbounded quantifiers")
        return None
    key = (pattern, int(ignore_case))
    if key not in _REGEX_CACHE:
        try:
            _REGEX_CACHE[key] = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            log.warning("bad pattern %r: %s", pattern, exc)
            _REGEX_CACHE[key] = None
    return _REGEX_CACHE[key]


@dataclass(slots=True)
class StrategyOutcome:
    candidates: list[Candidate]
    reason: str = ""

    @property
    def unique(self) -> Candidate | None:
        return self.candidates[0] if len(self.candidates) == 1 else None


def _pages_for(doc: PdfDocument, spec: dict) -> list[Page]:
    page_spec = spec.get("page", "any")
    if page_spec in (None, "any", 0):
        return doc.pages
    if page_spec == "last":
        return doc.pages[-1:]
    if isinstance(page_spec, int):
        return [p for p in doc.pages if p.number == page_spec]
    if isinstance(page_spec, list):
        return [p for p in doc.pages if p.number in page_spec]
    return doc.pages


def _apply_pattern(text: str, spec: dict) -> list[str]:
    """Return every distinct substring matching the pattern, or the whole text."""
    pattern = spec.get("pattern")
    if not pattern:
        return [text] if text else []
    rx = compile_pattern(pattern, spec.get("ignore_case", False))
    if rx is None:
        return [text] if text else []
    if rx.pattern.startswith("^") and rx.pattern.endswith("$"):
        return [text] if rx.fullmatch(text.strip()) else []
    found = [m.group(1) if rx.groups else m.group(0) for m in rx.finditer(text)]
    seen, out = set(), []
    for f in found:
        f = f.strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _post(text: str, steps: list[str] | None) -> str:
    out = text
    for step in steps or []:
        s = step.lower()
        if s == "strip":
            out = out.strip()
        elif s == "collapse_ws":
            out = re.sub(r"\s+", " ", out).strip()
        elif s == "upper":
            out = out.upper()
        elif s == "lower":
            out = out.lower()
        elif s == "digits_only":
            out = re.sub(r"\D", "", out)
        elif s == "alnum_only":
            out = re.sub(r"[^A-Za-z0-9]", "", out)
        elif s == "strip_currency":
            out = re.sub(r"(₹|Rs\.?|INR|\$|€|£)\s*", "", out, flags=re.IGNORECASE)
        elif s == "remove_spaces":
            out = out.replace(" ", "")
        elif s.startswith("strip_prefix:"):
            prefix = step.split(":", 1)[1]
            if out.lower().startswith(prefix.lower()):
                out = out[len(prefix):].strip()
    return out.strip()


# ---------------------------------------------------------------- strategies
def anchor_relative(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    anchor_spec = spec.get("anchor") or {}
    anchor_text = anchor_spec.get("text", "")
    if not anchor_text:
        return StrategyOutcome([], "ANCHOR_NOT_FOUND")

    window = spec.get("value_window") or {}
    occurrence = (anchor_spec.get("occurrence") or "first").lower()
    scope = anchor_spec.get("search_scope") or {}
    region = scope.get("region_norm")

    pages = _pages_for(doc, {"page": scope.get("page", spec.get("page", "any"))})
    candidates: list[Candidate] = []
    any_anchor = False

    for page in pages:
        hits = A.find_anchors(
            page, anchor_text,
            mode=anchor_spec.get("match", "fuzzy"),
            min_ratio=float(anchor_spec.get("min_ratio", 0.85)) * 100
            if float(anchor_spec.get("min_ratio", 0.85)) <= 1 else
            float(anchor_spec.get("min_ratio", 85)),
            region_norm=region)
        if not hits:
            continue
        any_anchor = True
        if occurrence == "first":
            hits = hits[:1]
        elif occurrence == "last":
            hits = hits[-1:]

        for hit in hits:
            bbox = A.value_window(
                hit, page,
                direction=window.get("direction", "right"),
                max_distance=float(window.get("max_distance_pt", 220)),
                vertical_tolerance=float(window.get("vertical_tolerance_pt", 6)),
                max_below=float(window.get("max_below_pt", 40)))
            text, vbox, words = A.text_in_window(
                page, bbox, stop_at=window.get("stop_at_anchor"))
            if not text:
                continue
            for value in _apply_pattern(text, spec):
                conf = min((w.conf for w in words), default=1.0)
                candidates.append(Candidate(
                    text=_post(value, spec.get("post")), bbox=vbox or bbox,
                    page=page.number, conf=conf, strategy="anchor_relative"))

    candidates = _dedupe(candidates)
    if not candidates:
        return StrategyOutcome([], "NO_CANDIDATE" if any_anchor else "ANCHOR_NOT_FOUND")
    return StrategyOutcome(candidates)


def regex_page(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    """Position-independent match over page text. For globally unique IDs."""
    rx = compile_pattern(spec.get("pattern", ""), spec.get("ignore_case", False))
    if rx is None:
        return StrategyOutcome([], "NO_CANDIDATE")

    candidates: list[Candidate] = []
    for page in _pages_for(doc, spec):
        for line in page.lines:
            for m in rx.finditer(line.text):
                value = m.group(1) if rx.groups else m.group(0)
                value = _post(value.strip(), spec.get("post"))
                if not value:
                    continue
                bbox = line.bbox
                for w in line.words:
                    if value.replace(" ", "") in w.text.replace(" ", ""):
                        bbox = w.bbox
                        break
                candidates.append(Candidate(
                    text=value, bbox=bbox, page=page.number,
                    conf=min((w.conf for w in line.words), default=1.0),
                    strategy="regex_page"))
    candidates = _dedupe(candidates)
    return StrategyOutcome(candidates, "" if candidates else "NO_CANDIDATE")


def region_absolute(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    """Fixed normalised box. Fallback only - breaks when the layout shifts."""
    bbox_norm = spec.get("bbox_norm")
    if not bbox_norm or len(bbox_norm) != 4:
        return StrategyOutcome([], "NO_CANDIDATE")
    tol = float(spec.get("tolerance_pt", 8))
    candidates: list[Candidate] = []

    for page in _pages_for(doc, spec):
        bbox = page.to_abs(bbox_norm)
        padded = (bbox[0] - tol, bbox[1] - tol, bbox[2] + tol, bbox[3] + tol)
        text, vbox, words = A.text_in_window(page, padded)
        if not text:
            continue
        for value in _apply_pattern(text, spec):
            candidates.append(Candidate(
                text=_post(value, spec.get("post")), bbox=vbox or padded,
                page=page.number, conf=min((w.conf for w in words), default=1.0),
                strategy="region_absolute"))
    candidates = _dedupe(candidates)
    return StrategyOutcome(candidates, "" if candidates else "NO_CANDIDATE")


def label_pair(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    """Split a 'Label: Value' line. Common in two-column header blocks."""
    label = spec.get("label", "")
    if not label:
        return StrategyOutcome([], "NO_CANDIDATE")
    from rapidfuzz import fuzz
    norm_label = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    candidates: list[Candidate] = []

    for page in _pages_for(doc, spec):
        for line in page.lines:
            if ":" not in line.text:
                continue
            left, _, right = line.text.partition(":")
            ln = re.sub(r"[^a-z0-9]+", " ", left.lower()).strip()
            if not ln or fuzz.ratio(ln, norm_label) < float(spec.get("min_ratio", 88)):
                continue
            for value in _apply_pattern(right.strip(), spec):
                candidates.append(Candidate(
                    text=_post(value, spec.get("post")), bbox=line.bbox,
                    page=page.number,
                    conf=min((w.conf for w in line.words), default=1.0),
                    strategy="label_pair"))
    candidates = _dedupe(candidates)
    return StrategyOutcome(candidates, "" if candidates else "NO_CANDIDATE")


def constant(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    """A value fixed by the template. Zero extraction risk."""
    value = spec.get("value")
    if value is None:
        return StrategyOutcome([], "NO_CANDIDATE")
    return StrategyOutcome([Candidate(text=str(value), bbox=(0, 0, 0, 0),
                                      page=0, conf=1.0, strategy="constant")])


REGISTRY = {
    "anchor_relative": anchor_relative,
    "regex_page": regex_page,
    "region_absolute": region_absolute,
    "label_pair": label_pair,
    "constant": constant,
}


def run_strategy(doc: PdfDocument, spec: dict) -> StrategyOutcome:
    fn = REGISTRY.get((spec.get("type") or "").lower())
    if fn is None:
        return StrategyOutcome([], "NO_CANDIDATE")
    try:
        return fn(doc, spec)
    except Exception as exc:
        log.warning("strategy %s raised: %s", spec.get("type"), exc, exc_info=True)
        return StrategyOutcome([], "EXCEPTION")


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in candidates:
        key = c.text.strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out
