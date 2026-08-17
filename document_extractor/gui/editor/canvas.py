"""PDF canvas for the template editor.

A QGraphicsScene rather than a pixmap in a label: field boxes need scene
coordinates, selection, and resize handles.

All geometry is stored in PDF points and converted at the boundary. Rendering
at device pixel ratio matters: without it, boxes land in the wrong place on
150% scaled displays, which is a bug that ships often and is hard to spot on a
100% dev machine.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QImage, QPainter, QPen, QPixmap,
                           QFont)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView)

from ...domain.models import Page
from ...domain.pdf.raster import render_page_png

FIELD_COLOURS = ["#2E7DD1", "#C7522A", "#2F8F5B", "#8B5CF6", "#D97706",
                 "#0E7490", "#BE185D", "#4D7C0F"]


class ResizeHandle(QGraphicsEllipseItem):
    """A small draggable circle at one corner of a FieldBox.

    Mirrors the corner-drag interaction already proven in the browser
    prototype - same shape, same behaviour, now driving a real Qt scene
    instead of a CSS overlay. A handle is a CHILD of its FieldBox, which is
    what makes reading/writing "the parent box's current rectangle" a
    coordinate-mapping problem rather than independent bookkeeping: the
    handle's own position is always in the parent's local coordinate space.
    """
    SIZE = 9

    def __init__(self, parent_box: "FieldBox", corner: str):
        r = self.SIZE / 2
        super().__init__(-r, -r, self.SIZE, self.SIZE, parent_box)
        self.parent_box = parent_box
        self.corner = corner   # 'nw' | 'ne' | 'sw' | 'se'
        self.setBrush(QBrush(QColor(parent_box.colour)))
        self.setPen(QPen(QColor("white"), 1.5))
        self.setZValue(10)
        self.setFlag(QGraphicsItem.ItemIsMovable, False)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        cursor = {"nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
                 "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor}[corner]
        self.setCursor(cursor)

    def mousePressEvent(self, event):
        # Consume the click here so it never reaches the box underneath and
        # gets misread as "start moving the whole box" - a resize and a
        # move are different gestures and must not both fire from one drag.
        event.accept()

    def mouseMoveEvent(self, event):
        local = self.parent_box.mapFromScene(event.scenePos())
        rect = self.parent_box.rect()
        if "w" in self.corner:
            rect.setLeft(min(local.x(), rect.right() - 4))
        if "e" in self.corner:
            rect.setRight(max(local.x(), rect.left() + 4))
        if "n" in self.corner:
            rect.setTop(min(local.y(), rect.bottom() - 4))
        if "s" in self.corner:
            rect.setBottom(max(local.y(), rect.top() + 4))
        self.parent_box.apply_resize(rect.normalized())
        event.accept()

    def mouseReleaseEvent(self, event):
        self.parent_box.resize_finished()
        event.accept()


class FieldBox(QGraphicsRectItem):
    """A named rectangle overlaying the page. Corners can be dragged to
    resize it; the canvas is told about every change so whatever owns the
    canvas (the template editor) can re-extract text from the new area
    live, the same way the browser prototype does."""

    def __init__(self, rect: QRectF, name: str, colour: str, editable=True,
                canvas: "PdfCanvas | None" = None):
        super().__init__(rect)
        self.field_name = name
        self.colour = colour
        self.canvas = canvas
        pen = QPen(QColor(colour), 1.6)
        self.setPen(pen)
        fill = QColor(colour)
        fill.setAlpha(38)
        self.setBrush(QBrush(fill))
        if editable:
            self.setFlags(QGraphicsItem.ItemIsSelectable |
                          QGraphicsItem.ItemIsMovable)
        self.label = QGraphicsSimpleTextItem(name, self)
        font = QFont()
        font.setPointSizeF(7.5)
        self.label.setFont(font)
        self.label.setBrush(QBrush(QColor(colour)))
        self.label.setPos(rect.left(), rect.top() - 11)

        self.handles: dict[str, ResizeHandle] = {}
        if editable:
            for corner in ("nw", "ne", "sw", "se"):
                h = ResizeHandle(self, corner)
                self.handles[corner] = h
            self._reposition_handles()

    def _reposition_handles(self) -> None:
        r = self.rect()
        positions = {"nw": r.topLeft(), "ne": r.topRight(),
                    "sw": r.bottomLeft(), "se": r.bottomRight()}
        for corner, handle in self.handles.items():
            handle.setPos(positions[corner])

    def apply_resize(self, new_rect: QRectF) -> None:
        """Called continuously while a handle is being dragged - update the
        box's own geometry, its handles, and tell the canvas so a live
        preview can update, exactly the same shape as the prototype's
        mousemove handler."""
        self.setRect(new_rect)
        self.label.setPos(new_rect.left(), new_rect.top() - 11)
        self._reposition_handles()
        if self.canvas is not None:
            self.canvas.box_resizing.emit(self.field_name, new_rect)

    def resize_finished(self) -> None:
        """Called once, on mouse release - the point at which the editor
        should treat the correction as final and update the saved field
        definition, not just the live preview."""
        if self.canvas is not None:
            self.canvas.box_resized.emit(self.field_name, self.rect())

    def set_name(self, name: str) -> None:
        self.field_name = name
        self.label.setText(name)


class PdfCanvas(QGraphicsView):
    """Renders one page and lets the user drag a selection box over it."""

    region_selected = Signal(QRectF)       # in PDF points
    box_clicked = Signal(str)
    box_resizing = Signal(str, QRectF)     # field_name, new rect in PIXELS, fires continuously while dragging
    box_resized = Signal(str, QRectF)      # field_name, final rect in PIXELS, fires once on release

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(QBrush(QColor("#3A3A3A")))

        self.pdf_path: str | None = None
        self.page_number = 1
        self.page: Page | None = None
        self.dpi = 150
        self.scale_factor = self.dpi / 72.0
        self._pixmap_item = None
        self._boxes: dict[str, FieldBox] = {}
        self._drag_origin: QPointF | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.snap_to_words = True

    # -- loading -----------------------------------------------------------
    def load(self, pdf_path: str, page_number: int = 1, page: Page | None = None,
             dpi: int = 150) -> bool:
        png, actual_scale = render_page_png(pdf_path, page_number, dpi=dpi)
        if png is None:
            return False
        image = QImage.fromData(png, "PNG")
        if image.isNull():
            return False

        self.pdf_path = pdf_path
        self.page_number = page_number
        self.page = page
        self.dpi = dpi
        # The renderer can silently use a smaller scale than requested on a
        # very large page (see raster.py). Using the real value it reports
        # back, rather than recomputing dpi/72 here, is what keeps every
        # click and drag lined up with the actual pixels on screen - the
        # discrepancy is invisible by eye but would otherwise put every
        # selected region at the wrong place on the page.
        self.scale_factor = actual_scale

        self._scene.clear()
        self._boxes.clear()
        self._pixmap_item = self._scene.addPixmap(QPixmap.fromImage(image))
        self._scene.setSceneRect(QRectF(image.rect()))
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
        return True

    def zoom(self, factor: float) -> None:
        self.scale(factor, factor)

    def fit(self) -> None:
        if self._scene.sceneRect().isValid():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
        else:
            super().wheelEvent(event)

    # -- coordinate conversion --------------------------------------------
    def to_points(self, rect: QRectF) -> QRectF:
        s = self.scale_factor
        return QRectF(rect.left() / s, rect.top() / s,
                      rect.width() / s, rect.height() / s)

    def to_pixels(self, rect: QRectF) -> QRectF:
        s = self.scale_factor
        return QRectF(rect.left() * s, rect.top() * s,
                      rect.width() * s, rect.height() * s)

    # -- overlays ----------------------------------------------------------
    def add_box(self, name: str, bbox_points, colour_index: int = 0) -> None:
        x0, y0, x1, y1 = bbox_points
        rect = self.to_pixels(QRectF(x0, y0, x1 - x0, y1 - y0))
        colour = FIELD_COLOURS[colour_index % len(FIELD_COLOURS)]
        box = FieldBox(rect, name, colour, canvas=self)
        self._scene.addItem(box)
        self._boxes[name] = box

    def clear_boxes(self) -> None:
        for box in self._boxes.values():
            self._scene.removeItem(box)
        self._boxes.clear()

    def highlight(self, name: str) -> None:
        for key, box in self._boxes.items():
            width = 3.0 if key == name else 1.6
            box.setPen(QPen(QColor(box.colour), width))

    # -- drag to select ----------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.pos())
            if isinstance(item, FieldBox):
                self.box_clicked.emit(item.field_name)
                super().mousePressEvent(event)
                return
            if isinstance(item, QGraphicsSimpleTextItem) and \
                    isinstance(item.parentItem(), FieldBox):
                self.box_clicked.emit(item.parentItem().field_name)
                return
            self._drag_origin = self.mapToScene(event.pos())
            self._rubber = QGraphicsRectItem(QRectF(self._drag_origin,
                                                    self._drag_origin))
            pen = QPen(QColor("#1F3B57"), 1.4, Qt.DashLine)
            self._rubber.setPen(pen)
            self._scene.addItem(self._rubber)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._rubber is not None and self._drag_origin is not None:
            current = self.mapToScene(event.pos())
            self._rubber.setRect(QRectF(self._drag_origin, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rubber is not None and self._drag_origin is not None:
            rect = self._rubber.rect()
            self._scene.removeItem(self._rubber)
            self._rubber = None
            self._drag_origin = None
            if rect.width() > 4 and rect.height() > 4:
                points = self.to_points(rect)
                if self.snap_to_words and self.page:
                    points = self._snap(points)
                self.region_selected.emit(points)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _snap(self, rect: QRectF) -> QRectF:
        """Snap the drawn box to whole word bounds. Removes pixel-hunting."""
        bbox = (rect.left(), rect.top(), rect.right(), rect.bottom())
        words = self.page.words_in(bbox, pad=2.0)
        if not words:
            return rect
        x0 = min(w.x0 for w in words)
        y0 = min(w.y0 for w in words)
        x1 = max(w.x1 for w in words)
        y1 = max(w.y1 for w in words)
        return QRectF(x0, y0, x1 - x0, y1 - y0)
