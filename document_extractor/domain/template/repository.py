"""Template repository: load, save, version, and regression-test templates.

Templates are the system of record. Every save archives the previous version so
a layout fix that breaks 3,000 historical documents can be rolled back.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from ...infra.paths import slugify
from ...infra.store import AtomicJson
from .schema import Template, validate

log = logging.getLogger(__name__)


def canonical_hash(template: dict) -> str:
    payload = json.dumps(template, sort_keys=True, separators=(",", ":"),
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_template_id(vendor: str, doc_type: str) -> str:
    return f"{slugify(vendor)}__{slugify(doc_type)}"


class TemplateRepository:
    """All templates live in memory for the session; 100 of them is ~2 MB."""

    def __init__(self, templates_dir: str | Path):
        self.root = Path(templates_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._templates: dict[str, Template] = {}
        self._paths: dict[str, Path] = {}
        self._errors: list[tuple[str, str]] = []

    # -- loading -----------------------------------------------------------
    def load_all(self) -> int:
        self._templates.clear()
        self._paths.clear()
        self._errors.clear()
        for path in sorted(self.root.rglob("template.json")):
            try:
                raw = AtomicJson.read(path)
                if not isinstance(raw, dict):
                    raise ValueError("not a JSON object")
                tpl = validate(raw)
                self._templates[tpl.template_id] = tpl
                self._paths[tpl.template_id] = path
            except Exception as exc:
                rel = str(path.relative_to(self.root))
                self._errors.append((rel, str(exc)))
                log.error("invalid template %s: %s", rel, exc)
        log.info("loaded %d templates (%d invalid)",
                 len(self._templates), len(self._errors))
        return len(self._templates)

    @property
    def errors(self) -> list[tuple[str, str]]:
        return list(self._errors)

    def all(self) -> list[Template]:
        return sorted(self._templates.values(),
                      key=lambda t: (t.vendor.display_name, t.document_type))

    def get(self, template_id: str) -> Template | None:
        return self._templates.get(template_id)

    def path_for(self, template_id: str) -> Path | None:
        return self._paths.get(template_id)

    def dir_for(self, tpl: Template) -> Path:
        vendor = tpl.vendor.slug or slugify(tpl.vendor.display_name)
        return self.root / vendor / slugify(tpl.document_type)

    # -- saving ------------------------------------------------------------
    def save(self, tpl: Template, *, bump_version: bool = True) -> Path:
        directory = self.dir_for(tpl)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "versions").mkdir(exist_ok=True)
        (directory / "samples").mkdir(exist_ok=True)

        target = directory / "template.json"
        if target.exists() and bump_version:
            previous = AtomicJson.read(target)
            if isinstance(previous, dict):
                old_v = int(previous.get("version", 1))
                AtomicJson.write(directory / "versions" / f"v{old_v}.json", previous)
                tpl.version = old_v + 1

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not tpl.created_utc:
            tpl.created_utc = now
        tpl.modified_utc = now
        if not tpl.vendor.slug:
            tpl.vendor.slug = slugify(tpl.vendor.display_name)

        AtomicJson.write_retry(target, tpl.to_dict())
        self._templates[tpl.template_id] = tpl
        self._paths[tpl.template_id] = target
        log.info("saved template %s v%d", tpl.template_id, tpl.version)
        return target

    def versions(self, template_id: str) -> list[int]:
        path = self._paths.get(template_id)
        if not path:
            return []
        vdir = path.parent / "versions"
        out = []
        for f in vdir.glob("v*.json"):
            try:
                out.append(int(f.stem[1:]))
            except ValueError:
                continue
        return sorted(out)

    def rollback(self, template_id: str, version: int) -> Template | None:
        path = self._paths.get(template_id)
        if not path:
            return None
        archived = path.parent / "versions" / f"v{version}.json"
        raw = AtomicJson.read(archived)
        if not isinstance(raw, dict):
            return None
        tpl = validate(raw)
        self.save(tpl, bump_version=True)
        return tpl

    def delete(self, template_id: str) -> bool:
        path = self._paths.get(template_id)
        if not path or not path.exists():
            return False
        AtomicJson.write(path.parent / "versions" /
                         f"deleted_{int(time.time())}.json",
                         AtomicJson.read(path, {}))
        path.unlink()
        self._templates.pop(template_id, None)
        self._paths.pop(template_id, None)
        return True

    # -- samples and regression -------------------------------------------
    def samples(self, template_id: str) -> list[Path]:
        path = self._paths.get(template_id)
        if not path:
            return []
        return sorted((path.parent / "samples").glob("*.pdf"))

    def expected_path(self, sample_pdf: Path) -> Path:
        return sample_pdf.with_suffix(".expected.json")

    def write_expected(self, sample_pdf: Path, doc_result) -> Path:
        target = self.expected_path(sample_pdf)
        payload = {
            "fields": {n: (None if f.value is None else str(f.value))
                       for n, f in doc_result.fields.items()},
            "line_count": len(doc_result.line_items),
            "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        AtomicJson.write(target, payload)
        return target

    def validate_template(self, template_id: str, extract_fn) -> dict:
        """Re-run every golden sample and diff against the recorded expectation.

        This is what stops a fix for one vendor's new layout from silently
        breaking every document already processed under the old one.
        """
        tpl = self.get(template_id)
        if tpl is None:
            return {"ok": False, "error": "template not found"}

        report = {"template_id": template_id, "samples": [], "passed": 0,
                  "failed": 0, "no_baseline": 0}

        for pdf in self.samples(template_id):
            expected = AtomicJson.read(self.expected_path(pdf))
            try:
                result = extract_fn(pdf, tpl)
            except Exception as exc:
                report["samples"].append({"sample": pdf.name, "status": "ERROR",
                                          "detail": str(exc)})
                report["failed"] += 1
                continue

            if not isinstance(expected, dict):
                report["samples"].append({"sample": pdf.name,
                                          "status": "NO_BASELINE",
                                          "detail": "no .expected.json recorded"})
                report["no_baseline"] += 1
                continue

            diffs = []
            for name, want in (expected.get("fields") or {}).items():
                got = result.fields.get(name)
                got_val = None if (got is None or got.value is None) else str(got.value)
                if got_val != want:
                    diffs.append(f"{name}: expected {want!r}, got {got_val!r}")
            want_lines = expected.get("line_count")
            if want_lines is not None and want_lines != len(result.line_items):
                diffs.append(f"line_count: expected {want_lines}, "
                             f"got {len(result.line_items)}")

            if diffs:
                report["samples"].append({"sample": pdf.name, "status": "FAIL",
                                          "detail": "; ".join(diffs[:6])})
                report["failed"] += 1
            else:
                report["samples"].append({"sample": pdf.name, "status": "PASS",
                                          "detail": ""})
                report["passed"] += 1

        report["ok"] = report["failed"] == 0
        return report

    # -- import / export ---------------------------------------------------
    def export(self, template_id: str, target: Path) -> Path | None:
        tpl = self.get(template_id)
        if tpl is None:
            return None
        AtomicJson.write(target, tpl.to_dict())
        return target

    def import_file(self, source: Path) -> Template:
        """Import an externally supplied template. Validated, never trusted."""
        raw = AtomicJson.read(source)
        if not isinstance(raw, dict):
            raise ValueError("not a valid template file")
        tpl = validate(raw)
        self.save(tpl, bump_version=False)
        return tpl
