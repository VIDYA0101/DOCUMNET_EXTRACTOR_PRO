"""Data-root resolution and path safety.

Every writable path in the application is derived from here. Nothing else in
the codebase may construct an absolute path to user data.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from pathlib import Path

APP_NAME = "DocumentExtractor"

# Windows device names that cannot be used as file or folder names.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_SLUG_MAX = 48


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Directory containing the executable (frozen) or the package root (source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled(*parts: str) -> Path:
    """Resolve a read-only bundled resource (tesseract, icons, ...)."""
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base.joinpath(*parts)


def _writable(path: Path) -> bool:
    """Probe writability by actually writing. Never infer it from the path string."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe_{os.getpid()}"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def resolve_data_root(override: str | None = None) -> Path:
    """Resolve DATA_ROOT in priority order, probing writability at each step."""
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    env = os.environ.get("DOCEXTRACTOR_DATA")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(app_dir().parent / "Data")   # C:\DocumentExtractor\Data
    candidates.append(app_dir() / "Data")          # portable / source tree

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / APP_NAME)
    candidates.append(Path.home() / f".{APP_NAME.lower()}")

    for c in candidates:
        if _writable(c):
            return c.resolve()
    raise RuntimeError(
        "No writable data location found. Tried: "
        + ", ".join(str(c) for c in candidates)
    )


class DataRoot:
    """Typed accessors for every folder under DATA_ROOT."""

    SUBDIRS = (
        "Templates", "Input", "Output", "Review", "Archive",
        "Logs", "Work", "index", "cache",
    )

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def ensure(self) -> "DataRoot":
        for name in self.SUBDIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "Work" / "runs").mkdir(parents=True, exist_ok=True)
        (self.root / "Logs" / "runs").mkdir(parents=True, exist_ok=True)
        (self.root / "cache" / "rasters").mkdir(parents=True, exist_ok=True)
        return self

    @property
    def templates(self) -> Path: return self.root / "Templates"
    @property
    def input(self) -> Path: return self.root / "Input"
    @property
    def output(self) -> Path: return self.root / "Output"
    @property
    def review(self) -> Path: return self.root / "Review"
    @property
    def archive(self) -> Path: return self.root / "Archive"
    @property
    def logs(self) -> Path: return self.root / "Logs"
    @property
    def work(self) -> Path: return self.root / "Work"
    @property
    def index(self) -> Path: return self.root / "index"
    @property
    def cache(self) -> Path: return self.root / "cache"
    @property
    def settings_file(self) -> Path: return self.root / "settings.json"
    @property
    def lock_file(self) -> Path: return self.root / ".lock"

    def run_dir(self, run_id: str) -> Path:
        return self.work / "runs" / safe_component(run_id)

    def output_dir(self, run_id: str) -> Path:
        return self.output / safe_component(run_id)

    def sweep_tmp(self) -> int:
        """Delete orphaned *.tmp files left by an interrupted atomic write."""
        n = 0
        for p in self.root.rglob("*.tmp"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n


def slugify(text: str, fallback: str = "unknown") -> str:
    """Convert untrusted text (e.g. a vendor name read off a PDF) into a safe
    path component.

    Never pass extracted text to the filesystem without going through here.
    """
    if not text:
        return fallback
    norm = unicodedata.normalize("NFKD", str(text))
    norm = norm.encode("ascii", "ignore").decode("ascii").lower()
    norm = re.sub(r"[^a-z0-9]+", "_", norm).strip("_.")
    norm = re.sub(r"_{2,}", "_", norm)
    if not norm:
        return fallback
    if len(norm) > _SLUG_MAX:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        norm = f"{norm[:_SLUG_MAX - 9]}_{digest}"
    if norm in _RESERVED:
        norm = f"{norm}_"
    return norm


def safe_component(name: str) -> str:
    """Sanitise a single path component while preserving readability."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    if not cleaned:
        return "unnamed"
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned[:120]


def contained(root: Path, candidate: Path) -> Path:
    """Assert that `candidate` resolves inside `root`. Belt and braces against
    traversal via extracted vendor names."""
    root_r = Path(root).resolve()
    cand_r = Path(candidate).resolve()
    if not cand_r.is_relative_to(root_r):
        raise ValueError(f"Path escapes its root: {cand_r} not under {root_r}")
    return cand_r


def long_path(p: Path) -> str:
    r"""Return a \\?\ extended path on Windows to survive MAX_PATH."""
    s = str(Path(p).resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s
