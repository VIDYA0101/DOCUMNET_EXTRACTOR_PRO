"""Canonical domain model.

Everything downstream of PDF parsing sees only these types. The text path and
the OCR path both produce a Page; nothing else knows which one ran.

Coordinates: points (1/72"), top-left origin, page-relative. Normalised by
pdf/text_layer at the boundary so A4 and Letter templates interoperate.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    EXTRACTED = "EXTRACTED"            # found, validated, above floor
    LOW_CONFIDENCE = "LOW_CONFIDENCE"  # found, below floor -> export flagged
    AMBIGUOUS = "AMBIGUOUS"            # >1 candidate -> no value exported
    MISSING = "MISSING"                # optional field, not found
    NOT_FOUND = "NOT_FOUND"            # required field, not found
    INVALID = "INVALID"                # found but failed a validator
    FAILED = "FAILED"                  # extraction raised


class DocStatus(str, Enum):
    OK = "OK"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class Reason(str, Enum):
    ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
    NO_CANDIDATE = "NO_CANDIDATE"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    REGEX_MISMATCH = "REGEX_MISMATCH"
    TYPE_PARSE_FAILED = "TYPE_PARSE_FAILED"
    RANGE_VIOLATION = "RANGE_VIOLATION"
    CHECKSUM_FAILED = "CHECKSUM_FAILED"
    GSTIN_CHECKSUM_FAIL = "GSTIN_CHECKSUM_FAIL"
    OCR_CONF_LOW = "OCR_CONF_LOW"
    LINE_SUM_MISMATCH = "LINE_SUM_MISMATCH"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    ROW_MATH_MISMATCH = "ROW_MATH_MISMATCH"
    TEMPLATE_NO_MATCH = "TEMPLATE_NO_MATCH"
    TEMPLATE_LOW_SCORE = "TEMPLATE_LOW_SCORE"
    TEMPLATE_MARGIN_LOW = "TEMPLATE_MARGIN_LOW"
    DATE_AMBIGUOUS = "DATE_AMBIGUOUS"
    NO_TEXT_LAYER = "NO_TEXT_LAYER"
    TEXT_LAYER_CORRUPT = "TEXT_LAYER_CORRUPT"
    OCR_DISABLED = "OCR_DISABLED"
    OCR_NOT_INSTALLED = "OCR_NOT_INSTALLED"
    OCR_NO_TEXT_FOUND = "OCR_NO_TEXT_FOUND"
    ENCRYPTED = "ENCRYPTED"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNREADABLE = "UNREADABLE"
    TIMEOUT = "TIMEOUT"
    WORKER_CRASHED = "WORKER_CRASHED"
    DUPLICATE_FILE = "DUPLICATE_FILE"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    NO_TABLE_HEADER = "NO_TABLE_HEADER"
    EMPTY_TABLE = "EMPTY_TABLE"
    EXCEPTION = "EXCEPTION"


class PageSource(str, Enum):
    TEXT = "text"
    OCR = "ocr"
    CORRUPT = "corrupt"
    EMPTY = "empty"


# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 1.0          # 0..1; Tesseract conf/100, or 1.0 for text layer
    line_id: int = -1

    @property
    def cx(self) -> float: return (self.x0 + self.x1) / 2.0
    @property
    def cy(self) -> float: return (self.y0 + self.y1) / 2.0
    @property
    def height(self) -> float: return self.y1 - self.y0
    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(slots=True)
class TextLine:
    text: str
    words: list[Word]
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cy(self) -> float: return (self.y0 + self.y1) / 2.0
    @property
    def bbox(self): return (self.x0, self.y0, self.x1, self.y1)


@dataclass(slots=True)
class Ruling:
    """A vector line from the PDF. Used for table column detection."""
    x0: float
    y0: float
    x1: float
    y1: float
    vertical: bool


@dataclass(slots=True)
class Page:
    number: int                 # 1-based
    width: float
    height: float
    source: PageSource
    words: list[Word] = field(default_factory=list)
    lines: list[TextLine] = field(default_factory=list)
    rulings: list[Ruling] = field(default_factory=list)
    rotation: int = 0

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    @property
    def mean_conf(self) -> float:
        if not self.words:
            return 0.0
        return statistics.fmean(w.conf for w in self.words)

    def to_abs(self, bbox_norm) -> tuple[float, float, float, float]:
        """Normalised [0,1] bbox -> absolute points on this page."""
        x0, y0, x1, y1 = bbox_norm
        return (x0 * self.width, y0 * self.height, x1 * self.width, y1 * self.height)

    def to_norm(self, bbox) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bbox
        w = self.width or 1.0
        h = self.height or 1.0
        return (x0 / w, y0 / h, x1 / w, y1 / h)

    def words_in(self, bbox, pad: float = 0.0) -> list[Word]:
        x0, y0, x1, y1 = bbox
        return [w for w in self.words
                if w.cx >= x0 - pad and w.cx <= x1 + pad
                and w.cy >= y0 - pad and w.cy <= y1 + pad]


@dataclass(slots=True)
class PdfDocument:
    path: str
    sha256: str
    page_count: int
    pages: list[Page] = field(default_factory=list)
    encrypted: bool = False
    file_size: int = 0
    load_error: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def needs_ocr(self) -> bool:
        return any(p.source in (PageSource.CORRUPT, PageSource.EMPTY)
                   for p in self.pages)


# --------------------------------------------------------------------------
@dataclass(slots=True)
class Candidate:
    """One possible value for a field, before ambiguity resolution."""
    text: str
    bbox: tuple[float, float, float, float]
    page: int
    conf: float = 1.0
    strategy: str = ""


@dataclass(slots=True)
class FieldResult:
    name: str
    value: Any = None                # typed value (Decimal / date / str / int)
    raw: str = ""                    # text before post-processing
    status: Status = Status.MISSING
    confidence: float = 0.0
    strategy: str = ""
    page: int = 0
    bbox: tuple | None = None
    reasons: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # populated when AMBIGUOUS
    corrected: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.EXTRACTED, Status.LOW_CONFIDENCE)

    def display(self) -> str:
        if self.value is None:
            return ""
        return str(self.value)


@dataclass(slots=True)
class LineItem:
    index: int
    cells: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    page: int = 0
    bbox: tuple | None = None
    status: Status = Status.EXTRACTED
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass(slots=True)
class CrossCheckResult:
    check_id: str
    passed: bool
    detail: str = ""
    severity: str = "review"         # review | warn


@dataclass(slots=True)
class DocResult:
    doc_id: str
    source_path: str
    source_name: str
    sha256: str
    run_id: str = ""
    page_count: int = 0
    template_id: str = ""
    template_version: int = 0
    vendor: str = ""
    doc_type: str = ""
    match_score: float = 0.0
    match_margin: float = 0.0
    runner_up: str = ""
    status: DocStatus = DocStatus.FAILED
    fields: dict[str, FieldResult] = field(default_factory=dict)
    line_items: list[LineItem] = field(default_factory=list)
    cross_checks: list[CrossCheckResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    duration_ms: int = 0
    processed_utc: str = ""
    error: str | None = None
    traceback: str | None = None

    # -- derived ----------------------------------------------------------
    def field_success_rate(self) -> float:
        if not self.fields:
            return 0.0
        good = sum(1 for f in self.fields.values() if f.status == Status.EXTRACTED)
        return good / len(self.fields)

    def flagged_fields(self) -> list[FieldResult]:
        return [f for f in self.fields.values()
                if f.status not in (Status.EXTRACTED, Status.MISSING)]

    def derive_status(self) -> DocStatus:
        if self.error:
            return DocStatus.FAILED
        if not self.template_id:
            return DocStatus.FAILED
        if any(not c.passed and c.severity == "review" for c in self.cross_checks):
            return DocStatus.NEEDS_REVIEW
        for f in self.fields.values():
            if f.status in (Status.LOW_CONFIDENCE, Status.AMBIGUOUS,
                            Status.NOT_FOUND, Status.INVALID, Status.FAILED):
                return DocStatus.NEEDS_REVIEW
        if any(li.status != Status.EXTRACTED for li in self.line_items):
            return DocStatus.NEEDS_REVIEW
        return DocStatus.OK

    def all_reasons(self) -> list[str]:
        out = list(self.reasons)
        for f in self.fields.values():
            out.extend(f.reasons)
        for c in self.cross_checks:
            if not c.passed:
                out.append(c.check_id.upper())
        seen, uniq = set(), []
        for r in out:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return uniq

    def journal_line(self) -> dict:
        """The compact record appended to journal.jsonl and read by the grid."""
        return {
            "doc_id": self.doc_id,
            "src": self.source_name,
            "sha256": self.sha256[:16],
            "template_id": self.template_id,
            "tpl_version": self.template_version,
            "vendor": self.vendor,
            "doc_type": self.doc_type,
            "status": self.status.value,
            "score": round(self.match_score, 3),
            "reasons": self.all_reasons()[:8],
            "fields_ok": sum(1 for f in self.fields.values()
                             if f.status == Status.EXTRACTED),
            "fields_total": len(self.fields),
            "lines": len(self.line_items),
            "ms": self.duration_ms,
            "ts": self.processed_utc,
        }

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "source_path": self.source_path,
            "source_name": self.source_name, "sha256": self.sha256,
            "run_id": self.run_id, "page_count": self.page_count,
            "template_id": self.template_id, "template_version": self.template_version,
            "vendor": self.vendor, "doc_type": self.doc_type,
            "match_score": self.match_score, "match_margin": self.match_margin,
            "runner_up": self.runner_up, "status": self.status.value,
            "duration_ms": self.duration_ms, "processed_utc": self.processed_utc,
            "error": self.error, "traceback": self.traceback,
            "reasons": self.reasons,
            "fields": {
                n: {"value": None if f.value is None else str(f.value),
                    "raw": f.raw, "status": f.status.value,
                    "confidence": round(f.confidence, 4), "strategy": f.strategy,
                    "page": f.page, "bbox": list(f.bbox) if f.bbox else None,
                    "reasons": f.reasons, "candidates": f.candidates,
                    "corrected": f.corrected}
                for n, f in self.fields.items()
            },
            "line_items": [
                {"index": li.index,
                 "cells": {k: (None if v is None else str(v))
                           for k, v in li.cells.items()},
                 "raw": li.raw, "page": li.page,
                 "bbox": list(li.bbox) if li.bbox else None,
                 "status": li.status.value, "reasons": li.reasons,
                 "confidence": round(li.confidence, 4)}
                for li in self.line_items
            ],
            "cross_checks": [
                {"check_id": c.check_id, "passed": c.passed,
                 "detail": c.detail, "severity": c.severity}
                for c in self.cross_checks
            ],
        }
