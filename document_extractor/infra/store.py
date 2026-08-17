"""File-only persistence layer.

Three patterns, and nothing else in the codebase touches disk for state:

  A. AtomicJson   - rewritable documents (templates, settings, manifests)
  B. AppendLog    - accumulating facts (journals, hashes, audit)
  C. IndexSnapshot- fast startup for the in-memory index

There is no database. See docs/file_only_data_layer.md.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

SNAPSHOT_SCHEMA = 1


# --------------------------------------------------------------------------- A
class AtomicJson:
    """Write-to-temp-then-replace. os.replace is atomic on NTFS and POSIX."""

    @staticmethod
    def read(path: str | Path, default: Any = None) -> Any:
        p = Path(path)
        if not p.exists():
            return default
        try:
            with p.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def write(path: str | Path, obj: Any, *, indent: int | None = 2) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=indent, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)  # atomic

    @staticmethod
    def write_retry(path: str | Path, obj: Any, attempts: int = 3) -> None:
        """Antivirus and indexers transiently lock files on Windows."""
        delay = 0.5
        for i in range(attempts):
            try:
                AtomicJson.write(path, obj)
                return
            except PermissionError:
                if i == attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 3


# --------------------------------------------------------------------------- B
class AppendLog:
    """Single-writer append-only JSONL.

    A truncated final line (hard kill mid-append) is discarded on read; the
    record was incomplete by definition.
    """

    def __init__(self, path: str | Path, fsync_every: int = 50):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._since_sync = 0
        self._fsync_every = fsync_every

    def _open(self):
        if self._fh is None:
            self._fh = self.path.open("a", encoding="utf-8", newline="\n")
        return self._fh

    def append(self, record: dict) -> None:
        fh = self._open()
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        self._since_sync += 1
        if self._since_sync >= self._fsync_every:
            os.fsync(fh.fileno())
            self._since_sync = 0

    def sync(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._since_sync = 0

    def close(self) -> None:
        if self._fh is not None:
            self.sync()
            self._fh.close()
            self._fh = None

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    # -- reading -----------------------------------------------------------
    def read_from(self, offset: int = 0) -> tuple[list[dict], int]:
        """Return records after `offset` plus the new safe offset.

        The offset advances only past complete, newline-terminated lines, so a
        partial tail is simply re-read next time rather than lost or duplicated.
        """
        if not self.path.exists():
            return [], 0
        out: list[dict] = []
        safe = offset
        with self.path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # partial write; stop, do not advance offset
                safe += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # corrupt line: skip, offset already advanced
        return out, safe

    def read_all(self) -> list[dict]:
        return self.read_from(0)[0]

    def iter_all(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def compact(self, keep: Callable[[dict], bool]) -> int:
        """Rewrite the log keeping only records matching `keep`. Atomic."""
        self.close()
        tmp = self.path.with_suffix(".jsonl.tmp")
        kept = 0
        with tmp.open("w", encoding="utf-8", newline="\n") as out:
            for rec in self.iter_all():
                if keep(rec):
                    out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                    kept += 1
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, self.path)
        return kept


# --------------------------------------------------------------------------- C
class IndexSnapshot:
    """Rebuildable startup cache. Delete it and the index rebuilds from logs."""

    def __init__(self, cache_dir: str | Path):
        self.path = Path(cache_dir) / "index_snapshot.json"

    def load(self) -> dict | None:
        data = AtomicJson.read(self.path)
        if not isinstance(data, dict):
            return None
        if data.get("schema") != SNAPSHOT_SCHEMA:
            return None
        return data

    def save(self, payload: dict) -> None:
        payload = dict(payload)
        payload["schema"] = SNAPSHOT_SCHEMA
        payload["saved_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        AtomicJson.write(self.path, payload, indent=None)

    def invalidate(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class MemoryIndex:
    """Cross-run history held in RAM: duplicate detection and template stats.

    Loaded via snapshot + tail replay, so startup cost is flat regardless of
    how much history has accumulated.
    """

    STATS_WINDOW = 200

    def __init__(self, index_dir: str | Path, cache_dir: str | Path):
        self.index_dir = Path(index_dir)
        self.seen_log = AppendLog(self.index_dir / "seen_hashes.jsonl")
        self.keys_log = AppendLog(self.index_dir / "invoice_keys.jsonl")
        self.stats_log = AppendLog(self.index_dir / "template_stats.jsonl")
        self.audit_log = AppendLog(self.index_dir / "audit.jsonl")
        self.snapshot = IndexSnapshot(cache_dir)

        self.seen_hashes: dict[str, str] = {}          # h16 -> doc_id
        self.invoice_keys: dict[str, str] = {}         # "vendor|invnum" -> doc_id
        self.template_stats: dict[str, deque] = {}     # template_id -> recent runs
        self._offsets: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------
    def load(self) -> None:
        snap = self.snapshot.load()
        if snap:
            self.seen_hashes = snap.get("seen_hashes", {})
            self.invoice_keys = snap.get("invoice_keys", {})
            self.template_stats = {
                k: deque(v, maxlen=self.STATS_WINDOW)
                for k, v in snap.get("template_stats", {}).items()
            }
            self._offsets = snap.get("offsets", {})
        self._replay_tail()

    def _replay_tail(self) -> None:
        recs, off = self.seen_log.read_from(self._offsets.get("seen", 0))
        for r in recs:
            self.seen_hashes[r["h16"]] = r.get("doc_id", "")
        self._offsets["seen"] = off

        recs, off = self.keys_log.read_from(self._offsets.get("keys", 0))
        for r in recs:
            self.invoice_keys[f"{r['vendor']}|{r['invnum']}"] = r.get("doc_id", "")
        self._offsets["keys"] = off

        recs, off = self.stats_log.read_from(self._offsets.get("stats", 0))
        for r in recs:
            self.template_stats.setdefault(
                r["template_id"], deque(maxlen=self.STATS_WINDOW)
            ).append(r)
        self._offsets["stats"] = off

    def flush(self) -> None:
        for log in (self.seen_log, self.keys_log, self.stats_log, self.audit_log):
            log.sync()
        self.snapshot.save({
            "seen_hashes": self.seen_hashes,
            "invoice_keys": self.invoice_keys,
            "template_stats": {k: list(v) for k, v in self.template_stats.items()},
            "offsets": self._offsets,
        })

    def close(self) -> None:
        self.flush()
        for log in (self.seen_log, self.keys_log, self.stats_log, self.audit_log):
            log.close()

    def rebuild(self) -> None:
        self.snapshot.invalidate()
        self.seen_hashes.clear()
        self.invoice_keys.clear()
        self.template_stats.clear()
        self._offsets.clear()
        self._replay_tail()

    # -- queries -----------------------------------------------------------
    @staticmethod
    def h16(sha256_hex: str) -> str:
        return sha256_hex[:16]

    def has_seen(self, sha256_hex: str) -> str | None:
        return self.seen_hashes.get(self.h16(sha256_hex))

    def has_invoice(self, vendor: str, invnum: str) -> str | None:
        if not vendor or not invnum:
            return None
        return self.invoice_keys.get(f"{vendor}|{invnum}")

    # -- writes ------------------------------------------------------------
    def record_document(self, *, sha256_hex: str, doc_id: str, run_id: str,
                        vendor: str = "", invoice_number: str = "") -> None:
        h = self.h16(sha256_hex)
        if h not in self.seen_hashes:
            self.seen_hashes[h] = doc_id
            self.seen_log.append({"h16": h, "doc_id": doc_id, "run_id": run_id,
                                  "ts": _now()})
        if vendor and invoice_number:
            key = f"{vendor}|{invoice_number}"
            if key not in self.invoice_keys:
                self.invoice_keys[key] = doc_id
                self.keys_log.append({"vendor": vendor, "invnum": invoice_number,
                                      "doc_id": doc_id, "run_id": run_id,
                                      "ts": _now()})

    def record_template_run(self, template_id: str, stats: dict) -> None:
        rec = {"template_id": template_id, "ts": _now(), **stats}
        self.template_stats.setdefault(
            template_id, deque(maxlen=self.STATS_WINDOW)
        ).append(rec)
        self.stats_log.append(rec)

    def record_correction(self, *, doc_id: str, field: str, old: str, new: str,
                          user: str, reason: str = "") -> None:
        self.audit_log.append({"doc_id": doc_id, "field": field, "old": old,
                               "new": new, "user": user, "reason": reason,
                               "ts": _now()})
        self.audit_log.sync()

    # -- drift -------------------------------------------------------------
    def baseline(self, template_id: str) -> dict | None:
        """Mean field-success rate over prior runs, for drift comparison."""
        hist = self.template_stats.get(template_id)
        if not hist or len(hist) < 3:
            return None
        prior = list(hist)[:-1]
        n = len(prior)
        return {
            "runs": n,
            "field_success": sum(r.get("field_success", 0.0) for r in prior) / n,
            "match_score": sum(r.get("match_score", 0.0) for r in prior) / n,
        }


# --------------------------------------------------------------------------- lock
class InstanceLock:
    """Single-writer guarantee. A stale lock (dead PID / old heartbeat) is taken over."""

    STALE_SECONDS = 120

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.held = False

    def acquire(self) -> bool:
        info = AtomicJson.read(self.path)
        if isinstance(info, dict):
            age = time.time() - float(info.get("heartbeat", 0))
            if age < self.STALE_SECONDS and info.get("pid") != os.getpid():
                return False
        AtomicJson.write(self.path, {"pid": os.getpid(), "heartbeat": time.time(),
                                     "started": _now()}, indent=None)
        self.held = True
        return True

    def heartbeat(self) -> None:
        if self.held:
            AtomicJson.write(self.path, {"pid": os.getpid(),
                                         "heartbeat": time.time()}, indent=None)

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
