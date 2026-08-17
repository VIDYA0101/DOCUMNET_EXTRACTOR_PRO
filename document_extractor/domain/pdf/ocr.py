"""Optical character recognition, via an externally-installed Tesseract.

Deliberately not bundled into the packaged EXE. Tesseract is a separate
native binary with its own DLLs and language data files - exactly the kind
of dependency that has already caused three rounds of Windows-only packaging
bugs in this project (pyexpat's split-out DLL, pypdfium2's ctypes-loaded
engine, Anaconda's non-standard file layout), none of which were
reproducible without an actual Windows machine to test on. Bundling a fourth
native dependency into the same build would very likely repeat that pattern.

Keeping Tesseract external avoids all of that: the app detects it if it is
there, uses it, and explains plainly how to install it if not - the same
relationship this project already has with Python itself for the
run-app.bat path.

Everything downstream of this module - anchors, extraction strategies,
confidence scoring, field discovery - already expects a Page full of Word
objects in PDF points, with a per-word confidence. That is true whether the
words came from the text layer or from here; nothing else in the codebase
needs to know or care which.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .raster import render_page_image
from .text_layer import _group_lines, normalise_text
from ..models import Page, PageSource, Word

log = logging.getLogger(__name__)

# Common install locations on Windows when Tesseract was not added to PATH -
# the official UB-Mannheim installer defaults to one of these.
_WINDOWS_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

_NOT_CHECKED = object()      # sentinel: caching hasn't run yet
_cached_path: str | None = _NOT_CHECKED  # type: ignore[assignment]


def find_tesseract() -> str | None:
    """Locate the tesseract executable. Cached after the first real check,
    since probing the filesystem and spawning a subprocess on every single
    page of a large batch would be wasteful."""
    global _cached_path
    if _cached_path is not _NOT_CHECKED:
        return _cached_path

    found = shutil.which("tesseract")
    if not found:
        for candidate in _WINDOWS_CANDIDATES:
            if Path(candidate).exists():
                found = candidate
                break
    _cached_path = found
    return found


def is_available() -> bool:
    return find_tesseract() is not None


def install_instructions() -> str:
    return (
        "OCR needs Tesseract, a free offline OCR engine, installed "
        "separately - it is not bundled with this application.\n\n"
        "Windows: download and run the installer from\n"
        "  https://github.com/UB-Mannheim/tesseract/wiki\n"
        "Accept the default install location, then restart this "
        "application.\n\n"
        "This only needs to be done once per machine."
    )


def ocr_page(pdf_path: str | Path, page_number: int, *, dpi: int = 300,
            lang: str = "eng") -> Page | None:
    """OCR a single page and return it in the same Page/Word shape the
    text-layer path produces. Returns None if Tesseract is unavailable or
    the page could not be rendered - never raises for those cases, matching
    the rest of this package's "never crash the batch over one document"
    contract.
    """
    tesseract_path = find_tesseract()
    if not tesseract_path:
        log.warning("OCR requested but tesseract was not found")
        return None

    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        log.error("pytesseract not installed")
        return None

    pytesseract.pytesseract.tesseract_cmd = tesseract_path

    image, scale = render_page_image(pdf_path, page_number, dpi=dpi)
    if image is None:
        return None

    try:
        data = pytesseract.image_to_data(image, lang=lang, output_type=Output.DICT)
    except Exception as exc:
        log.warning("OCR failed on %s page %s: %s", pdf_path, page_number, exc)
        return None

    # image_to_data returns one row per detected element at every structural
    # level (page/block/paragraph/line/word). Level 5 is the word level -
    # anything else here is a container row, not a token with real text.
    words: list[Word] = []
    n = len(data.get("level", []))
    for i in range(n):
        if data["level"][i] != 5:
            continue
        text = normalise_text(data["text"][i])
        if not text:
            continue
        conf_raw = data["conf"][i]
        try:
            conf = max(0.0, min(1.0, float(conf_raw) / 100.0))
        except (TypeError, ValueError):
            conf = 0.0

        # Pixel coordinates from the rendered image, converted back to PDF
        # points using the ACTUAL scale render_page_image reported - not a
        # recomputed dpi/72, for exactly the reason documented in raster.py.
        left, top = data["left"][i], data["top"][i]
        width, height = data["width"][i], data["height"][i]
        words.append(Word(
            text=text,
            x0=left / scale, y0=top / scale,
            x1=(left + width) / scale, y1=(top + height) / scale,
            conf=conf,
        ))

    lines = _group_lines(words)
    for line_id, line in enumerate(lines):
        for w in line.words:
            object.__setattr__(w, "line_id", line_id)

    page_w_pt = image.width / scale
    page_h_pt = image.height / scale
    return Page(number=page_number, width=page_w_pt, height=page_h_pt,
                source=PageSource.OCR, words=words, lines=lines, rulings=[])
