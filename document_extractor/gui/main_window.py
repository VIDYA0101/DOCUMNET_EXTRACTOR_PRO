"""Main window: sidebar navigation over stacked screens.

The GUI thread never touches a PDF. A supervisor QThread owns the process pool
and emits throttled progress; signalling per file from six workers floods the
Qt event queue and freezes the UI, which is a particularly annoying bug to
diagnose because the freeze is caused by the progress bar.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSpinBox,
                               QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QTextEdit, QVBoxLayout,
                               QWidget)
from PySide6.QtCore import QUrl

from ..app.batch import APP_VERSION, BatchRunner, RunConfig, discover_pdfs
from ..domain.export.workbook import write_outputs
from ..domain.template.repository import TemplateRepository
from ..infra.store import AtomicJson, MemoryIndex
from .editor.template_editor import TemplateEditor

log = logging.getLogger(__name__)

STYLE = """
QMainWindow, QWidget { background: #FFFFFF; color: #1B2733; }
#Sidebar { background: #1F3B57; }
#Sidebar QPushButton {
    background: transparent; color: #C8D6E5; border: none;
    text-align: left; padding: 11px 18px; font-size: 13px;
}
#Sidebar QPushButton:hover { background: #2A4A6B; color: #FFFFFF; }
#Sidebar QPushButton:checked { background: #14293D; color: #FFFFFF;
    border-left: 3px solid #4FA3E3; font-weight: 600; }
#Title { font-size: 19px; font-weight: 600; color: #1F3B57; }
QPushButton { background: #EDF2F7; border: 1px solid #CBD5E0;
    border-radius: 4px; padding: 6px 14px; }
QPushButton:hover { background: #E2E8F0; }
QPushButton[primary="true"] { background: #1F3B57; color: white;
    border: 1px solid #1F3B57; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #2A4A6B; }
QTableWidget { gridline-color: #E2E8F0; }
QHeaderView::section { background: #F2F5F8; padding: 5px;
    border: none; border-bottom: 1px solid #CBD5E0; font-weight: 600; }
QGroupBox { border: 1px solid #DDE4EC; border-radius: 5px;
    margin-top: 10px; padding-top: 8px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
"""


class BatchWorker(QObject):
    """Runs the batch off the GUI thread and reports throttled progress."""
    progress = Signal(int, int, int, int, int, str)   # done,total,ok,rev,fail,current
    finished = Signal(object, object)                 # results, output paths
    failed = Signal(str)

    def __init__(self, config: RunConfig, templates, memory_index, resume=False):
        super().__init__()
        self.config = config
        self.templates = templates
        self.memory_index = memory_index
        self.resume = resume
        self.runner: BatchRunner | None = None
        self._last_emit = 0.0

    def cancel(self) -> None:
        if self.runner:
            self.runner.cancel()

    def run(self) -> None:
        try:
            def on_progress(p):
                now = time.monotonic()
                if now - self._last_emit < 0.1 and p.done < p.total:
                    return
                self._last_emit = now
                self.progress.emit(p.done, p.total, p.ok, p.review, p.failed,
                                   p.current)

            self.runner = BatchRunner(self.config, self.templates,
                                      on_progress=on_progress,
                                      memory_index=self.memory_index)
            results = self.runner.run(resume=self.resume)
            manifest = AtomicJson.read(self.runner.manifest_path, {}) or {}
            out_dir = Path(self.config.data_root) / "Output" / self.config.run_id
            from ..domain.export.workbook import collect_export_fields
            paths = write_outputs(
                results, manifest, out_dir,
                include_field_detail=self.config.include_field_detail,
                selected_fields=collect_export_fields(results, self.templates))
            self.finished.emit(results, paths)
        except Exception as exc:
            log.exception("batch failed")
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, data_root, settings, repo: TemplateRepository,
                 memory_index: MemoryIndex):
        super().__init__()
        self.data_root = data_root
        self.settings = settings
        self.repo = repo
        self.memory_index = memory_index
        self.results = []
        self.output_paths = {}
        self.thread: QThread | None = None
        self.worker: BatchWorker | None = None

        self.setWindowTitle(f"Document Extractor {APP_VERSION}")
        self.resize(1320, 860)
        self.setStyleSheet(STYLE)
        self._build()
        self.refresh_all()

        self._heartbeat = QTimer(self)
        self._heartbeat.timeout.connect(self._tick)
        self._heartbeat.start(30_000)

    # -- shell -------------------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(200)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 16, 0, 16)
        sl.setSpacing(2)

        brand = QLabel("  Document\n  Extractor")
        brand.setStyleSheet("color:#FFFFFF;font-size:16px;font-weight:600;"
                            "padding:8px 14px 18px 14px;")
        sl.addWidget(brand)

        self.stack = QStackedWidget()
        self.nav_buttons = []
        pages = [("Dashboard", self._page_dashboard),
                 ("Template Library", self._page_library),
                 ("Create Template", self._page_create),
                 ("Process Documents", self._page_process),
                 ("Results", self._page_results),
                 ("Review", self._page_review),
                 ("Settings", self._page_settings)]
        for index, (name, factory) in enumerate(pages):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, i=index: self.go(i))
            sl.addWidget(btn)
            self.nav_buttons.append(btn)
            self.stack.addWidget(factory())
        sl.addStretch(1)

        self.lbl_root = QLabel(f"  {self.data_root.root}")
        self.lbl_root.setWordWrap(True)
        self.lbl_root.setStyleSheet("color:#7F97AD;font-size:10px;padding:8px;")
        sl.addWidget(self.lbl_root)

        layout.addWidget(sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.go(0)

    def go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)
        if index == 1:
            self.refresh_library()
        elif index == 3:
            self.refresh_template_combo()

    @staticmethod
    def _titled(title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        heading = QLabel(title)
        heading.setObjectName("Title")
        layout.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setStyleSheet("color:#5B6B7B;padding-bottom:6px;")
            sub.setWordWrap(True)
            layout.addWidget(sub)
        return page, layout

    # -- pages -------------------------------------------------------------
    def _page_dashboard(self) -> QWidget:
        page, layout = self._titled("Dashboard")
        self.cards = QLabel()
        self.cards.setStyleSheet("font-size:13px;padding:14px;background:#F2F5F8;"
                                 "border:1px solid #DDE4EC;border-radius:5px;")
        layout.addWidget(self.cards)

        self.lbl_drift = QLabel()
        self.lbl_drift.setWordWrap(True)
        self.lbl_drift.setStyleSheet("padding:10px;")
        layout.addWidget(self.lbl_drift)

        row = QHBoxLayout()
        for text, slot in [("Create a template", lambda: self.go(2)),
                           ("Process documents", lambda: self.go(3)),
                           ("Open data folder", self.open_data_folder)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _page_library(self) -> QWidget:
        page, layout = self._titled(
            "Template Library",
            "Validate re-runs each template's stored samples and compares them "
            "against the recorded baseline, so an edit cannot silently break "
            "documents that already worked.")
        self.tbl_templates = QTableWidget(0, 6)
        self.tbl_templates.setHorizontalHeaderLabels(
            ["Template", "Vendor", "Type", "Version", "Fields", "Samples"])
        self.tbl_templates.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.tbl_templates.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.tbl_templates, 1)

        row = QHBoxLayout()
        for text, slot in [("Validate selected", self.validate_selected),
                           ("Record baselines", self.record_baselines),
                           ("Export…", self.export_template),
                           ("Import…", self.import_template),
                           ("Reload from disk", self.refresh_library)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)

        self.txt_validation = QTextEdit()
        self.txt_validation.setReadOnly(True)
        self.txt_validation.setMaximumHeight(190)
        layout.addWidget(self.txt_validation)
        return page

    def _page_create(self) -> QWidget:
        page, layout = self._titled("Create Template")
        self.editor = TemplateEditor(self.repo, self.settings)
        self.editor.saved.connect(lambda _: (self.refresh_all(), self.go(1)))
        layout.addWidget(self.editor, 1)
        return page

    def _page_process(self) -> QWidget:
        page, layout = self._titled("Process Documents")

        box = QGroupBox("Batch")
        form = QFormLayout(box)
        folder_row = QHBoxLayout()
        self.lbl_input = QLabel(str(self.data_root.input))
        btn_browse = QPushButton("Choose folder…")
        btn_browse.clicked.connect(self.choose_input)
        folder_row.addWidget(self.lbl_input, 1)
        folder_row.addWidget(btn_browse)
        form.addRow("Input folder", folder_row)

        self.cmb_template = QComboBox()
        form.addRow("Template", self.cmb_template)
        self.chk_recursive = QCheckBox("Include subfolders")
        self.chk_recursive.setChecked(True)
        self.chk_resume = QCheckBox("Resume the last interrupted run")
        form.addRow("", self.chk_recursive)
        form.addRow("", self.chk_resume)
        layout.addWidget(box)

        row = QHBoxLayout()
        self.btn_start = QPushButton("Start processing")
        self.btn_start.setProperty("primary", True)
        self.btn_start.clicked.connect(self.start_batch)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_batch)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        self.lbl_progress = QLabel("Idle")
        layout.addWidget(self.lbl_progress)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Consolas", 9))
        layout.addWidget(self.txt_log, 1)
        return page

    def _page_results(self) -> QWidget:
        page, layout = self._titled("Results")
        self.tbl_results = QTableWidget(0, 8)
        self.tbl_results.setHorizontalHeaderLabels(
            ["File", "Template", "Status", "Score", "Fields OK", "Lines",
             "Reasons", "ms"])
        self.tbl_results.horizontalHeader().setSectionResizeMode(
            6, QHeaderView.Stretch)
        self.tbl_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self.tbl_results, 1)

        row = QHBoxLayout()
        for text, slot in [("Open full report", self.open_output),
                           ("Open fields-only file", self.open_fields_output),
                           ("Open report", self.open_report),
                           ("Open output folder", self.open_output_folder)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _page_review(self) -> QWidget:
        page, layout = self._titled(
            "Review",
            "Documents where a value was uncertain, ambiguous, or failed a "
            "cross-check. Nothing here was guessed.")
        splitter = QSplitter(Qt.Horizontal)
        self.list_review = QListWidget()
        self.list_review.currentRowChanged.connect(self.show_review_item)
        splitter.addWidget(self.list_review)
        self.txt_review = QTextEdit()
        self.txt_review.setReadOnly(True)
        self.txt_review.setFont(QFont("Consolas", 9))
        splitter.addWidget(self.txt_review)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        return page

    def _page_settings(self) -> QWidget:
        page, layout = self._titled("Settings")
        box = QGroupBox("Processing")
        form = QFormLayout(box)
        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(0, 32)
        self.sp_workers.setValue(self.settings.workers)
        self.sp_workers.setSpecialValueText("automatic")
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(10, 3600)
        self.sp_timeout.setValue(self.settings.per_file_timeout_s)
        self.sp_floor = QDoubleSpinBox()
        self.sp_floor.setRange(0.0, 1.0)
        self.sp_floor.setSingleStep(0.05)
        self.sp_floor.setValue(self.settings.confidence_floor)
        self.sp_match = QDoubleSpinBox()
        self.sp_match.setRange(0.0, 1.0)
        self.sp_match.setSingleStep(0.05)
        self.sp_match.setValue(self.settings.template_min_score)
        self.chk_dayfirst = QCheckBox("Dates are day-first (DD/MM/YYYY)")
        self.chk_dayfirst.setChecked(self.settings.dayfirst)
        self.chk_detail = QCheckBox("Include the FieldDetail sheet in Excel")
        self.chk_detail.setChecked(self.settings.include_field_detail_sheet)
        form.addRow("Worker processes", self.sp_workers)
        form.addRow("Per-file timeout (s)", self.sp_timeout)
        form.addRow("Confidence floor", self.sp_floor)
        form.addRow("Template min score", self.sp_match)
        form.addRow("", self.chk_dayfirst)
        form.addRow("", self.chk_detail)
        layout.addWidget(box)

        ocr_box = QGroupBox("OCR (for scanned documents)")
        ocr_form = QFormLayout(ocr_box)
        self.chk_ocr = QCheckBox("Enable OCR for pages with no text layer")
        self.chk_ocr.setChecked(self.settings.ocr_enabled)
        self.sp_ocr_dpi = QSpinBox()
        self.sp_ocr_dpi.setRange(150, 600)
        self.sp_ocr_dpi.setSingleStep(50)
        self.sp_ocr_dpi.setValue(self.settings.ocr_dpi)
        self.lbl_ocr_status = QLabel()
        self.lbl_ocr_status.setWordWrap(True)
        btn_ocr_check = QPushButton("Check now")
        btn_ocr_check.clicked.connect(self.refresh_ocr_status)
        ocr_status_row = QHBoxLayout()
        ocr_status_row.addWidget(self.lbl_ocr_status, 1)
        ocr_status_row.addWidget(btn_ocr_check)
        ocr_form.addRow("", self.chk_ocr)
        ocr_form.addRow("Scan resolution (DPI)", self.sp_ocr_dpi)
        ocr_form.addRow("Tesseract", ocr_status_row)
        ocr_note = QLabel(
            "OCR only ever runs on a page that has no usable text layer - it "
            "never touches a normal digital PDF, and never slows one down. "
            "Tesseract is a separate, free program this application does "
            "not install for you.")
        ocr_note.setWordWrap(True)
        ocr_note.setStyleSheet("color:#5B6B7B;font-size:11px;")
        ocr_form.addRow(ocr_note)
        layout.addWidget(ocr_box)
        self.refresh_ocr_status()

        warn = QLabel("Lowering the confidence floor trades accuracy for "
                      "throughput: fewer documents reach Review, and more "
                      "uncertain values are exported as if they were clean.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#8A5A00;background:#FFF6DF;padding:8px;"
                           "border:1px solid #E8CE8A;border-radius:4px;")
        layout.addWidget(warn)

        row = QHBoxLayout()
        b_save = QPushButton("Save settings")
        b_save.setProperty("primary", True)
        b_save.clicked.connect(self.save_settings)
        b_rebuild = QPushButton("Rebuild index cache")
        b_rebuild.clicked.connect(self.rebuild_index)
        b_logs = QPushButton("Open logs folder")
        b_logs.clicked.connect(lambda: self._open(self.data_root.logs))
        for b in (b_save, b_rebuild, b_logs):
            row.addWidget(b)
        row.addStretch(1)
        layout.addLayout(row)

        info = QLabel(
            f"Version {APP_VERSION}\nData root: {self.data_root.root}\n"
            "This application makes no network connections.")
        info.setStyleSheet("color:#5B6B7B;padding-top:12px;")
        layout.addWidget(info)
        layout.addStretch(1)
        return page

    # -- actions -----------------------------------------------------------
    def refresh_all(self) -> None:
        self.repo.load_all()
        self.refresh_library()
        self.refresh_template_combo()
        self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        templates = self.repo.all()
        self.cards.setText(
            f"Templates: {len(templates)}          "
            f"Documents in history: {len(self.memory_index.seen_hashes)}          "
            f"Invalid template files: {len(self.repo.errors)}")
        drift = []
        for tpl in templates:
            base = self.memory_index.baseline(tpl.template_id)
            if base and base["field_success"] < 0.85:
                drift.append(f"{tpl.template_id}: field success has fallen to "
                             f"{base['field_success']:.0%} over the last "
                             f"{base['runs']} runs — the vendor's layout may "
                             f"have changed.")
        self.lbl_drift.setText("\n".join(drift) if drift
                               else "No layout drift detected.")

    def refresh_library(self) -> None:
        self.tbl_templates.setRowCount(0)
        for tpl in self.repo.all():
            row = self.tbl_templates.rowCount()
            self.tbl_templates.insertRow(row)
            values = [tpl.template_id, tpl.vendor.display_name, tpl.document_type,
                      str(tpl.version), str(len(tpl.fields)),
                      str(len(self.repo.samples(tpl.template_id)))]
            for col, value in enumerate(values):
                self.tbl_templates.setItem(row, col, QTableWidgetItem(value))
        if self.repo.errors:
            self.txt_validation.setPlainText(
                "Invalid template files (skipped):\n" +
                "\n".join(f"  {rel}: {err}" for rel, err in self.repo.errors))

    def refresh_template_combo(self) -> None:
        current = self.cmb_template.currentText()
        self.cmb_template.clear()
        self.cmb_template.addItem("Automatic (match each document)")
        for tpl in self.repo.all():
            self.cmb_template.addItem(tpl.template_id)
        if current:
            index = self.cmb_template.findText(current)
            if index >= 0:
                self.cmb_template.setCurrentIndex(index)

    def _selected_template_id(self) -> str | None:
        row = self.tbl_templates.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select a template",
                                    "Choose a template in the list first.")
            return None
        return self.tbl_templates.item(row, 0).text()

    def _extract_fn(self):
        from ..domain.extraction.engine import extract_document
        from ..domain.pdf.text_layer import load_pdf

        def run(pdf_path, tpl):
            doc = load_pdf(pdf_path, max_pages=self.settings.max_pages,
                           ocr_enabled=self.settings.ocr_enabled,
                           ocr_dpi=self.settings.ocr_dpi)
            return extract_document(
                doc, tpl.to_dict(), match_score=1.0,
                settings={"date_formats": tpl.date_formats or
                          self.settings.date_formats,
                          "dayfirst": tpl.dayfirst,
                          "confidence_floor": self.settings.confidence_floor},
                doc_id=Path(pdf_path).stem)
        return run

    def validate_selected(self) -> None:
        template_id = self._selected_template_id()
        if not template_id:
            return
        report = self.repo.validate_template(template_id, self._extract_fn())
        lines = [f"{template_id}: {report.get('passed',0)} passed, "
                 f"{report.get('failed',0)} failed, "
                 f"{report.get('no_baseline',0)} without a baseline", ""]
        for s in report.get("samples", []):
            lines.append(f"  [{s['status']}] {s['sample']}  {s['detail']}")
        self.txt_validation.setPlainText("\n".join(lines))

    def record_baselines(self) -> None:
        template_id = self._selected_template_id()
        if not template_id:
            return
        tpl = self.repo.get(template_id)
        extract = self._extract_fn()
        count = 0
        for pdf in self.repo.samples(template_id):
            self.repo.write_expected(pdf, extract(pdf, tpl))
            count += 1
        self.txt_validation.setPlainText(
            f"Recorded {count} baseline(s) for {template_id}.\n"
            "Future edits to this template are now checked against these.")

    def export_template(self) -> None:
        template_id = self._selected_template_id()
        if not template_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export template",
                                              f"{template_id}.json",
                                              "JSON (*.json)")
        if path:
            self.repo.export(template_id, Path(path))

    def import_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import template", "",
                                              "JSON (*.json)")
        if not path:
            return
        answer = QMessageBox.question(
            self, "Imported template",
            "This template came from outside this machine. Its patterns and "
            "cross-checks will be validated, but review them before relying "
            "on the output.\n\nImport it?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        try:
            tpl = self.repo.import_file(Path(path))
            self.refresh_all()
            QMessageBox.information(self, "Imported", f"Imported {tpl.template_id}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not import", str(exc))

    def choose_input(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose input folder",
                                                  str(self.data_root.input))
        if folder:
            self.lbl_input.setText(folder)

    def start_batch(self) -> None:
        if self.thread is not None:
            return
        folder = self.lbl_input.text()
        files = discover_pdfs(folder, self.chk_recursive.isChecked())
        if not files:
            QMessageBox.warning(self, "No PDFs", f"No PDF files found in\n{folder}")
            return
        templates = self.repo.all()
        if not templates:
            QMessageBox.warning(self, "No templates",
                                "Create a template before processing.")
            return

        choice = self.cmb_template.currentText()
        template_id = None if choice.startswith("Automatic") else choice

        config = RunConfig(
            run_id=RunConfig.new_run_id(), input_dir=folder,
            data_root=str(self.data_root.root), template_id=template_id,
            workers=self.settings.worker_count(),
            per_file_timeout_s=self.settings.per_file_timeout_s,
            max_pages=self.settings.max_pages,
            max_file_mb=self.settings.max_file_mb,
            confidence_floor=self.settings.confidence_floor,
            template_min_score=self.settings.template_min_score,
            template_review_below=self.settings.template_review_below,
            template_margin=self.settings.template_margin,
            dayfirst=self.settings.dayfirst,
            date_formats=self.settings.date_formats,
            include_field_detail=self.settings.include_field_detail_sheet,
            ocr_enabled=self.settings.ocr_enabled,
            ocr_dpi=self.settings.ocr_dpi,
            recursive=self.chk_recursive.isChecked())

        self.txt_log.clear()
        self.txt_log.append(f"Run {config.run_id}")
        self.txt_log.append(f"{len(files)} PDFs, {len(templates)} templates, "
                            f"{config.workers} workers")
        self.progress.setMaximum(len(files))
        self.progress.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        self.thread = QThread(self)
        self.worker = BatchWorker(config, templates, self.memory_index,
                                  resume=self.chk_resume.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.thread.start()

    def cancel_batch(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.txt_log.append("Cancelling after the current files finish…")

    def on_progress(self, done, total, ok, review, failed, current) -> None:
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.lbl_progress.setText(
            f"{done}/{total}   clean {ok}   review {review}   failed {failed}"
            f"   —   {current}")

    def on_finished(self, results, paths) -> None:
        self.results = results
        self.output_paths = paths
        self._teardown_thread()
        self.txt_log.append(f"\nFinished. {len(results)} documents.")
        self.txt_log.append(f"Full report   : {paths.get('xlsx')}")
        if paths.get('fields_xlsx'):
            self.txt_log.append(f"Fields only   : {paths['fields_xlsx']}")
        self.txt_log.append(f"HTML report   : {paths.get('report')}")
        self.populate_results()
        self.populate_review()
        self.refresh_dashboard()
        self.go(4)

    def on_failed(self, message: str) -> None:
        self._teardown_thread()
        QMessageBox.critical(self, "Batch failed",
                             f"{message}\n\nSee the log at:\n"
                             f"{self.data_root.logs / 'app.log'}")

    def _teardown_thread(self) -> None:
        if self.thread:
            self.thread.quit()
            self.thread.wait(5000)
            self.thread = None
        self.worker = None
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)

    def populate_results(self) -> None:
        self.tbl_results.setRowCount(0)
        for r in self.results:
            row = self.tbl_results.rowCount()
            self.tbl_results.insertRow(row)
            values = [r.source_name, r.template_id, r.status.value,
                      f"{r.match_score:.2f}",
                      f"{sum(1 for f in r.fields.values() if f.ok)}/{len(r.fields)}",
                      str(len(r.line_items)), ", ".join(r.all_reasons()[:3]),
                      str(r.duration_ms)]
            for col, value in enumerate(values):
                self.tbl_results.setItem(row, col, QTableWidgetItem(value))

    def populate_review(self) -> None:
        self.list_review.clear()
        self._review = [r for r in self.results if r.status.value != "OK"]
        for r in self._review:
            self.list_review.addItem(f"{r.source_name}  [{r.status.value}]")

    def show_review_item(self, index: int) -> None:
        if index < 0 or index >= len(getattr(self, "_review", [])):
            return
        r = self._review[index]
        lines = [f"File     : {r.source_name}",
                 f"Template : {r.template_id} v{r.template_version}",
                 f"Match    : {r.match_score:.3f} (margin {r.match_margin:.3f})",
                 f"Status   : {r.status.value}", ""]
        for name, f in r.fields.items():
            marker = " " if f.status.value == "EXTRACTED" else ">"
            lines.append(f"{marker} {name:<18} {f.status.value:<15} "
                         f"{f.display()[:40]:<42} {f.confidence:.2f}  "
                         f"{', '.join(f.reasons)}")
            if f.candidates:
                lines.append(f"      candidates: {', '.join(f.candidates)}")
        lines.append("")
        for c in r.cross_checks:
            lines.append(f"[{'PASS' if c.passed else 'FAIL'}] {c.check_id}: "
                         f"{c.detail}")
        self.txt_review.setPlainText("\n".join(lines))

    def save_settings(self) -> None:
        self.settings.workers = self.sp_workers.value()
        self.settings.per_file_timeout_s = self.sp_timeout.value()
        self.settings.confidence_floor = self.sp_floor.value()
        self.settings.template_min_score = self.sp_match.value()
        self.settings.dayfirst = self.chk_dayfirst.isChecked()
        self.settings.include_field_detail_sheet = self.chk_detail.isChecked()
        self.settings.ocr_enabled = self.chk_ocr.isChecked()
        self.settings.ocr_dpi = self.sp_ocr_dpi.value()
        self.settings.save(self.data_root.settings_file)
        QMessageBox.information(self, "Saved", "Settings saved.")

    def refresh_ocr_status(self) -> None:
        from ..domain.pdf.ocr import find_tesseract, is_available
        if is_available():
            self.lbl_ocr_status.setText(f"Found: {find_tesseract()}")
            self.lbl_ocr_status.setStyleSheet("color:#2F8F5B;")
        else:
            self.lbl_ocr_status.setText(
                "Not found. OCR will be skipped even if enabled above.")
            self.lbl_ocr_status.setStyleSheet("color:#C7522A;")

    def rebuild_index(self) -> None:
        self.memory_index.rebuild()
        self.refresh_dashboard()
        QMessageBox.information(self, "Rebuilt",
                                "The index cache was rebuilt from the logs.")

    def _tick(self) -> None:
        self.memory_index.flush()

    # -- opening things ----------------------------------------------------
    @staticmethod
    def _open(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_data_folder(self) -> None:
        self._open(self.data_root.root)

    def open_output(self) -> None:
        if self.output_paths.get("xlsx"):
            self._open(self.output_paths["xlsx"])

    def open_fields_output(self) -> None:
        if self.output_paths.get("fields_xlsx"):
            self._open(self.output_paths["fields_xlsx"])
        else:
            QMessageBox.information(
                self, "No fields-only file",
                "This run's templates had no fields ticked for export, so "
                "only the full report was produced. Tick fields in Create "
                "Template and reprocess to get this file too.")

    def open_report(self) -> None:
        if self.output_paths.get("report"):
            self._open(self.output_paths["report"])

    def open_output_folder(self) -> None:
        self._open(self.data_root.output)

    # -- shutdown ----------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.thread is not None:
            answer = QMessageBox.question(
                self, "A batch is running",
                "Processing is still in progress. Cancel it and close?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.cancel_batch()
            self._teardown_thread()
        try:
            self.memory_index.close()
        except Exception:
            pass
        event.accept()
