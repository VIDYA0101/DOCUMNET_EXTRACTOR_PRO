"""Page rendering via pypdfium2 (BSD/Apache) - used by the editor canvas,
review crops, and OCR rasterisation.

Deliberately not PyMuPDF: that is AGPL-3.0 and would require releasing this
application's source under AGPL.

One core renderer, not two. The editor canvas and the OCR pipeline both need
a page rendered to an image at a target DPI, and both need to know the REAL
scale used - which can differ from the requested one, see the guard below.
Having render_page_png and ocr.py each reimplement this would let them drift
apart exactly the way canvas.py's own copy of this logic already did once.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MAX_RENDER_PIXELS = 60_000_000   # guard against decompression bombs


def render_page_image(pdf_path: str | Path, page_number: int, dpi: int = 150):
    """Render a 1-based page to a PIL Image. Returns (image, actual_scale).

    actual_scale is points-to-pixels, which can be LOWER than dpi/72 implies
    if the page was large enough to hit the safety clamp below. A caller
    that assumes the requested DPI was honoured, rather than using this
    returned value, computes every subsequent coordinate wrong on exactly
    the pages where the clamp fires - silently, since nothing about a
    smaller-than-expected image looks wrong on its own. (This is not a
    hypothetical: it is what canvas.py did before this was fixed.)

    Returns (None, None) on any failure - a missing PDF, a page number out
    of range, a corrupt file. Never raises for bad input.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.error("pypdfium2 not installed")
        return None, None

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            if page_number < 1 or page_number > len(pdf):
                return None, None
            page = pdf[page_number - 1]
            scale = dpi / 72.0
            w, h = page.get_size()
            if (w * scale) * (h * scale) > MAX_RENDER_PIXELS:
                scale = (MAX_RENDER_PIXELS / (w * h)) ** 0.5
            image = page.render(scale=scale).to_pil()
            return image, scale
        finally:
            pdf.close()
    except Exception as exc:
        log.warning("render failed for %s p%s: %s", pdf_path, page_number, exc)
        return None, None


def render_page_png(pdf_path: str | Path, page_number: int, dpi: int = 150,
                    crop=None) -> tuple[bytes, float] | tuple[None, None]:
    """Render a 1-based page to PNG bytes. `crop` is a bbox in points.
    Returns (png_bytes, actual_scale) - see render_page_image for why the
    returned scale matters and must be used, not recomputed from dpi.
    """
    image, scale = render_page_image(pdf_path, page_number, dpi=dpi)
    if image is None:
        return None, None
    try:
        if crop:
            x0, y0, x1, y1 = crop
            box = (max(0, int(x0 * scale)), max(0, int(y0 * scale)),
                   min(image.width, int(x1 * scale)),
                   min(image.height, int(y1 * scale)))
            if box[2] > box[0] and box[3] > box[1]:
                image = image.crop(box)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue(), scale
    except Exception as exc:
        log.warning("PNG encode failed for %s p%s: %s", pdf_path, page_number, exc)
        return None, None


def page_sizes(pdf_path: str | Path) -> list[tuple[float, float]]:
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            return [tuple(pdf[i].get_size()) for i in range(len(pdf))]
        finally:
            pdf.close()
    except Exception:
        return []
