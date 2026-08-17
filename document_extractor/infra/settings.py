"""Application settings. Plain JSON, atomic writes, defaults merged on load."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .store import AtomicJson


@dataclass
class Settings:
    data_root: str = ""
    workers: int = 0                    # 0 = auto (cpu_count - 1, capped)
    per_file_timeout_s: int = 120
    max_pages: int = 200
    max_file_mb: int = 100
    ocr_enabled: bool = False           # v1.1
    ocr_provider: str = "tesseract"
    ocr_dpi: int = 300
    confidence_floor: float = 0.80
    template_min_score: float = 0.72
    template_review_below: float = 0.85
    template_margin: float = 0.10
    dayfirst: bool = True               # India-first date parsing
    default_currency: str = "INR"
    archive_originals: bool = True
    delete_after_archive: bool = False  # destructive: opt-in only
    split_output_by_vendor: bool = False
    include_field_detail_sheet: bool = False
    log_level: str = "INFO"
    log_values: bool = False            # PII: values only at DEBUG
    log_retention_days: int = 90
    date_formats: list = field(default_factory=lambda: [
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
        "%Y-%m-%d", "%d/%m/%y", "%d-%b-%Y", "%b %d, %Y",
    ])

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        raw = AtomicJson.read(path, {}) or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: str | Path) -> None:
        AtomicJson.write(path, asdict(self))

    def worker_count(self) -> int:
        import os
        if self.workers > 0:
            return self.workers
        return max(1, min((os.cpu_count() or 2) - 1, 6))
