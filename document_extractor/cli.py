"""Headless command line. Same engine as the GUI, no Qt required.

Useful for scheduled runs, CI regression of templates, and diagnosing a
production problem on a machine where the GUI will not start.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .app.batch import APP_VERSION, BatchRunner, RunConfig, discover_pdfs
from .domain.export.workbook import write_outputs
from .domain.template.repository import TemplateRepository
from .infra import logging_setup
from .infra.paths import DataRoot, resolve_data_root
from .infra.settings import Settings
from .infra.store import AtomicJson, MemoryIndex


def _bootstrap(data_root: str | None):
    root = DataRoot(resolve_data_root(data_root)).ensure()
    settings = Settings.load(root.settings_file)
    logging_setup.setup(root.logs, settings.log_level)
    root.sweep_tmp()
    return root, settings


def cmd_process(args) -> int:
    root, settings = _bootstrap(args.data_root)
    repo = TemplateRepository(root.templates)
    repo.load_all()
    for rel, err in repo.errors:
        print(f"  ! invalid template {rel}: {err}", file=sys.stderr)

    templates = repo.all()
    if not templates:
        print(f"No templates found in {root.templates}", file=sys.stderr)
        return 2

    files = discover_pdfs(args.input, recursive=not args.no_recursive)
    if not files:
        print(f"No PDFs found in {args.input}", file=sys.stderr)
        return 2

    index = MemoryIndex(root.index, root.cache)
    index.load()

    cfg = RunConfig(
        run_id=args.run_id or RunConfig.new_run_id(),
        input_dir=str(args.input), data_root=str(root.root),
        template_id=args.template, workers=args.workers or settings.worker_count(),
        per_file_timeout_s=settings.per_file_timeout_s,
        max_pages=settings.max_pages, max_file_mb=settings.max_file_mb,
        confidence_floor=settings.confidence_floor,
        template_min_score=settings.template_min_score,
        template_review_below=settings.template_review_below,
        template_margin=settings.template_margin,
        dayfirst=settings.dayfirst, date_formats=settings.date_formats,
        include_field_detail=settings.include_field_detail_sheet,
        ocr_enabled=settings.ocr_enabled, ocr_dpi=settings.ocr_dpi,
        recursive=not args.no_recursive)

    print(f"Run {cfg.run_id}: {len(files)} PDFs, {len(templates)} templates, "
          f"{cfg.workers} workers")
    started = time.time()
    last = [0.0]

    def on_progress(p):
        now = time.time()
        if now - last[0] < 0.2 and p.done < p.total:
            return                      # throttle: never flood the console
        last[0] = now
        p.elapsed_s = now - started
        pct = 100 * p.done / max(p.total, 1)
        sys.stdout.write(
            f"\r  {p.done}/{p.total} ({pct:5.1f}%)  ok={p.ok} "
            f"review={p.review} failed={p.failed}  {p.rate:.1f}/s   ")
        sys.stdout.flush()

    runner = BatchRunner(cfg, templates, on_progress=on_progress,
                         memory_index=index)
    results = runner.run(files, resume=args.resume)
    print()

    manifest = AtomicJson.read(runner.manifest_path, {}) or {}
    manifest["column_order"] = []
    out_dir = root.output / cfg.run_id
    from .domain.export.workbook import collect_export_fields
    paths = write_outputs(results, manifest, out_dir,
                          include_field_detail=cfg.include_field_detail,
                          selected_fields=collect_export_fields(results, templates))
    index.close()

    p = runner.progress
    print(f"\nDone in {time.time() - started:.1f}s")
    print(f"  clean        : {p.ok}")
    print(f"  needs review : {p.review}")
    print(f"  failed       : {p.failed}")
    print(f"\n  {paths['xlsx']}")
    print(f"  {paths['report']}")
    return 0 if p.failed == 0 else 1


def cmd_validate(args) -> int:
    """Re-run every golden sample and diff against the recorded expectation."""
    root, settings = _bootstrap(args.data_root)
    repo = TemplateRepository(root.templates)
    repo.load_all()

    from .domain.extraction.engine import extract_document
    from .domain.pdf.text_layer import load_pdf

    def extract(pdf_path, tpl):
        doc = load_pdf(pdf_path, max_pages=settings.max_pages,
                       ocr_enabled=settings.ocr_enabled, ocr_dpi=settings.ocr_dpi)
        return extract_document(
            doc, tpl.to_dict(), match_score=1.0,
            settings={"date_formats": tpl.date_formats or settings.date_formats,
                      "dayfirst": tpl.dayfirst,
                      "confidence_floor": settings.confidence_floor},
            doc_id=Path(pdf_path).stem)

    targets = [args.template] if args.template else [t.template_id for t in repo.all()]
    failures = 0
    for template_id in targets:
        report = repo.validate_template(template_id, extract)
        mark = "PASS" if report.get("ok") else "FAIL"
        print(f"\n[{mark}] {template_id}: {report.get('passed', 0)} passed, "
              f"{report.get('failed', 0)} failed, "
              f"{report.get('no_baseline', 0)} without baseline")
        for s in report.get("samples", []):
            if s["status"] != "PASS":
                print(f"    {s['status']:<12} {s['sample']}: {s['detail']}")
        failures += report.get("failed", 0)
    return 1 if failures else 0


def cmd_record(args) -> int:
    """Record the current extraction of each sample as its golden baseline."""
    root, settings = _bootstrap(args.data_root)
    repo = TemplateRepository(root.templates)
    repo.load_all()

    from .domain.extraction.engine import extract_document
    from .domain.pdf.text_layer import load_pdf

    tpl = repo.get(args.template)
    if tpl is None:
        print(f"Template not found: {args.template}", file=sys.stderr)
        return 2
    n = 0
    for pdf in repo.samples(args.template):
        doc = load_pdf(pdf, max_pages=settings.max_pages,
                       ocr_enabled=settings.ocr_enabled, ocr_dpi=settings.ocr_dpi)
        result = extract_document(
            doc, tpl.to_dict(), match_score=1.0,
            settings={"date_formats": tpl.date_formats or settings.date_formats,
                      "dayfirst": tpl.dayfirst,
                      "confidence_floor": settings.confidence_floor},
            doc_id=pdf.stem)
        repo.write_expected(pdf, result)
        n += 1
        print(f"  recorded {pdf.name}: {len(result.fields)} fields, "
              f"{len(result.line_items)} lines")
    print(f"Recorded {n} baselines for {args.template}")
    return 0


def cmd_info(args) -> int:
    root, settings = _bootstrap(args.data_root)
    repo = TemplateRepository(root.templates)
    repo.load_all()
    print(f"DocumentExtractor {APP_VERSION}")
    print(f"Data root : {root.root}")
    print(f"Templates : {len(repo.all())}")
    for t in repo.all():
        print(f"   {t.template_id:<40} v{t.version}  "
              f"{len(t.fields)} fields, {len(t.tables)} table(s)")
    for rel, err in repo.errors:
        print(f"   ! {rel}: {err}")
    index = MemoryIndex(root.index, root.cache)
    index.load()
    print(f"History   : {len(index.seen_hashes)} documents, "
          f"{len(index.invoice_keys)} invoice keys")
    index.close()
    return 0


def cmd_selftest(args) -> int:
    """Exercise the exact import and execution paths the GUI uses.

    This exists because "the imports succeed" and "the packaged EXE works" are
    different claims. A missing native library (pyexpat, pypdfium2's engine,
    a Qt platform plugin) only shows up when the real code path that touches
    it actually runs - which, for the GUI, includes constructing a real
    QApplication. Running this against the frozen EXE, not just the source
    tree, is what the build script now does before calling itself successful.
    """
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    print("[1/3] Importing the GUI window and its dependencies "
          "(Qt, openpyxl, pdfplumber)...")
    try:
        from PySide6.QtWidgets import QApplication

        from .gui.main_window import MainWindow  # noqa: F401
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("[2/3] Starting Qt and rendering a PDF page "
          "(exercises the pdfium native library)...")
    try:
        app = QApplication.instance() or QApplication([])
        from .domain.pdf.raster import render_page_png

        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "selftest.pdf"
            sample.write_bytes(_MINIMAL_PDF)
            png, _actual_scale = render_page_png(sample, 1, dpi=72)
            if not png:
                print("FAILED: render_page_png returned nothing", file=sys.stderr)
                return 1
        del app
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("[3/3] Writing a workbook (exercises openpyxl -> expat)...")
    try:
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as tmp:
            wb = Workbook()
            wb.active.append(["selftest", 1])
            wb.save(Path(tmp) / "selftest.xlsx")
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print("\nAll checks passed.")
    return 0


# A hand-built single-page blank PDF, small enough to embed as source. Used
# only by `selftest`, so the packaged app never needs reportlab (a dev-only
# dependency) just to prove it can render a page.
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000052 00000 n 
0000000101 00000 n 
trailer<</Size 4/Root 1 0 R>>
startxref
160
%%EOF"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="document-extractor",
                                description="Offline PDF field extraction")
    p.add_argument("--data-root", help="override the data folder")
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("process", help="process a folder of PDFs")
    pr.add_argument("input")
    pr.add_argument("--template", help="force a template instead of matching")
    pr.add_argument("--workers", type=int, default=0)
    pr.add_argument("--run-id")
    pr.add_argument("--resume", action="store_true")
    pr.add_argument("--no-recursive", action="store_true")
    pr.set_defaults(func=cmd_process)

    v = sub.add_parser("validate", help="run golden fixtures for templates")
    v.add_argument("--template")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("record", help="record golden baselines for a template")
    r.add_argument("template")
    r.set_defaults(func=cmd_record)

    i = sub.add_parser("info", help="show data root and template inventory")
    i.set_defaults(func=cmd_info)

    st = sub.add_parser("selftest",
                        help="verify the packaged build can actually run")
    st.set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("fatal")
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
