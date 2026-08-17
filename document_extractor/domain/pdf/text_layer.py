"""PDF loading, per-page triage, and text-layer extraction.

Triage runs per page, not per document: real batches contain digital invoices
with scanned annexures, and documents whose ToUnicode maps are broken extract
as text that is confidently wrong.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

import pdfplumber

from ..models import Page, PageSource, PdfDocument, Ruling, TextLine, Word

log = logging.getLogger(__name__)

# Triage thresholds
MIN_CHARS = 50
MIN_PRINTABLE_RATIO = 0.85
MIN_COVERAGE = 0.005

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"), None)
_CURRENCY_FIX = {"\uf0b9": "\u20b9", "\ufffd": ""}


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def normalise_text(s: str) -> str:
    """NFKC, strip zero-width, fix mangled currency glyphs, collapse whitespace.

    Zero-width characters silently break regex matches; this is invisible in a
    debugger and costs hours.
    """
    if not s:
        return ""
    for bad, good in _CURRENCY_FIX.items():
        s = s.replace(bad, good)
    s = s.translate(_ZERO_WIDTH)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    good = sum(1 for c in text if c.isprintable() or c in " \n\t")
    return good / len(text)


def _alphabet_ok(text: str) -> bool:
    """Detect a broken ToUnicode CMap.

    Such PDFs extract text successfully but yield one repeated glyph or a run
    of control characters, so downstream code produces confidently wrong
    output. A ratio of unique-to-total characters cannot be used directly:
    distinct characters saturate around 70 while text length grows without
    bound, so long healthy pages would be flagged. Compare the unique count
    against a saturating expectation instead.
    """
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return False
    unique = len(set(stripped))
    expected = min(20, max(6, len(stripped) // 12))
    return unique >= expected


def triage_page(text: str, words: list[Word], page_w: float, page_h: float,
                has_images: bool) -> PageSource:
    """Classify a page's text layer. Returns TEXT, CORRUPT or EMPTY.

    EMPTY/CORRUPT pages are the OCR queue; with OCR disabled they surface as
    an explicit reason rather than a silently empty extraction.
    """
    clean = text.strip()
    if len(clean) < MIN_CHARS:
        return PageSource.EMPTY
    if _printable_ratio(clean) < MIN_PRINTABLE_RATIO:
        return PageSource.CORRUPT
    if not _alphabet_ok(clean):
        return PageSource.CORRUPT
    if words and page_w and page_h:
        area = sum((w.x1 - w.x0) * (w.y1 - w.y0) for w in words)
        if area / (page_w * page_h) < MIN_COVERAGE and has_images:
            return PageSource.EMPTY
    return PageSource.TEXT


def _group_lines(words: list[Word], tol: float = 2.5) -> list[TextLine]:
    """Cluster words into visual lines by y-centre, then order left-to-right."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (round(w.cy, 1), w.x0))
    lines: list[list[Word]] = []
    current: list[Word] = [ordered[0]]
    ref = ordered[0].cy
    band = max(tol, ordered[0].height * 0.6)
    for w in ordered[1:]:
        if abs(w.cy - ref) <= band:
            current.append(w)
        else:
            lines.append(current)
            current = [w]
            ref = w.cy
            band = max(tol, w.height * 0.6)
    lines.append(current)

    out: list[TextLine] = []
    for idx, group in enumerate(lines):
        group.sort(key=lambda w: w.x0)
        out.append(TextLine(
            text=normalise_text(" ".join(w.text for w in group)),
            words=group,
            x0=min(w.x0 for w in group), y0=min(w.y0 for w in group),
            x1=max(w.x1 for w in group), y1=max(w.y1 for w in group),
        ))
    return out


def _extract_page(pp_page, number: int) -> Page:
    width = float(pp_page.width or 0)
    height = float(pp_page.height or 0)
    rotation = int(getattr(pp_page, "rotation", 0) or 0)

    words: list[Word] = []
    try:
        raw_words = pp_page.extract_words(
            use_text_flow=False, keep_blank_chars=False,
            extra_attrs=["size"]) or []
    except Exception as exc:  # malformed content stream
        log.warning("word extraction failed on page %s: %s", number, exc)
        raw_words = []

    for rw in raw_words:
        text = normalise_text(rw.get("text", ""))
        if not text:
            continue
        words.append(Word(
            text=text,
            x0=float(rw["x0"]), y0=float(rw["top"]),
            x1=float(rw["x1"]), y1=float(rw["bottom"]),
            conf=1.0,
        ))

    rulings: list[Ruling] = []
    try:
        for ln in (pp_page.lines or []):
            x0, y0 = float(ln["x0"]), float(ln["top"])
            x1, y1 = float(ln["x1"]), float(ln["bottom"])
            rulings.append(Ruling(x0, y0, x1, y1, vertical=abs(x1 - x0) < 1.5))
        for rc in (pp_page.rects or []):
            x0, y0 = float(rc["x0"]), float(rc["top"])
            x1, y1 = float(rc["x1"]), float(rc["bottom"])
            if abs(x1 - x0) < 2.0:
                rulings.append(Ruling(x0, y0, x1, y1, vertical=True))
            elif abs(y1 - y0) < 2.0:
                rulings.append(Ruling(x0, y0, x1, y1, vertical=False))
    except Exception:
        pass

    lines = _group_lines(words)
    raw_text = "\n".join(l.text for l in lines)
    has_images = bool(getattr(pp_page, "images", None))
    source = triage_page(raw_text, words, width, height, has_images)

    for i, ln in enumerate(lines):
        for w in ln.words:
            object.__setattr__(w, "line_id", i)

    return Page(number=number, width=width, height=height, source=source,
                words=words, lines=lines, rulings=rulings, rotation=rotation)


def load_pdf(path: str | Path, *, max_pages: int = 200,
             max_file_mb: int = 100, password: str | None = None,
             ocr_enabled: bool = False, ocr_dpi: int = 300) -> PdfDocument:
    """Load a PDF into the canonical page model. Never raises for bad input.

    ocr_enabled only affects pages that have no usable text layer at all -
    it never runs on, or overrides, a page that already extracted real text.
    OCR is slow (roughly a second or more per page) and imperfect by nature,
    so it is applied as narrowly as possible: the last resort for a page,
    not the default path for every page.
    """
    p = Path(path)
    size = p.stat().st_size if p.exists() else 0
    doc = PdfDocument(path=str(p), sha256="", page_count=0, file_size=size)

    if not p.exists():
        doc.load_error = "UNREADABLE"
        return doc
    if size > max_file_mb * 1024 * 1024:
        doc.load_error = "FILE_TOO_LARGE"
        return doc

    try:
        doc.sha256 = sha256_file(p)
    except OSError:
        doc.load_error = "UNREADABLE"
        return doc

    try:
        with pdfplumber.open(str(p), password=password or "") as pdf:
            doc.page_count = len(pdf.pages)
            if doc.page_count > max_pages:
                doc.load_error = "TOO_MANY_PAGES"
                return doc
            for i, pp in enumerate(pdf.pages, start=1):
                try:
                    doc.pages.append(_extract_page(pp, i))
                except Exception as exc:
                    log.warning("page %s of %s failed: %s", i, p.name, exc)
                    doc.pages.append(Page(number=i, width=0, height=0,
                                          source=PageSource.CORRUPT))
    except Exception as exc:
        msg = str(exc).lower()
        doc.load_error = "ENCRYPTED" if ("password" in msg or "encrypt" in msg) \
            else "UNREADABLE"
        doc.encrypted = doc.load_error == "ENCRYPTED"
        log.warning("could not open %s: %s", p.name, exc)
        return doc

    if ocr_enabled and any(pg.source in (PageSource.CORRUPT, PageSource.EMPTY)
                           for pg in doc.pages):
        _apply_ocr_fallback(doc, p, ocr_dpi)
    return doc


def _apply_ocr_fallback(doc: PdfDocument, path: Path, dpi: int) -> None:
    """Replace any page with no usable text with an OCR'd version, in place.

    Imported lazily so pytesseract (and the external tesseract binary it
    needs) are only ever touched by code paths that actually asked for OCR -
    every other code path in this application, including the whole test
    suite up to this point, has zero dependency on either one.
    """
    from .ocr import is_available, ocr_page

    if not is_available():
        log.info("OCR requested for %s but tesseract is not installed", path.name)
        return

    for i, page in enumerate(doc.pages):
        if page.source not in (PageSource.CORRUPT, PageSource.EMPTY):
            continue
        try:
            ocr_result = ocr_page(str(path), page.number, dpi=dpi)
        except Exception as exc:
            log.warning("OCR raised on %s page %s: %s", path.name, page.number, exc)
            continue
        if ocr_result is not None:
            doc.pages[i] = ocr_result
