"""Extraction orchestration for one document against one template.

Ordering is deliberate: strategies produce candidates, ambiguity is refused
rather than resolved, validators run before confidence, and cross-checks run
last so they can demote fields that individually looked fine.
"""
from __future__ import annotations

import logging
import time

from ..models import (DocResult, FieldResult, PdfDocument, Reason, Status)
from . import confidence as C
from . import crosscheck, strategies, tables
from .types import parse_by_type
from .validators import run_validators

log = logging.getLogger(__name__)


def extract_document(doc: PdfDocument, template: dict, *, match_score: float = 1.0,
                     settings=None, doc_id: str = "", run_id: str = "") -> DocResult:
    started = time.perf_counter()
    settings = settings or {}
    date_formats = settings.get("date_formats")
    dayfirst = settings.get("dayfirst", True)
    floor_default = float(settings.get("confidence_floor", 0.80))

    result = DocResult(
        doc_id=doc_id, source_path=doc.path,
        source_name=doc.path.replace("\\", "/").rsplit("/", 1)[-1],
        sha256=doc.sha256, run_id=run_id, page_count=doc.page_count,
        template_id=template.get("template_id", ""),
        template_version=int(template.get("version", 1)),
        vendor=(template.get("vendor") or {}).get("display_name", ""),
        doc_type=template.get("document_type", ""),
        match_score=match_score,
        processed_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    if doc.load_error:
        result.error = doc.load_error
        result.reasons.append(doc.load_error)
        return _finish(result, started)

    # ---- tables first: their totals feed the header cross-checks -----------
    for tspec in template.get("tables") or []:
        items, treasons = tables.extract_table(
            doc, tspec, date_formats=date_formats, dayfirst=dayfirst)
        result.line_items.extend(items)
        result.reasons.extend(treasons)

    # ---- single-value fields ----------------------------------------------
    for fspec in template.get("fields") or []:
        try:
            fr = _extract_field(doc, fspec, match_score, date_formats,
                                dayfirst, floor_default)
        except Exception as exc:
            log.warning("field %s raised: %s", fspec.get("name"), exc, exc_info=True)
            fr = FieldResult(name=fspec.get("name", "?"), status=Status.FAILED,
                             reasons=[Reason.EXCEPTION.value])
        result.fields[fr.name] = fr

    # ---- cross-checks demote fields that individually looked fine ----------
    checks = crosscheck.run_checks(
        template.get("cross_checks") or [], result.fields, result.line_items)
    result.cross_checks = checks

    failed_checks = [c for c in checks if not c.passed and c.severity == "review"]
    if failed_checks:
        involved = set()
        for c in failed_checks:
            involved.update(crosscheck._names_used(
                next((s.get("expr", "") for s in template.get("cross_checks") or []
                      if s.get("id") == c.check_id), "")))
        for name in involved:
            fr = result.fields.get(name)
            if fr and fr.status == Status.EXTRACTED:
                fr.confidence = round(fr.confidence * 0.40, 4)
                fr.reasons.append(Reason.TOTAL_MISMATCH.value)
                if fr.confidence < floor_default:
                    fr.status = Status.LOW_CONFIDENCE

    result.status = result.derive_status()
    return _finish(result, started)


def _finish(result: DocResult, started: float) -> DocResult:
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    if not result.processed_utc:
        result.processed_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return result


def _extract_field(doc: PdfDocument, spec: dict, match_score: float,
                   date_formats, dayfirst: bool, floor_default: float) -> FieldResult:
    name = spec.get("name", "field")
    required = bool(spec.get("required", False))
    data_type = spec.get("data_type", "string")
    # A serialised template emits the key with an explicit null, so a plain
    # dict.get(key, default) returns None rather than the default.
    floor = float(spec.get("confidence_floor") or floor_default)
    on_ambiguous = (spec.get("on_ambiguous") or "review").lower()

    fr = FieldResult(name=name)
    specs = spec.get("strategies") or []
    if not specs:
        fr.status = Status.NOT_FOUND if required else Status.MISSING
        fr.reasons.append(Reason.NO_CANDIDATE.value)
        return fr

    last_reason = Reason.NO_CANDIDATE.value
    for index, sspec in enumerate(specs):
        outcome = strategies.run_strategy(doc, sspec)
        if not outcome.candidates:
            last_reason = outcome.reason or Reason.NO_CANDIDATE.value
            continue

        # Ambiguity is refused, not resolved by ordering.
        if len(outcome.candidates) > 1 and on_ambiguous != "first":
            fr.status = Status.AMBIGUOUS
            fr.reasons.append(Reason.MULTIPLE_CANDIDATES.value)
            fr.candidates = [c.text for c in outcome.candidates[:6]]
            fr.strategy = sspec.get("type", "")
            fr.page = outcome.candidates[0].page
            fr.bbox = outcome.candidates[0].bbox
            fr.confidence = 0.0
            return fr

        cand = outcome.candidates[0]
        fr.raw = cand.text
        fr.strategy = cand.strategy
        fr.page = cand.page
        fr.bbox = cand.bbox

        parsed = parse_by_type(cand.text, data_type, formats=date_formats,
                               dayfirst=dayfirst)
        if parsed.ambiguous:
            fr.reasons.append(Reason.DATE_AMBIGUOUS.value)

        failures = run_validators(spec.get("validators") or [], cand.text, parsed.value)
        pattern_ok = None
        if sspec.get("pattern"):
            rx = strategies.compile_pattern(sspec["pattern"],
                                            sspec.get("ignore_case", False))
            pattern_ok = bool(rx and rx.search(cand.text))

        score = C.field_confidence(
            template_match=match_score, strategy=cand.strategy,
            strategy_index=index, ocr_conf=cand.conf, pattern_ok=pattern_ok,
            candidate_count=len(outcome.candidates), type_ok=parsed.ok,
            cross_check_ok=None if not failures else False)
        fr.confidence = round(score.value, 4)

        if not parsed.ok:
            fr.status = Status.INVALID
            fr.reasons.append(parsed.reason or Reason.TYPE_PARSE_FAILED.value)
            return fr
        fr.value = parsed.value

        if failures:
            fr.status = Status.INVALID
            fr.reasons.extend(failures)
            return fr

        if parsed.ambiguous:
            fr.status = Status.LOW_CONFIDENCE
            fr.confidence = min(fr.confidence, 0.55)
            return fr

        fr.status = (Status.EXTRACTED if fr.confidence >= floor
                     else Status.LOW_CONFIDENCE)
        if fr.status == Status.LOW_CONFIDENCE:
            fr.reasons.append(f"BELOW_FLOOR_{int(floor * 100)}")
        return fr

    fr.status = Status.NOT_FOUND if required else Status.MISSING
    fr.reasons.append(last_reason)
    return fr
