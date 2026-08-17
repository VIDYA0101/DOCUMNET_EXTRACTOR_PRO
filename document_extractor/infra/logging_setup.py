"""Logging: rotating human log + structured JSONL, with correlation IDs.

Workers never open the log file. They emit records onto a queue that the
supervisor process drains, because concurrent appends from six processes
produce interleaved, corrupt lines.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

_CONFIGURED = False


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "doc_id", "template_id", "reason"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(log_dir: str | Path, level: str = "INFO", run_id: str | None = None) -> None:
    """Configure root logging. Safe to call once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "runs").mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    human = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    human.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"))
    root.addHandler(human)

    name = f"runs/{run_id}.jsonl" if run_id else "app.jsonl"
    structured = logging.handlers.RotatingFileHandler(
        log_dir / name, maxBytes=20 * 1024 * 1024, backupCount=3, encoding="utf-8")
    structured.setFormatter(JsonlFormatter())
    root.addHandler(structured)

    # --noconsole sets stdout/stderr to None; guard before adding a stream handler.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
        root.addHandler(console)

    _CONFIGURED = True


def purge_old(log_dir: str | Path, days: int = 90) -> int:
    cutoff = time.time() - days * 86400
    removed = 0
    for p in Path(log_dir).rglob("*"):
        if p.is_file() and p.stat().st_mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
