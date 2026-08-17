"""Template editor.

The user drags a box; the application infers a durable anchor-relative rule
from what is inside and around it. Storing the rectangle itself would produce
a template that breaks on the second invoice, because a 30-line invoice pushes
the totals hundreds of points down the page.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ...domain.extraction.anchors import generalise_pattern, infer_anchor
from ...domain.extraction.discover import DiscoveredField, discover_fields
from ...domain.extraction.engine import extract_document
from ...domain.matching.matcher import build_fingerprint
from ...domain.models import PdfDocument
from ...domain.pdf.text_layer import load_pdf
from ...domain.template.repository import TemplateRepository, make_template_id
from ...domain.template.schema import Template, validate
from ...infra.paths import slugify
from .canvas import PdfCanvas

DOC_TYPES = ["Invoice", "Purchase Order", "Delivery Note", "Statement",
             "Credit Note", "Receipt", "Other"]
DATA_TYPES = ["string", "decimal", "date", "int"]
COMMON_FIELDS = ["invoice_number", "invoice_date", "po_number", "customer_name",
                 "gst_number", "subtotal", "tax_total", "grand_total",
                 "vendor_name", "due_date", "currency"]


class TemplateEditor(QWidget):
    saved = Signal(str)

    def __init__(self, repo: TemplateRepository, settings, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.settings = settings
        self.doc: PdfDocument | None = None
        self.pdf_path: str | None = None
        self.template: Template | None = None
        self.field_defs: list[dict] = []
        self.sample_paths: list[Path] = []
        self._pending_bbox = None
        self._discovered_fields: list = []
        self._build()

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_open = QPushButton("Open sample PDF…")
        self.btn_open.clicked.connect(self.open_pdf)
        self.btn_add_sample = QPushButton("Add another sample…")
        self.btn_add_sample.clicked.connect(self.add_sample)
        self.btn_test = QPushButton("Test on all samples")
        self.btn_test.clicked.connect(self.test_samples)
        self.btn_save = QPushButton("Save template")
        self.btn_save.setProperty("primary", True)
        self.btn_save.clicked.connect(self.save)
        self.lbl_page = QLabel("no document")
        for w in (self.btn_open, self.btn_add_sample, self.btn_test):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.lbl_page)
        bar.addWidget(self.btn_save)
        outer.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        self.canvas = PdfCanvas()
        self.canvas.region_selected.connect(self.on_region)
        self.canvas.box_clicked.connect(self.select_field)
        self.canvas.box_resizing.connect(self.on_box_resizing)
        self.canvas.box_resized.connect(self.on_box_resized)
        splitter.addWidget(self.canvas)

        side = QWidget()
        side_layout = QVBoxLayout(side)

        meta = QGroupBox("Vendor and document")
        form = QFormLayout(meta)
        self.in_vendor = QLineEdit()
        self.in_vendor.setPlaceholderText("ACME INDUSTRIES PVT LTD")
        self.in_gstin = QLineEdit()
        self.in_gstin.setPlaceholderText("29AABCA1234A1ZF (optional)")
        self.in_doctype = QComboBox()
        self.in_doctype.addItems(DOC_TYPES)
        self.in_doctype.setEditable(True)
        form.addRow("Vendor name", self.in_vendor)
        form.addRow("GSTIN / Tax ID", self.in_gstin)
        form.addRow("Document type", self.in_doctype)
        side_layout.addWidget(meta)

        discovered = QGroupBox("Fields found automatically")
        dlayout = QVBoxLayout(discovered)
        self.lbl_discovered_hint = QLabel(
            "Open a PDF to scan it. Click any row to fill in the field below "
            "with it - no need to find and drag over it by hand. If "
            "something's missing or wrong here, draw a box over it on the "
            "page instead.")
        self.lbl_discovered_hint.setWordWrap(True)
        self.lbl_discovered_hint.setStyleSheet("color:#5B6B7B;font-size:11px;")
        self.list_discovered = QListWidget()
        self.list_discovered.setMaximumHeight(160)
        self.list_discovered.itemClicked.connect(self.use_discovered_field)
        self.list_discovered.itemChanged.connect(self.on_discovered_checkbox_changed)
        dlayout.addWidget(self.lbl_discovered_hint)
        dlayout.addWidget(self.list_discovered)
        side_layout.addWidget(discovered)

        rule = QGroupBox("Selected field")
        rform = QFormLayout(rule)
        self.in_field = QComboBox()
        self.in_field.setEditable(True)
        self.in_field.addItems(COMMON_FIELDS)
        self.in_type = QComboBox()
        self.in_type.addItems(DATA_TYPES)
        self.in_required = QCheckBox("Required")
        self.in_strategy = QComboBox()
        self.in_anchor = QLineEdit()
        self.in_direction = QComboBox()
        self.in_direction.addItems(["right", "below", "left", "above"])
        self.in_pattern = QLineEdit()
        self.in_distance = QDoubleSpinBox()
        self.in_distance.setRange(20, 600)
        self.in_distance.setValue(220)
        self.lbl_preview = QLabel("—")
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet("padding:6px;background:#F2F5F8;"
                                       "border:1px solid #D6DEE6;")
        rform.addRow("Field name", self.in_field)
        rform.addRow("Data type", self.in_type)
        rform.addRow("", self.in_required)
        rform.addRow("Rule", self.in_strategy)
        rform.addRow("Anchor text", self.in_anchor)
        rform.addRow("Value is", self.in_direction)
        rform.addRow("Max distance (pt)", self.in_distance)
        rform.addRow("Pattern", self.in_pattern)
        rform.addRow("Extracts", self.lbl_preview)

        btns = QHBoxLayout()
        self.btn_preview = QPushButton("Preview")
        self.btn_preview.clicked.connect(self.preview_rule)
        self.btn_apply = QPushButton("Add / update field")
        self.btn_apply.clicked.connect(self.apply_field)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.clicked.connect(self.remove_field)
        for b in (self.btn_preview, self.btn_apply, self.btn_remove):
            btns.addWidget(b)
        rform.addRow(btns)
        side_layout.addWidget(rule)

        self.list_fields = QListWidget()
        self.list_fields.itemClicked.connect(
            lambda item: self.select_field(item.text().split("  ")[0]))
        side_layout.addWidget(QLabel("Fields defined"))
        side_layout.addWidget(self.list_fields, 1)

        self.table_results = QTableWidget(0, 3)
        self.table_results.setHorizontalHeaderLabels(["Sample", "Result", "Detail"])
        self.table_results.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        side_layout.addWidget(QLabel("Sample test results"))
        side_layout.addWidget(self.table_results, 1)

        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        self.hint = QLabel(
            "Drag a box around a value on the page. The rule is inferred from "
            "the label next to it, so the template still works when the layout "
            "shifts. Add at least three samples before saving.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#5B6B7B;padding:4px;")
        outer.addWidget(self.hint)

    # -- documents ---------------------------------------------------------
    def open_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open a representative PDF",
                                              "", "PDF files (*.pdf)")
        if not path:
            return
        self.field_defs.clear()
        self.list_fields.clear()
        self.sample_paths = [Path(path)]
        self._load(path)

    def add_sample(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add sample PDFs", "",
                                                "PDF files (*.pdf)")
        for p in paths:
            if Path(p) not in self.sample_paths:
                self.sample_paths.append(Path(p))
        self.hint.setText(f"{len(self.sample_paths)} sample(s) loaded. "
                          "Three or more makes a template that survives.")

    def _load(self, path: str) -> None:
        self.doc = load_pdf(path, max_pages=self.settings.max_pages,
                            max_file_mb=self.settings.max_file_mb,
                            ocr_enabled=self.settings.ocr_enabled,
                            ocr_dpi=self.settings.ocr_dpi)
        if self.doc.load_error:
            QMessageBox.warning(self, "Cannot open",
                                f"{Path(path).name}: {self.doc.load_error}")
            return
        if not self.doc.pages:
            QMessageBox.warning(self, "Empty document", "No pages found.")
            return
        page = self.doc.pages[0]
        if page.source.value in ("corrupt", "empty"):
            # By this point load_pdf() has already tried OCR if it was
            # enabled, so a page still in this state means OCR either isn't
            # turned on, isn't installed, or genuinely couldn't read this
            # page - three different situations that call for different
            # next steps, so the message says which one applies.
            if not self.settings.ocr_enabled:
                QMessageBox.information(
                    self, "No text layer",
                    "This page has no usable text layer, and OCR is turned "
                    "off. Turn it on in Settings, or use a digital PDF for "
                    "the template.")
            else:
                from ...domain.pdf.ocr import install_instructions, is_available
                if not is_available():
                    QMessageBox.information(self, "OCR not installed",
                                            install_instructions())
                else:
                    QMessageBox.information(
                        self, "Could not read this page",
                        "OCR ran on this page but did not find readable "
                        "text - it may genuinely be blank, or scanned too "
                        "faintly to read. Try a clearer sample.")
        self.pdf_path = path
        self.canvas.load(path, 1, page)
        self.lbl_page.setText(f"{Path(path).name} — page 1 of {self.doc.page_count}")
        if not self.in_vendor.text():
            self._guess_vendor(page)
        self._run_discovery()

    def _run_discovery(self) -> None:
        """Scan the whole document for label:value pairs, verbatim.

        This runs automatically on open specifically so it is there before
        anyone has to hunt for a value by eye - the box-drawing flow below
        stays exactly as it was, as the fallback for whatever this heuristic
        misses, not a replacement for it.
        """
        self.list_discovered.clear()
        self._discovered_fields = discover_fields(self.doc, max_fields=200)
        if not self._discovered_fields:
            self.lbl_discovered_hint.setText(
                "No label:value pairs were found automatically on this "
                "document. Draw a box over a value on the page as usual.")
            return
        for f in self._discovered_fields:
            display = f"{f.display_label}  =  {f.value}"
            if len(display) > 90:
                display = display[:87] + "…"
            item = QListWidgetItem(f"[p{f.page}] {display}")
            item.setData(Qt.UserRole, f)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.list_discovered.addItem(item)
        self.lbl_discovered_hint.setText(
            f"{len(self._discovered_fields)} field(s) found. Tick a box to add "
            "it to your template - click the row (not the box) to see where "
            "it came from and correct it if needed.")

    def on_discovered_checkbox_changed(self, item: QListWidgetItem) -> None:
        """Ticking a box is the 'include this field' action - the template's
        saved field list is exactly the set of ticked boxes, nothing more."""
        f: DiscoveredField = item.data(Qt.UserRole)
        if f is None:
            return
        if item.checkState() == Qt.Checked:
            spec = self._discovered_field_to_spec(f)
            self.field_defs = [d for d in self.field_defs if d["name"] != spec["name"]]
            self.field_defs.append(spec)
        else:
            self.field_defs = [d for d in self.field_defs
                               if d["name"] != f.suggested_field_name]
        self._refresh_fields()

    def _discovered_field_to_spec(self, f: DiscoveredField) -> dict:
        """Build a real field spec from a discovered label:value pair - the
        same shape _current_spec() produces from a manual drag, so both
        paths save identically. _display_bbox is not part of the template
        schema; it exists only so the editor can redraw and let this exact
        field be corrected later - stripped out before the template is
        actually saved (see _build_template)."""
        return {
            "name": f.suggested_field_name, "label": f.label,
            "kind": "single", "data_type": "string", "required": False,
            "strategies": [{
                "type": "anchor_relative",
                "anchor": {"text": f.label, "match": "fuzzy", "min_ratio": 0.85,
                          "occurrence": "first", "search_scope": {"page": f.page}},
                "value_window": {"direction": "right", "max_distance_pt": 220,
                                 "vertical_tolerance_pt": 6},
                "pattern": generalise_pattern([f.value]) or None,
                "post": ["strip", "collapse_ws"],
            }],
            "validators": [], "on_ambiguous": "review",
            "_display_bbox": list(f.bbox) if f.bbox else None,
            "_display_page": f.page,
        }

    def use_discovered_field(self, item: QListWidgetItem) -> None:
        """Fill in the field form from a row the user clicked, the same way
        drawing a box over that value on the page would - just without
        having to find it first."""
        f: DiscoveredField = item.data(Qt.UserRole)
        if f is None or self.doc is None:
            return
        page = next((p for p in self.doc.pages if p.number == f.page), None)
        if page is None:
            return

        if f.page != self.canvas.page_number:
            self.canvas.load(self.pdf_path, f.page, page)
            self.lbl_page.setText(
                f"{Path(self.pdf_path).name} — page {f.page} of {self.doc.page_count}")

        self.in_field.setCurrentText(f.suggested_field_name)
        self.in_strategy.clear()
        self.in_strategy.addItem("Anchor-relative (recommended)")
        self.in_strategy.addItem("Fixed position (fragile)")
        # The label came directly from the document's own text next to a
        # colon, not a proximity guess - it is the anchor, not just a hint
        # toward one, so it goes in verbatim rather than through infer_anchor.
        self.in_anchor.setText(f.label)
        self.in_direction.setCurrentText("right")
        self.in_pattern.setText(generalise_pattern([f.value]))
        self.lbl_preview.setText(f.value)
        self._guess_type(f.value)

        if f.bbox:
            self._pending_bbox = f.bbox
            self.canvas.clear_boxes()
            self.canvas.add_box(f.suggested_field_name, f.bbox, 0)
            self.canvas.highlight(f.suggested_field_name)

    def on_box_resizing(self, field_name: str, rect_px) -> None:
        """Fires continuously while a handle is being dragged - update the
        preview only, nothing saved yet. Exactly the same live-update
        behaviour already proven in the browser prototype, driven by the
        same underlying page.words_in() the rest of the app uses, not a
        separate implementation."""
        if self.doc is None or not self.doc.pages:
            return
        page = next((p for p in self.doc.pages if p.number == self.canvas.page_number),
                    self.doc.pages[0])
        bbox_pt = self.canvas.to_points(rect_px)
        bbox = (bbox_pt.left(), bbox_pt.top(), bbox_pt.right(), bbox_pt.bottom())
        words = page.words_in(bbox)
        text = " ".join(w.text for w in sorted(words, key=lambda w: w.x0))
        self.lbl_preview.setText(text or "(nothing in this box)")

    def on_box_resized(self, field_name: str, rect_px) -> None:
        """Fires once, on release - the correction is now final. Updates the
        saved field definition, not just what is shown on screen: the new
        bbox, the re-read value, a pattern regenerated from that value, and
        - if the corrected position sits near a different label - a better
        anchor than the one this field started with."""
        if self.doc is None or not self.doc.pages:
            return
        page = next((p for p in self.doc.pages if p.number == self.canvas.page_number),
                    self.doc.pages[0])
        bbox_pt = self.canvas.to_points(rect_px)
        bbox = (bbox_pt.left(), bbox_pt.top(), bbox_pt.right(), bbox_pt.bottom())
        words = page.words_in(bbox)
        text = " ".join(w.text for w in sorted(words, key=lambda w: w.x0))

        spec = next((f for f in self.field_defs if f["name"] == field_name), None)
        if spec is None:
            return
        spec["_display_bbox"] = list(bbox)
        spec["_display_page"] = page.number
        if text:
            spec["strategies"][0]["pattern"] = generalise_pattern([text])
            proposal = infer_anchor(page, bbox)
            if proposal:
                anchor_text, direction = proposal
                spec["strategies"][0]["anchor"]["text"] = anchor_text
                spec["strategies"][0]["value_window"]["direction"] = direction
        self.lbl_preview.setText(text or "(nothing in this box - field kept as before)")


        for line in page.lines[:4]:
            text = line.text.strip()
            if len(text) > 4 and any(c.isalpha() for c in text):
                self.in_vendor.setText(text)
                break
        import re
        m = re.search(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b",
                      page.text)
        if m:
            self.in_gstin.setText(m.group(0))

    # -- rule inference ----------------------------------------------------
    def on_region(self, rect) -> None:
        if self.doc is None or not self.doc.pages:
            return
        page = self.doc.pages[0]
        bbox = (rect.left(), rect.top(), rect.right(), rect.bottom())
        self._pending_bbox = bbox
        words = page.words_in(bbox)
        sample_text = " ".join(w.text for w in sorted(words, key=lambda w: w.x0))

        self.in_strategy.clear()
        proposal = infer_anchor(page, bbox)
        if proposal:
            anchor_text, direction = proposal
            self.in_strategy.addItem("Anchor-relative (recommended)")
            self.in_strategy.addItem("Fixed position (fragile)")
            self.in_anchor.setText(anchor_text)
            self.in_direction.setCurrentText(direction)
        else:
            self.in_strategy.addItem("Fixed position (no label found nearby)")
            self.in_anchor.setText("")

        self.in_pattern.setText(generalise_pattern([sample_text]))
        if sample_text:
            self.lbl_preview.setText(sample_text)
        else:
            # Not just "nothing found" - show exactly what was searched, so a
            # box that finds nothing is diagnosable on sight rather than a
            # dead end. A box drawn far from any text, on the wrong page, or
            # with coordinates that ended up outside the page entirely all
            # look identical as a bare "-" otherwise.
            total_words_on_page = len(page.words)
            self.lbl_preview.setText(
                f"nothing found in this box  "
                f"(searched x:{bbox[0]:.0f}-{bbox[2]:.0f}, "
                f"y:{bbox[1]:.0f}-{bbox[3]:.0f} pt on a "
                f"{page.width:.0f}x{page.height:.0f}pt page, "
                f"{total_words_on_page} words total on the page)")
        self._guess_type(sample_text)

    def _guess_type(self, text: str) -> None:
        from ...domain.extraction.types import parse_amount, parse_date
        if parse_date(text, self.settings.date_formats).ok:
            self.in_type.setCurrentText("date")
        elif parse_amount(text).ok:
            self.in_type.setCurrentText("decimal")
        else:
            self.in_type.setCurrentText("string")

    def _current_spec(self) -> dict | None:
        name = slugify(self.in_field.currentText().strip(), "")
        if not name:
            QMessageBox.warning(self, "Name required", "Give the field a name.")
            return None
        use_anchor = (self.in_anchor.text().strip()
                      and "Anchor" in self.in_strategy.currentText())
        if use_anchor:
            strategy = {
                "type": "anchor_relative",
                "anchor": {"text": self.in_anchor.text().strip(),
                           "match": "fuzzy", "min_ratio": 0.85,
                           "occurrence": "first",
                           "search_scope": {"page": 1}},
                "value_window": {"direction": self.in_direction.currentText(),
                                 "max_distance_pt": self.in_distance.value(),
                                 "vertical_tolerance_pt": 6},
                "pattern": self.in_pattern.text().strip() or None,
                "post": ["strip", "collapse_ws"],
            }
        else:
            if not self._pending_bbox or not self.doc:
                QMessageBox.warning(self, "Draw a box first",
                                    "Select a region on the page.")
                return None
            page = self.doc.pages[0]
            strategy = {
                "type": "region_absolute", "page": 1,
                "bbox_norm": list(page.to_norm(self._pending_bbox)),
                "tolerance_pt": 8,
                "pattern": self.in_pattern.text().strip() or None,
                "post": ["strip", "collapse_ws"],
            }
        return {
            "name": name, "label": self.in_field.currentText().strip(),
            "kind": "single", "data_type": self.in_type.currentText(),
            "required": self.in_required.isChecked(),
            "strategies": [strategy], "validators": [], "on_ambiguous": "review",
        }

    def preview_rule(self) -> None:
        spec = self._current_spec()
        if spec is None or self.doc is None:
            return
        from ...domain.extraction import strategies as S
        outcome = S.run_strategy(self.doc, spec["strategies"][0])
        if not outcome.candidates:
            self.lbl_preview.setText(f"nothing found ({outcome.reason})")
        elif len(outcome.candidates) > 1:
            found = ", ".join(c.text for c in outcome.candidates[:4])
            self.lbl_preview.setText(f"ambiguous — {len(outcome.candidates)} "
                                     f"matches: {found}")
        else:
            self.lbl_preview.setText(outcome.candidates[0].text)

    def apply_field(self) -> None:
        spec = self._current_spec()
        if spec is None:
            return
        self.field_defs = [f for f in self.field_defs if f["name"] != spec["name"]]
        self.field_defs.append(spec)
        self._refresh_fields()

    def remove_field(self) -> None:
        name = slugify(self.in_field.currentText().strip(), "")
        self.field_defs = [f for f in self.field_defs if f["name"] != name]
        self._refresh_fields()

    def select_field(self, name: str) -> None:
        spec = next((f for f in self.field_defs if f["name"] == name), None)
        if not spec:
            return
        self.in_field.setCurrentText(spec["name"])
        self.in_type.setCurrentText(spec["data_type"])
        self.in_required.setChecked(spec.get("required", False))
        s = spec["strategies"][0]
        self.in_pattern.setText(s.get("pattern") or "")
        if s["type"] == "anchor_relative":
            self.in_anchor.setText(s["anchor"]["text"])
            self.in_direction.setCurrentText(s["value_window"]["direction"])
        self.canvas.highlight(name)

    def _refresh_fields(self) -> None:
        self.list_fields.clear()
        self.canvas.clear_boxes()
        for i, spec in enumerate(self.field_defs):
            s = spec["strategies"][0]
            how = (f"anchor: {s['anchor']['text']}"
                   if s["type"] == "anchor_relative" else "fixed position")
            item = QListWidgetItem(f"{spec['name']}  ({spec['data_type']}, {how})")
            self.list_fields.addItem(item)

            bbox = None
            if s["type"] == "region_absolute" and self.doc and self.doc.pages:
                bbox = self.doc.pages[0].to_abs(s["bbox_norm"])
            elif spec.get("_display_bbox") and spec.get("_display_page", 1) == \
                    self.canvas.page_number:
                # A field built from a discovered value on the CURRENTLY
                # shown page - anchor_relative fields have no fixed bbox of
                # their own, but this one is still worth showing so it can
                # be corrected the same way, and so ticking several boxes
                # shows all of them on the page at once, not just the last.
                bbox = tuple(spec["_display_bbox"])
            if bbox:
                self.canvas.add_box(spec["name"], bbox, i)

    # -- build, test, save -------------------------------------------------
    def _build_template(self) -> Template | None:
        vendor = self.in_vendor.text().strip()
        doc_type = self.in_doctype.currentText().strip()
        if not vendor or not doc_type:
            QMessageBox.warning(self, "Missing details",
                                "Vendor name and document type are required.")
            return None
        if not self.field_defs:
            QMessageBox.warning(self, "No fields", "Define at least one field.")
            return None

        fingerprint = (build_fingerprint(self.doc, vendor) if self.doc
                       else {"required_tokens": [vendor]})
        gstin = self.in_gstin.text().strip().upper()
        names = {f["name"] for f in self.field_defs}
        checks = []
        if {"subtotal", "grand_total", "tax_total"} <= names:
            checks.append({
                "id": "grand_total",
                "expr": "abs(subtotal + tax_total - grand_total) <= "
                        "max(1.0, 0.005 * grand_total)",
                "on_fail": "review"})

        # _display_bbox / _display_page exist only so this editor can redraw
        # and correct a field - meaningless to the extraction engine, and
        # would just be clutter in the saved file. Strip them here, at the
        # one place every save path passes through, rather than remembering
        # to do it wherever a field spec gets built.
        clean_fields = [{k: v for k, v in f.items() if not k.startswith("_")}
                        for f in self.field_defs]

        raw = {
            "schema_version": 1,
            "template_id": make_template_id(vendor, doc_type),
            "version": 1,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "vendor": {"display_name": vendor, "slug": slugify(vendor),
                       "aliases": [],
                       "identifiers": {"gstin": gstin or None}},
            "document_type": doc_type.lower().replace(" ", "_"),
            "dayfirst": self.settings.dayfirst,
            "date_formats": list(self.settings.date_formats),
            "fingerprint": fingerprint,
            "fields": clean_fields,
            "tables": [],
            "cross_checks": checks,
            "output": {"sheet_group": doc_type.lower().replace(" ", "_"),
                       "column_order": [f["name"] for f in self.field_defs]},
        }
        try:
            return validate(raw)
        except Exception as exc:
            QMessageBox.critical(self, "Invalid template", str(exc))
            return None

    def test_samples(self) -> None:
        tpl = self._build_template()
        if tpl is None:
            return
        self.table_results.setRowCount(0)
        settings = {"date_formats": tpl.date_formats,
                    "dayfirst": tpl.dayfirst,
                    "confidence_floor": self.settings.confidence_floor}
        for path in self.sample_paths:
            doc = load_pdf(path, max_pages=self.settings.max_pages,
                           ocr_enabled=self.settings.ocr_enabled,
                           ocr_dpi=self.settings.ocr_dpi)
            if doc.load_error:
                self._add_row(path.name, "ERROR", doc.load_error)
                continue
            result = extract_document(doc, tpl.to_dict(), match_score=1.0,
                                      settings=settings, doc_id=path.stem)
            issues = [f"{f.name}={f.status.value}"
                      for f in result.fields.values()
                      if f.status.value not in ("EXTRACTED", "MISSING")]
            if issues:
                self._add_row(path.name, "REVIEW", "; ".join(issues[:4]))
            else:
                values = ", ".join(f"{n}={f.display()}"
                                   for n, f in list(result.fields.items())[:3])
                self._add_row(path.name, "PASS", values)

    def _add_row(self, sample: str, status: str, detail: str) -> None:
        row = self.table_results.rowCount()
        self.table_results.insertRow(row)
        self.table_results.setItem(row, 0, QTableWidgetItem(sample))
        self.table_results.setItem(row, 1, QTableWidgetItem(status))
        self.table_results.setItem(row, 2, QTableWidgetItem(detail))

    def save(self) -> None:
        tpl = self._build_template()
        if tpl is None:
            return
        if len(self.sample_paths) < 3:
            answer = QMessageBox.question(
                self, "Only one or two samples",
                "A template validated against fewer than three documents is a "
                "guess. It will very likely fail on the next invoice.\n\n"
                "Save anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        path = self.repo.save(tpl, bump_version=True)
        samples_dir = path.parent / "samples"
        samples_dir.mkdir(exist_ok=True)
        for src in self.sample_paths:
            try:
                shutil.copy(src, samples_dir / src.name)
            except OSError:
                pass

        QMessageBox.information(
            self, "Template saved",
            f"{tpl.template_id} v{tpl.version}\n\nSaved to:\n{path}\n\n"
            f"{len(self.sample_paths)} sample(s) stored for regression testing.")
        self.saved.emit(tpl.template_id)
