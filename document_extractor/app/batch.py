"""Batch orchestration.

PDF parsing is CPU-bound, so this uses processes, not threads. Workers write
their own result JSON and return a small status dict; passing full page models
back through the pickle boundary would dominate runtime and exhaust memory.

Only the supervisor appends to the journal. Six processes appending to one file
produce interleaved, corrupt lines.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import time
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..domain.models import DocResult, DocStatus, Reason
from ..infra.store import AppendLog, AtomicJson

log = logging.getLogger(__name__)

APP_VERSION = "1.0.1-mvp"   # bumped: fixes the _guess_vendor crash


@dataclass
class RunConfig:
    run_id: str
    input_dir: str
    data_root: str
    template_id: str | None = None       # None = automatic matching
    workers: int = 4
    per_file_timeout_s: int = 120
    max_pages: int = 200
    max_file_mb: int = 100
    confidence_floor: float = 0.80
    template_min_score: float = 0.72
    template_review_below: float = 0.85
    template_margin: float = 0.10
    dayfirst: bool = True
    date_formats: list = field(default_factory=list)
    ocr_enabled: bool = False
    ocr_dpi: int = 300
    archive_originals: bool = True
    include_field_detail: bool = False
    recursive: bool = True
    skip_duplicates: bool = True

    @staticmethod
    def new_run_id() -> str:
        return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


@dataclass
class Progress:
    total: int = 0
    done: int = 0
    ok: int = 0
    review: int = 0
    failed: int = 0
    skipped: int = 0
    current: str = ""
    elapsed_s: float = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def rate(self) -> float:
        return self.done / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def eta_s(self) -> float:
        return self.remaining / self.rate if self.rate > 0 else 0.0


def discover_pdfs(folder: str | Path, recursive: bool = True) -> list[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    globber = folder.rglob if recursive else folder.glob
    return sorted(p for p in globber("*") if p.suffix.lower() == ".pdf"
                  and p.is_file())


class BatchRunner:
    """Owns the process pool. Drives progress via a callback, not Qt signals,
    so the domain stays importable headless."""

    def __init__(self, config: RunConfig, templates: list, on_progress=None,
                 memory_index=None):
        self.config = config
        self.templates = templates
        self.on_progress = on_progress
        self.memory_index = memory_index
        self.cancelled = False
        self.paused = False
        self.results: list[DocResult] = []
        self.progress = Progress()

        root = Path(config.data_root)
        self.run_dir = root / "Work" / "runs" / config.run_id
        (self.run_dir / "results").mkdir(parents=True, exist_ok=True)
        self.journal = AppendLog(self.run_dir / "journal.jsonl")
        self.manifest_path = self.run_dir / "manifest.json"

    # -- lifecycle ---------------------------------------------------------
    def cancel(self) -> None:
        self.cancelled = True

    def completed_hashes(self) -> set[str]:
        """Read the journal to support resume. Append-only, so it survives a kill."""
        return {rec.get("sha256", "") for rec in self.journal.read_all()
                if rec.get("sha256")}

    def run(self, files: list[Path] | None = None, resume: bool = False) -> list[DocResult]:
        cfg = self.config
        files = files or discover_pdfs(cfg.input_dir, cfg.recursive)

        already: set[str] = set()
        if resume:
            already = self.completed_hashes()
            log.info("resuming run %s; %d files already journalled",
                     cfg.run_id, len(already))
            for rec in self.journal.read_all():
                loaded = self._load_result(rec.get("doc_id", ""))
                if loaded:
                    self.results.append(loaded)

        started = time.time()
        self._write_manifest(started_utc=_utc(), status="running",
                             total=len(files))

        self.progress = Progress(total=len(files), done=len(self.results))
        template_dicts = [t.to_dict() for t in self.templates]
        payload = _payload(cfg)

        pending = [f for f in files]
        pool = self._make_pool()
        try:
            index = 0
            while index < len(pending):
                if self.cancelled:
                    log.info("run %s cancelled by user", cfg.run_id)
                    break
                chunk = pending[index:index + max(1, cfg.workers * 2)]
                index += len(chunk)
                futures = {}
                for path in chunk:
                    futures[pool.submit(_process_one, str(path), template_dicts,
                                        payload, str(self.run_dir))] = path

                for future, path in futures.items():
                    if self.cancelled:
                        future.cancel()
                        continue
                    try:
                        record = future.result(timeout=cfg.per_file_timeout_s)
                    except FutureTimeout:
                        # A malformed PDF can loop forever inside a native
                        # parser. Kill the pool rather than hang the batch.
                        log.error("timeout on %s; restarting pool", path.name)
                        record = _failure_record(path, Reason.TIMEOUT.value)
                        pool.shutdown(wait=False, cancel_futures=True)
                        pool = self._make_pool()
                    except BrokenProcessPool:
                        log.error("worker died on %s; restarting pool", path.name)
                        record = _failure_record(path, Reason.WORKER_CRASHED.value)
                        pool = self._make_pool()
                    except Exception as exc:
                        log.exception("unexpected failure on %s", path.name)
                        record = _failure_record(path, Reason.EXCEPTION.value,
                                                 str(exc))
                    self._accept(record, already)
        finally:
            try:
                pool.shutdown(wait=True)
            except Exception:
                pass
            self.journal.close()

        self._write_manifest(started_utc=_utc(started), finished_utc=_utc(),
                             status="cancelled" if self.cancelled else "complete",
                             total=len(files))
        return self.results

    # -- internals ---------------------------------------------------------
    def _make_pool(self) -> ProcessPoolExecutor:
        ctx = mp.get_context("spawn")
        try:
            return ProcessPoolExecutor(max_workers=max(1, self.config.workers),
                                       mp_context=ctx,
                                       max_tasks_per_child=50)
        except TypeError:
            # max_tasks_per_child needs Python 3.11+
            return ProcessPoolExecutor(max_workers=max(1, self.config.workers),
                                       mp_context=ctx)

    def _accept(self, record: dict, already: set[str]) -> None:
        sha = record.get("sha256", "")
        if sha and sha in already:
            self.progress.skipped += 1
            return

        # The result JSON is written by the worker before this journal line, so
        # a journal entry always implies the result file exists.
        self.journal.append(record)

        status = record.get("status", DocStatus.FAILED.value)
        self.progress.done += 1
        self.progress.current = record.get("src", "")
        if status == DocStatus.OK.value:
            self.progress.ok += 1
        elif status == DocStatus.NEEDS_REVIEW.value:
            self.progress.review += 1
        else:
            self.progress.failed += 1

        loaded = self._load_result(record.get("doc_id", ""))
        if loaded:
            self.results.append(loaded)
            if self.memory_index is not None:
                inv = loaded.fields.get("invoice_number")
                self.memory_index.record_document(
                    sha256_hex=loaded.sha256, doc_id=loaded.doc_id,
                    run_id=self.config.run_id, vendor=loaded.vendor,
                    invoice_number=str(inv.value) if inv and inv.value else "")

        if self.on_progress:
            self.on_progress(self.progress)

    def _load_result(self, doc_id: str) -> DocResult | None:
        if not doc_id:
            return None
        path = self.run_dir / "results" / doc_id[:2] / f"{doc_id}.json"
        raw = AtomicJson.read(path)
        return _rehydrate(raw) if isinstance(raw, dict) else None

    def _write_manifest(self, **extra) -> None:
        cfg = asdict(self.config)
        cfg.update(extra)
        cfg["app_version"] = APP_VERSION
        cfg["column_order"] = []
        AtomicJson.write(self.manifest_path, cfg)


# ------------------------------------------------------------------ worker
def _payload(cfg: RunConfig) -> dict:
    return {
        "template_id": cfg.template_id,
        "max_pages": cfg.max_pages,
        "max_file_mb": cfg.max_file_mb,
        "confidence_floor": cfg.confidence_floor,
        "template_min_score": cfg.template_min_score,
        "template_review_below": cfg.template_review_below,
        "template_margin": cfg.template_margin,
        "dayfirst": cfg.dayfirst,
        "date_formats": cfg.date_formats,
        "run_id": cfg.run_id,
        "ocr_enabled": cfg.ocr_enabled,
        "ocr_dpi": cfg.ocr_dpi,
    }


def _process_one(path: str, template_dicts: list[dict], payload: dict,
                 run_dir: str) -> dict:
    """Runs in a worker process. Must be module-level to be picklable.

    Returns a compact journal record; the full result is written to disk here
    so it never crosses the process boundary.
    """
    import hashlib

    from ..domain.extraction.engine import extract_document
    from ..domain.matching.matcher import TemplateMatcher
    from ..domain.models import DocResult as _DR
    from ..domain.pdf.text_layer import load_pdf
    from ..domain.template.schema import validate as validate_tpl

    name = Path(path).name
    doc_id = hashlib.sha256(f"{payload['run_id']}|{path}".encode()).hexdigest()[:24]
    started = time.perf_counter()

    try:
        doc = load_pdf(path, max_pages=payload["max_pages"],
                       max_file_mb=payload["max_file_mb"],
                       ocr_enabled=payload.get("ocr_enabled", False),
                       ocr_dpi=payload.get("ocr_dpi", 300))
        result = _DR(doc_id=doc_id, source_path=path, source_name=name,
                     sha256=doc.sha256, run_id=payload["run_id"],
                     page_count=doc.page_count, processed_utc=_utc())

        if doc.load_error:
            result.error = doc.load_error
            result.reasons.append(doc.load_error)
            result.status = DocStatus.FAILED
            _write_result(run_dir, result)
            return result.journal_line()

        if doc.needs_ocr and not any(p.source.value == "text" for p in doc.pages):
            result.reasons.append(Reason.NO_TEXT_LAYER.value)
            if payload.get("ocr_enabled"):
                # OCR was attempted and the page is STILL unusable - genuinely
                # blank, or tesseract itself is not installed on this machine.
                # Different situation from OCR simply being turned off, and
                # worth a different reason code so Review doesn't read as a
                # setting the user forgot to enable when they didn't.
                from ..domain.pdf.ocr import is_available as _ocr_available
                result.reasons.append(
                    Reason.OCR_NOT_INSTALLED.value if not _ocr_available()
                    else Reason.OCR_NO_TEXT_FOUND.value)
            else:
                result.reasons.append(Reason.OCR_DISABLED.value)
            result.error = Reason.NO_TEXT_LAYER.value
            result.status = DocStatus.FAILED
            _write_result(run_dir, result)
            return result.journal_line()

        templates = [validate_tpl(t) for t in template_dicts]
        forced = payload.get("template_id")
        if forced:
            chosen = next((t for t in templates if t.template_id == forced), None)
            score, margin, runner = 1.0, 1.0, ""
            reasons: list[str] = []
        else:
            matcher = TemplateMatcher(templates, payload)
            m = matcher.match(doc)
            chosen = m.template if m.accepted else None
            score, margin, runner = m.score, m.margin, m.runner_up
            reasons = list(m.reasons)

        if chosen is None:
            result.reasons.extend(reasons or [Reason.TEMPLATE_NO_MATCH.value])
            result.match_score = score
            result.match_margin = margin
            result.runner_up = runner
            result.status = DocStatus.NEEDS_REVIEW
            _write_result(run_dir, result)
            return result.journal_line()

        settings = {"date_formats": payload["date_formats"] or chosen.date_formats,
                    "dayfirst": payload["dayfirst"],
                    "confidence_floor": payload["confidence_floor"]}
        result = extract_document(doc, chosen.to_dict(), match_score=score,
                                  settings=settings, doc_id=doc_id,
                                  run_id=payload["run_id"])
        result.match_margin = margin
        result.runner_up = runner
        result.reasons.extend(reasons)
        result.status = result.derive_status()
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        _write_result(run_dir, result)
        return result.journal_line()

    except Exception as exc:
        return _failure_record(Path(path), Reason.EXCEPTION.value, str(exc),
                               traceback.format_exc(), doc_id)


def _write_result(run_dir: str, result: DocResult) -> None:
    # Shard by the first two characters: 10,000 files in one NTFS directory
    # makes Explorer unusable, and users do open these folders.
    target = Path(run_dir) / "results" / result.doc_id[:2] / f"{result.doc_id}.json"
    AtomicJson.write(target, result.to_dict(), indent=None)


def _failure_record(path: Path, reason: str, error: str = "",
                    tb: str = "", doc_id: str = "") -> dict:
    return {"doc_id": doc_id, "src": path.name, "sha256": "",
            "template_id": "", "tpl_version": 0, "vendor": "", "doc_type": "",
            "status": DocStatus.FAILED.value, "score": 0.0,
            "reasons": [reason], "fields_ok": 0, "fields_total": 0,
            "lines": 0, "ms": 0, "ts": _utc(), "error": error, "traceback": tb}


def _rehydrate(raw: dict) -> DocResult:
    """Rebuild a DocResult from its on-disk JSON."""
    from decimal import Decimal, InvalidOperation
    import datetime as _dt

    from ..domain.models import (CrossCheckResult, FieldResult, LineItem,
                                 Status)

    def coerce(text):
        if text is None or text == "":
            return None
        try:
            return _dt.date.fromisoformat(text)
        except (ValueError, TypeError):
            pass
        try:
            return Decimal(text)
        except (InvalidOperation, TypeError):
            return text

    r = DocResult(
        doc_id=raw.get("doc_id", ""), source_path=raw.get("source_path", ""),
        source_name=raw.get("source_name", ""), sha256=raw.get("sha256", ""),
        run_id=raw.get("run_id", ""), page_count=raw.get("page_count", 0),
        template_id=raw.get("template_id", ""),
        template_version=raw.get("template_version", 0),
        vendor=raw.get("vendor", ""), doc_type=raw.get("doc_type", ""),
        match_score=raw.get("match_score", 0.0),
        match_margin=raw.get("match_margin", 0.0),
        runner_up=raw.get("runner_up", ""),
        status=DocStatus(raw.get("status", "FAILED")),
        reasons=raw.get("reasons", []) or [],
        duration_ms=raw.get("duration_ms", 0),
        processed_utc=raw.get("processed_utc", ""), error=raw.get("error"),
        traceback=raw.get("traceback"))

    for name, f in (raw.get("fields") or {}).items():
        r.fields[name] = FieldResult(
            name=name, value=coerce(f.get("value")), raw=f.get("raw", ""),
            status=Status(f.get("status", "MISSING")),
            confidence=f.get("confidence", 0.0), strategy=f.get("strategy", ""),
            page=f.get("page", 0),
            bbox=tuple(f["bbox"]) if f.get("bbox") else None,
            reasons=f.get("reasons", []) or [],
            candidates=f.get("candidates", []) or [],
            corrected=f.get("corrected", False))

    for li in raw.get("line_items") or []:
        r.line_items.append(LineItem(
            index=li.get("index", 0),
            cells={k: coerce(v) for k, v in (li.get("cells") or {}).items()},
            raw=li.get("raw", {}) or {}, page=li.get("page", 0),
            bbox=tuple(li["bbox"]) if li.get("bbox") else None,
            status=Status(li.get("status", "EXTRACTED")),
            reasons=li.get("reasons", []) or [],
            confidence=li.get("confidence", 1.0)))

    for c in raw.get("cross_checks") or []:
        r.cross_checks.append(CrossCheckResult(
            check_id=c.get("check_id", ""), passed=c.get("passed", True),
            detail=c.get("detail", ""), severity=c.get("severity", "review")))
    return r


def _utc(epoch: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(epoch) if epoch else time.gmtime())
