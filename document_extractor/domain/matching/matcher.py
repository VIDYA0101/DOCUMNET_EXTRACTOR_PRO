"""Template matching.

Two stages, because scoring 100 templates by running full extraction on 1,000
documents is 100,000 extraction passes.

  1. Candidate generation - inverted index over identifiers and distinctive
     tokens. O(1) dict lookups.
  2. Verification - weighted score over five signals, plus a margin check.

The margin check matters as much as the absolute threshold: two vendors using
the same accounting package produce near-identical invoices, and 0.91 vs 0.90
is a coin flip that must not be resolved silently.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from ..models import PdfDocument, Reason
from ..template.schema import Template

WEIGHTS = {
    "identifier": 0.40,
    "required_tokens": 0.20,
    "anchor_resolution": 0.25,
    "text_profile": 0.10,
    "layout": 0.05,
}

_STOPWORDS = {
    "the", "and", "for", "you", "our", "your", "with", "this", "that", "from",
    "all", "any", "are", "was", "will", "have", "has", "not", "per", "pvt",
    "ltd", "inc", "llp", "co", "of", "to", "in", "on", "at", "by", "no", "or",
}
_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


@dataclass
class MatchResult:
    template_id: str = ""
    template: Template | None = None
    score: float = 0.0
    margin: float = 0.0
    runner_up: str = ""
    accepted: bool = False
    reasons: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)


def page_tokens(doc: PdfDocument, pages: int = 1) -> set[str]:
    text = " ".join(p.text for p in doc.pages[:pages])
    return {t.lower() for t in _TOKEN_RE.findall(text)
            if t.lower() not in _STOPWORDS}


def term_frequencies(doc: PdfDocument, pages: int = 1) -> dict[str, float]:
    text = " ".join(p.text for p in doc.pages[:pages])
    counts = Counter(t.lower() for t in _TOKEN_RE.findall(text)
                     if t.lower() not in _STOPWORDS)
    total = sum(counts.values()) or 1
    return {k: v / total for k, v in counts.most_common(60)}


def layout_signature(doc: PdfDocument, grid=(8, 8)) -> list[float]:
    """Coarse ink-density grid over page 1. A weak signal; a useful tiebreaker."""
    if not doc.pages:
        return []
    page = doc.pages[0]
    rows, cols = grid
    cells = [0.0] * (rows * cols)
    if not page.width or not page.height:
        return cells
    for w in page.words:
        r = min(rows - 1, max(0, int(w.cy / page.height * rows)))
        c = min(cols - 1, max(0, int(w.cx / page.width * cols)))
        cells[r * cols + c] += (w.x1 - w.x0) * (w.y1 - w.y0)
    total = sum(cells) or 1.0
    return [round(v / total, 5) for v in cells]


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    num = sum(a[k] * b[k] for k in shared)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da and db else 0.0


def _l2(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dist = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    return max(0.0, 1.0 - dist * 2.0)


class TemplateMatcher:
    def __init__(self, templates: list[Template], settings: dict | None = None):
        self.templates = list(templates)
        s = settings or {}
        self.min_score = float(s.get("template_min_score", 0.72))
        self.review_below = float(s.get("template_review_below", 0.85))
        self.min_margin = float(s.get("template_margin", 0.10))
        self.identifier_index: dict[str, list[str]] = {}
        self.token_index: dict[str, set[str]] = {}
        self._by_id = {t.template_id: t for t in self.templates}
        self._build_index()

    def _build_index(self) -> None:
        for tpl in self.templates:
            ids = tpl.vendor.identifiers
            for value in (ids.gstin, ids.pan, ids.vat, ids.cin):
                if value:
                    key = re.sub(r"\s", "", str(value)).upper()
                    self.identifier_index.setdefault(key, []).append(tpl.template_id)
            for token in tpl.fingerprint.required_tokens:
                for t in _TOKEN_RE.findall(token.lower()):
                    if t not in _STOPWORDS:
                        self.token_index.setdefault(t, set()).add(tpl.template_id)
            for alias in [tpl.vendor.display_name, *tpl.vendor.aliases]:
                for t in _TOKEN_RE.findall((alias or "").lower()):
                    if t not in _STOPWORDS:
                        self.token_index.setdefault(t, set()).add(tpl.template_id)

    # -- stage 1 -----------------------------------------------------------
    def candidates(self, doc: PdfDocument, top_k: int = 10) -> list[Template]:
        text_upper = re.sub(r"\s", "", doc.text.upper())
        hits: Counter[str] = Counter()

        for identifier, template_ids in self.identifier_index.items():
            if identifier and identifier in text_upper:
                for tid in template_ids:
                    hits[tid] += 10          # identifiers are near-decisive

        tokens = page_tokens(doc, pages=2)
        for token in tokens:
            for tid in self.token_index.get(token, ()):
                hits[tid] += 1

        if not hits:
            return list(self.templates)[:top_k]
        ordered = [self._by_id[tid] for tid, _ in hits.most_common(top_k)
                   if tid in self._by_id]
        return ordered

    # -- stage 2 -----------------------------------------------------------
    def score(self, doc: PdfDocument, tpl: Template) -> tuple[float, dict]:
        fp = tpl.fingerprint
        text_norm = " ".join(doc.text.split()).lower()
        text_compact = re.sub(r"\s", "", doc.text.upper())
        breakdown: dict[str, float] = {}

        # Forbidden tokens are a hard disqualifier: this is how a PROFORMA
        # INVOICE is kept off the TAX INVOICE template.
        for token in fp.forbidden_tokens:
            if token and token.lower() in text_norm:
                return 0.0, {"disqualified_by": token}

        ids = tpl.vendor.identifiers
        identifier_values = [v for v in (ids.gstin, ids.pan, ids.vat, ids.cin) if v]
        if identifier_values:
            found = sum(1 for v in identifier_values
                        if re.sub(r"\s", "", str(v)).upper() in text_compact)
            breakdown["identifier"] = found / len(identifier_values)
        else:
            breakdown["identifier"] = 0.0

        required = fp.required_tokens
        if required:
            found = sum(1 for t in required if t and t.lower() in text_norm)
            breakdown["required_tokens"] = found / len(required)
            if found < len(required):
                breakdown["required_tokens"] *= 0.5   # all should be present
        else:
            breakdown["required_tokens"] = 0.5

        breakdown["anchor_resolution"] = self._anchor_rate(doc, tpl)

        if fp.text_profile.top_terms:
            breakdown["text_profile"] = _cosine(term_frequencies(doc),
                                                fp.text_profile.top_terms)
        else:
            breakdown["text_profile"] = 0.5

        if fp.layout_signature.vector:
            breakdown["layout"] = _l2(layout_signature(doc),
                                      fp.layout_signature.vector)
        else:
            breakdown["layout"] = 0.5

        total = sum(WEIGHTS[k] * breakdown.get(k, 0.0) for k in WEIGHTS)
        return round(total, 4), breakdown

    def _anchor_rate(self, doc: PdfDocument, tpl: Template) -> float:
        """What fraction of this template's anchors actually resolve here?

        The strongest behavioural signal: it tests the template against the
        document rather than comparing surface statistics.
        """
        anchors: list[str] = []
        for f in tpl.fields:
            for s in f.strategies:
                if s.type == "anchor_relative" and s.anchor and s.anchor.text:
                    anchors.append(s.anchor.text)
                    break
        for t in tpl.tables:
            anchors.extend(t.start.header_tokens[:3])
        if not anchors:
            return 0.5
        text_norm = re.sub(r"[^a-z0-9 ]+", " ", doc.text.lower())
        text_norm = re.sub(r"\s+", " ", text_norm)
        found = sum(1 for a in anchors
                    if re.sub(r"[^a-z0-9 ]+", " ", a.lower()).strip() in text_norm)
        return found / len(anchors)

    # -- decision ----------------------------------------------------------
    def match(self, doc: PdfDocument) -> MatchResult:
        scored: list[tuple[float, Template, dict]] = []
        for tpl in self.candidates(doc):
            value, breakdown = self.score(doc, tpl)
            scored.append((value, tpl, breakdown))
        scored.sort(key=lambda x: -x[0])

        if not scored or scored[0][0] <= 0.0:
            return MatchResult(reasons=[Reason.TEMPLATE_NO_MATCH.value])

        best_score, best_tpl, breakdown = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        margin = round(best_score - second, 4)

        result = MatchResult(
            template_id=best_tpl.template_id, template=best_tpl,
            score=best_score, margin=margin,
            runner_up=scored[1][1].template_id if len(scored) > 1 else "",
            breakdown=breakdown)

        min_score = max(self.min_score, best_tpl.fingerprint.min_score)
        review_below = max(self.review_below, best_tpl.fingerprint.review_below)

        if best_score < min_score:
            result.reasons.append(Reason.TEMPLATE_LOW_SCORE.value)
            result.accepted = False
        elif len(scored) > 1 and margin < self.min_margin:
            result.reasons.append(Reason.TEMPLATE_MARGIN_LOW.value)
            result.accepted = False
        else:
            result.accepted = True
            if best_score < review_below:
                result.reasons.append(Reason.TEMPLATE_LOW_SCORE.value)
        return result


def build_fingerprint(doc: PdfDocument, vendor_name: str = "",
                      identifiers: dict | None = None) -> dict:
    """Derive a fingerprint from a sample document, for the template editor."""
    tf = term_frequencies(doc)
    top = dict(sorted(tf.items(), key=lambda kv: -kv[1])[:20])
    required: list[str] = []
    if vendor_name:
        required.append(vendor_name)
    head = doc.pages[0].text.upper() if doc.pages else ""
    for phrase in ("TAX INVOICE", "INVOICE", "PURCHASE ORDER", "DELIVERY NOTE",
                   "DELIVERY CHALLAN", "STATEMENT OF ACCOUNT", "CREDIT NOTE"):
        if phrase in head:
            required.append(phrase)
            break
    return {
        "required_tokens": required,
        "forbidden_tokens": [],
        "identifier_regexes": [],
        "layout_signature": {"grid": [8, 8], "vector": layout_signature(doc),
                             "weight": 0.05},
        "text_profile": {"top_terms": top, "weight": 0.10},
        "min_score": 0.72,
        "review_below": 0.85,
    }
