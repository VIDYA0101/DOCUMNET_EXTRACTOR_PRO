"""Whole-document field discovery.

Different from the rest of the extraction engine on purpose: everything else
in this package is built around a *template* someone has already defined -
named fields, specific anchors, typed values. This module runs with no
template at all. It walks every line on every page and pulls out every
label:value pair it can find, so a new document can be looked at and asked
"what's actually in here?" before anyone commits to a template for it.

Values returned here are the literal text found next to a label - never
parsed into a number or date, never computed, never combined with anything
else. What you see is what is printed on the page, verbatim.

Two problems have to be solved together to make this reliable on a real,
dense form:

  1. Two unrelated fields can sit at the same height on the page ("Ship
     To:" on the left, "Date:" on the right). Grouping by y-position alone
     - correct for normal reading order, which is what the rest of this
     codebase uses lines for - merges them into one garbled string.
  2. Multiple fields can also be packed onto one line with no big gap at
     all ("Total Taxable amount: 71.00 INR Total GST amount: 13.00 INR"),
     which a large gap threshold alone would fail to separate.

Fixing (1) with a wide geometric gap threshold and (2) with a word-level
scan that finds each label's own boundary - stopping at values, currency
codes, and parenthetical asides rather than just following gaps - handles
both without one undoing the other. Every number in this file (the gap
threshold, the currency list) was measured against a real invoice, not
picked arbitrarily; see the project's design notes for how.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import PdfDocument

_MAX_LABEL_LEN = 45
_MIN_LABEL_LEN = 2

# Measured against a real multi-column invoice: genuine column breaks were
# 115-160pt; a label reaching its own value across deliberate form spacing
# was 28-84pt. 100pt sits cleanly between the two in every case observed.
_COLUMN_GAP_PT = 100.0

_CURRENCY_CODES = {"INR", "USD", "EUR", "GBP", "AED", "SAR", "JPY", "CNY",
                    "AUD", "CAD", "SGD", "CHF", "RS"}
# Words that legitimately join a multi-word label ("Ship To / Bill To") and
# must not be mistaken for the start of a value just because they are not
# alphabetic.
_CONNECTORS = {"/", "&", "-", "and", "of", "no.", "no"}


def _segment_by_columns(line) -> list[list]:
    """Split a line's words into column groups by horizontal gap."""
    words = sorted(line.words, key=lambda w: w.x0)
    if not words:
        return []
    groups: list[list] = [[words[0]]]
    for w in words[1:]:
        if w.x0 - groups[-1][-1].x1 > _COLUMN_GAP_PT:
            groups.append([w])
        else:
            groups[-1].append(w)
    return groups


def _looks_like_label_word(word: str) -> bool:
    """Could this single word plausibly be part of a label (walking
    backward from a colon), as opposed to trailing value content?

    A short all-caps word is genuinely ambiguous on an invoice - "GST" is a
    label word, "INR" is a value word, and neither is distinguishable from
    the other by shape alone. The currency list exists because of that
    ambiguity, not as a general-purpose stopword list.
    """
    t = word.rstrip(":")
    if not t:
        return False
    if t.lower() in _CONNECTORS:
        return True
    if "(" in t or ")" in t:          # a parenthetical aside is value content
        return False
    if t.upper() in _CURRENCY_CODES:
        return False
    if re.fullmatch(r"[\d.,/%-]+", t):   # numeric, date-shaped, a ratio
        return False
    letters = sum(c.isalpha() for c in t)
    return letters >= max(1, len(t) * 0.5)


def _label_span_start(words: list[str], colon_index: int, floor: int) -> int:
    """Walk backward from a colon to find where its label begins, without
    crossing into the previous field's value (bounded by `floor`)."""
    start = colon_index
    j = colon_index - 1
    count = 0
    while j > floor and count < 6 and _looks_like_label_word(words[j]):
        start = j
        j -= 1
        count += 1
    return start


def _split_segment(words: list) -> list[tuple[str, str, tuple]]:
    """Find every label:value pair within one column segment."""
    texts = [w.text for w in words]
    colon_idx = [i for i, t in enumerate(texts) if t.endswith(":")]
    if not colon_idx:
        return []

    out: list[tuple[str, str, tuple]] = []
    prev_colon_end = -1
    for k, ci in enumerate(colon_idx):
        start = _label_span_start(texts, ci, prev_colon_end)
        label = " ".join(texts[start:ci + 1]).rstrip(":").strip()

        value_end = len(texts)
        if k + 1 < len(colon_idx):
            value_end = _label_span_start(texts, colon_idx[k + 1], ci)

        value = " ".join(texts[ci + 1:value_end]).strip()
        if label and (_MIN_LABEL_LEN <= len(label) <= _MAX_LABEL_LEN):
            label_words = words[start:ci + 1]
            value_words = words[ci + 1:value_end]
            # The bbox represents where the VALUE is - not the label. A box
            # spanning both looks harmless for a static highlight, but is
            # wrong the moment anything downstream treats the box as "this
            # is exactly what gets extracted" (an editor highlight a user
            # can drag to correct, for instance) - it would include the
            # label text in every correction. If value_words is somehow
            # empty, fall back to the label's own box rather than crash.
            bbox_words = value_words or label_words
            bbox = (min(w.x0 for w in bbox_words), min(w.y0 for w in bbox_words),
                    max(w.x1 for w in bbox_words), max(w.y1 for w in bbox_words))
            out.append((label, value, bbox))
        prev_colon_end = ci
    return out


@dataclass(slots=True)
class DiscoveredField:
    label: str
    value: str          # exact text as printed - never parsed or computed
    page: int
    occurrence: int      # 1st, 2nd, 3rd... time this exact label appears
    bbox: tuple[float, float, float, float] | None = None

    @property
    def display_label(self) -> str:
        """The label, disambiguated with a count if it repeats (e.g. a
        'Supplier:' field that appears once per line item)."""
        return self.label if self.occurrence == 1 else f"{self.label} #{self.occurrence}"

    @property
    def suggested_field_name(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_")
        if self.occurrence > 1:
            slug = f"{slug}_{self.occurrence}"
        return slug or "field"


def discover_fields(doc: PdfDocument, *, max_fields: int = 300) -> list[DiscoveredField]:
    """Find every label:value pair in a document, verbatim, no template needed."""
    seen_labels: dict[str, int] = {}
    out: list[DiscoveredField] = []

    for page in doc.pages:
        for line in page.lines:
            for segment in _segment_by_columns(line):
                for label, value, bbox in _split_segment(segment):
                    if not value:
                        continue
                    seen_labels[label] = seen_labels.get(label, 0) + 1
                    out.append(DiscoveredField(
                        label=label, value=value, page=page.number,
                        occurrence=seen_labels[label], bbox=bbox))
                    if len(out) >= max_fields:
                        return out
    return out


def to_json_rows(fields: list[DiscoveredField]) -> list[dict]:
    """Plain-dict form, for handing to a UI, a CSV writer, or json.dump."""
    return [
        {"field_name": f.suggested_field_name, "label": f.display_label,
         "value": f.value, "page": f.page, "bbox": list(f.bbox) if f.bbox else None}
        for f in fields
    ]
