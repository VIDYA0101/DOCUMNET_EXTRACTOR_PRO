"""Line-item table extraction.

The hardest part of the system. Naive "cluster words by y" fails on wrapped
descriptions, interleaved tax rows, and page breaks, so rows are anchored on a
designated column and everything else appends to the row above.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from rapidfuzz import fuzz

from ..models import LineItem, Page, PdfDocument, Reason, Status, Word
from .types import parse_by_type

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def find_header(page: Page, tokens: list[str], min_matched: int = 3,
                fuzzy: bool = True) -> tuple[object, float] | None:
    """Locate the table header row by fuzzy-matching its column labels."""
    targets = [_norm(t) for t in tokens if t]
    if not targets:
        return None
    best, best_score = None, 0.0

    for line in page.lines:
        words = [_norm(w.text) for w in line.words]
        if not words:
            continue
        matched = 0
        for target in targets:
            for w in words:
                if not w:
                    continue
                score = fuzz.ratio(w, target) if fuzzy else (100 if w == target else 0)
                if score >= 85 or target in w or w in target:
                    matched += 1
                    break
        if matched >= min_matched and matched > best_score:
            best, best_score = line, matched
    if best is None:
        return None
    return best, best_score / max(len(targets), 1)


def columns_from_rulings(page: Page, y0: float, y1: float,
                         min_count: int = 3) -> list[float] | None:
    """Derive column boundaries from vertical vector rulings. Most reliable."""
    xs = sorted({round(r.x0, 1) for r in page.rulings
                 if r.vertical and r.y0 <= y1 + 5 and r.y1 >= y0 - 5})
    merged: list[float] = []
    for x in xs:
        if not merged or x - merged[-1] > 4.0:
            merged.append(x)
    return merged if len(merged) >= min_count + 1 else None


def columns_from_header(header_line, page: Page) -> list[tuple[str, float, float]]:
    """Fall back to header token positions, extended to the midpoints between them."""
    words = sorted(header_line.words, key=lambda w: w.x0)
    if not words:
        return []
    groups: list[list[Word]] = [[words[0]]]
    for w in words[1:]:
        if w.x0 - groups[-1][-1].x1 <= 12:
            groups[-1].append(w)
        else:
            groups.append([w])

    cols: list[tuple[str, float, float]] = []
    for i, g in enumerate(groups):
        left = 0.0 if i == 0 else (groups[i - 1][-1].x1 + g[0].x0) / 2.0
        right = page.width if i == len(groups) - 1 else \
            (g[-1].x1 + groups[i + 1][0].x0) / 2.0
        cols.append((" ".join(w.text for w in g), left, right))
    return cols


def _end_y(page: Page, header_bottom: float, end_spec: dict) -> float:
    """Where the table stops on this page."""
    stop_y = page.height
    conditions = end_spec.get("conditions") or []
    stop_tokens = []
    for c in conditions:
        if c.get("type") == "anchor":
            stop_tokens += [_norm(t) for t in c.get("text", [])]
    if stop_tokens:
        for line in page.lines:
            if line.y0 <= header_bottom + 2:
                continue
            first = _norm(line.text.split(":")[0])
            for tok in stop_tokens:
                if not tok:
                    continue
                if first.startswith(tok) or fuzz.ratio(first[:len(tok) + 4], tok) >= 90:
                    return min(stop_y, line.y0 - 1)
    return stop_y


def _assign(words: list[Word], cols: list[tuple[str, float, float]]) -> dict[str, list[Word]]:
    out: dict[str, list[Word]] = {name: [] for name, _, _ in cols}
    for w in words:
        for name, left, right in cols:
            if left <= w.cx < right:
                out[name].append(w)
                break
    return out


def extract_table(doc: PdfDocument, spec: dict, *, date_formats=None,
                  dayfirst: bool = True) -> tuple[list[LineItem], list[str]]:
    """Extract one table definition across all pages it spans."""
    reasons: list[str] = []
    col_specs = spec.get("columns") or []
    if not col_specs:
        return [], [Reason.EMPTY_TABLE.value]

    start = spec.get("start") or {}
    end = spec.get("end") or {}
    grouping = spec.get("row_grouping") or {}
    multipage = spec.get("multipage") or {}
    filters = spec.get("row_filters") or {}

    anchor_cols = grouping.get("anchor_columns") or [col_specs[0]["name"]]
    y_tol = float(grouping.get("y_tolerance_pt", 3))
    wrapped = grouping.get("wrapped_lines", "append_to_previous")
    multiline_cols = {c["name"] for c in col_specs if c.get("multiline")}
    drop_patterns = [re.compile(p, re.IGNORECASE)
                     for p in (filters.get("drop_if_matches") or [])]

    items: list[LineItem] = []
    header_found_any = False
    pending: LineItem | None = None

    for page in doc.pages:
        found = find_header(page, start.get("header_tokens", []),
                            int(start.get("min_tokens_matched", 3)),
                            (start.get("match", "fuzzy") == "fuzzy"))
        if found is None:
            if not (header_found_any and multipage.get("continues")):
                continue
            header_line, header_bottom = None, 0.0
        else:
            header_line, _ = found
            header_bottom = header_line.y1
            header_found_any = True

        # Column boundaries: rulings first, then header positions, then template.
        cols: list[tuple[str, float, float]] = []
        y_end = _end_y(page, header_bottom, end)
        if header_line is not None:
            edges = columns_from_rulings(page, header_bottom, y_end,
                                         min_count=max(2, len(col_specs) - 2))
            if edges and len(edges) - 1 >= len(col_specs) - 1:
                for i, c in enumerate(col_specs):
                    if i + 1 < len(edges):
                        cols.append((c["name"], edges[i], edges[i + 1]))
                if len(cols) < len(col_specs):
                    cols = []
            if not cols:
                header_cols = columns_from_header(header_line, page)
                if len(header_cols) == len(col_specs):
                    cols = [(col_specs[i]["name"], header_cols[i][1], header_cols[i][2])
                            for i in range(len(col_specs))]
        if not cols:
            for c in col_specs:
                rng = c.get("x_range_norm")
                if not rng:
                    cols = []
                    break
                cols.append((c["name"], rng[0] * page.width, rng[1] * page.width))
        if not cols:
            reasons.append(Reason.NO_TABLE_HEADER.value)
            continue

        body = [w for w in page.words
                if header_bottom + 1 < w.cy < y_end]
        if not body:
            continue

        # Group into visual rows, then merge wrapped lines into their anchor row.
        rows: list[list[Word]] = []
        for w in sorted(body, key=lambda w: (round(w.cy, 1), w.x0)):
            if rows and abs(w.cy - rows[-1][0].cy) <= max(y_tol, w.height * 0.6):
                rows[-1].append(w)
            else:
                rows.append([w])

        for row_words in rows:
            assigned = _assign(row_words, cols)
            raw = {name: re.sub(r"\s+", " ",
                                " ".join(w.text for w in sorted(ws, key=lambda x: x.x0)))
                   .strip()
                   for name, ws in assigned.items()}
            joined = " ".join(v for v in raw.values() if v).strip()
            if not joined:
                continue
            if any(p.search(joined) for p in drop_patterns):
                continue

            has_anchor = any(raw.get(a) for a in anchor_cols)
            if not has_anchor and pending is not None and wrapped == "append_to_previous":
                for name in multiline_cols:
                    if raw.get(name):
                        pending.raw[name] = (pending.raw.get(name, "") + " "
                                             + raw[name]).strip()
                        pending.cells[name] = pending.raw[name]
                continue
            if not has_anchor:
                continue

            item = LineItem(index=len(items) + 1, raw=raw, page=page.number,
                            bbox=(min(w.x0 for w in row_words),
                                  min(w.y0 for w in row_words),
                                  max(w.x1 for w in row_words),
                                  max(w.y1 for w in row_words)),
                            confidence=min((w.conf for w in row_words), default=1.0))
            items.append(item)
            pending = item

    if not items:
        return [], reasons or [Reason.EMPTY_TABLE.value]

    # Type each cell, then validate the row's own arithmetic.
    col_by_name = {c["name"]: c for c in col_specs}
    for item in items:
        for name, rawval in item.raw.items():
            cs = col_by_name.get(name, {})
            res = parse_by_type(rawval, cs.get("type", "string"),
                                formats=date_formats, dayfirst=dayfirst)
            if res.ok:
                item.cells[name] = res.value
            else:
                item.cells[name] = None
                if cs.get("required") and rawval:
                    item.status = Status.INVALID
                    item.reasons.append(Reason.TYPE_PARSE_FAILED.value)
        for cs in col_specs:
            if cs.get("required") and not item.raw.get(cs["name"]):
                item.status = Status.INVALID
                item.reasons.append(f"MISSING_COLUMN_{cs['name'].upper()}")

        _row_math(item, spec)

    return items, reasons


def _row_math(item: LineItem, spec: dict) -> None:
    """quantity x unit_price should equal amount. Catches OCR digit errors."""
    validation = spec.get("validation") or {}
    if not validation.get("row_check_enabled", True):
        return
    q = item.cells.get("quantity")
    p = item.cells.get("unit_price")
    a = item.cells.get("amount")
    if q is None or p is None or a is None:
        return
    try:
        expected = Decimal(str(q)) * Decimal(str(p))
        tol = max(Decimal("1.0"), abs(Decimal(str(a))) * Decimal("0.01"))
        if abs(expected - Decimal(str(a))) > tol:
            item.status = Status.LOW_CONFIDENCE
            item.reasons.append(Reason.ROW_MATH_MISMATCH.value)
            item.confidence = min(item.confidence, 0.5)
    except Exception:
        pass
