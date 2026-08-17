# Document Extractor — Architecture Review

**Reviewer role:** Senior software architect
**Status:** Design review only. No implementation.
**Verdict:** The product concept is sound and buildable. The proposed *architecture* has four flaws serious enough to sink the project if carried into code, and roughly a dozen missing requirements. Details below.

---

## 0. Executive summary — the things you must change

| # | Issue | Severity | Recommendation |
|---|---|---|---|
| 1 | "No database" applied to *runtime state*, not just templates | **Critical** | Keep JSON as source of truth for templates. Add SQLite as a **disposable, rebuildable index/journal**. See §1.1. |
| 2 | Writable data folders sit next to the EXE | **Critical** | Program Files is read-only under UAC. Dual-mode data root resolution. See §2. |
| 3 | Field extraction based on drawn rectangles (absolute coordinates) | **Critical** | Rectangles break on variable-length invoices. Use **anchor-relative extraction**; the rectangle is only an authoring gesture. See §7. |
| 4 | PyMuPDF is AGPL-3.0 | **Critical (legal)** | For a distributed business app you must buy a commercial licence or switch to `pypdfium2` + `pdfplumber`. See §3.1. |
| 5 | Line-item tables treated as "just another field type" | High | Tables are 60% of the engineering effort. Separate subsystem. See §8. |
| 6 | No arithmetic/checksum cross-validation | High | The single highest-leverage accuracy mechanism, and it's absent. See §14. |
| 7 | No template versioning or regression fixtures | High | Vendors change layouts. Without golden tests, every template edit is a silent regression. See §12. |
| 8 | `multiprocessing` + PyInstaller | High | Missing `freeze_support()` fork-bombs the EXE. See §13. |
| 9 | Vendor name extracted from PDF → used as a folder name | High | Path-traversal / invalid-filename vector. See §18. |
| 10 | Indian number and date formats | High | `1,23,456.00` and `DD/MM/YYYY` break naive parsers. See §14.3. |

---

## 1. Recommended architecture

### 1.1 The database question — where I push back hardest

Your requirement is stated as *"I do not want SQL or any database."* I think the requirement you actually have is:

- No server to install, no service, no DBA, no connection strings.
- Templates must be human-readable, diffable, copyable, and editable outside the app.
- The whole thing must be a folder you can zip and move.

**SQLite satisfies all three.** It is a single file, it ships inside CPython's standard library (`import sqlite3` — zero extra dependency, zero install), and it is exactly "a normal local Windows file." Rejecting it as "a database" is rejecting a name, not a risk.

What you lose by refusing it, concretely:

- **Atomicity.** A JSON file rewritten mid-batch during a power loss is corrupt. SQLite gives you crash-safe transactions for free.
- **Resume.** A 1,000-PDF run that dies at file 812 must restart from zero without a durable per-file journal.
- **Duplicate detection.** "Have I already processed this invoice?" is an indexed lookup over tens of thousands of rows. In JSON it is a full directory scan.
- **The Results and Review screens.** Sorting, filtering, and paging 50,000 extracted rows in the GUI means loading every JSON file into RAM on every screen open.
- **Template lookup at 100+ templates.** Linear scan of 100 folders × N files per document × 1,000 documents.

**My recommendation — a compromise that keeps your requirement's intent intact:**

```
Source of truth  = JSON/JSONL files on disk (templates, run manifests, extraction results)
Index/journal    = SQLite file at Data/cache/index.db
```

The SQLite file is **100% derivable** from the JSON files. Delete it and the app rebuilds it on next launch. It is a cache, not a system of record. You can still zip the folder, hand-edit templates, diff them in Git, and read every result in Notepad. Nothing is locked in a binary blob.

If you reject even this, the mandatory fallback disciplines are:

- Every write is atomic: write to `x.json.tmp`, `os.replace()` onto `x.json`. Never write in place.
- The run journal is **append-only JSONL**, one line per file completed. Append-only survives crashes; rewritten JSON does not.
- The Results grid becomes a paged reader over JSONL with an in-memory row index built at startup.
- Accept O(n) template matching and cap the library at ~50 templates before performance degrades noticeably.

I'd take the SQLite cache. It costs you nothing and removes an entire class of bug.

### 1.2 Layered architecture

Keep the GUI and the extraction engine strictly separated. The engine must be importable and runnable headless — this is what makes it testable and what lets you ship a CLI later.

```
┌─────────────────────────────────────────────────────────────┐
│  Presentation (PySide6)                                     │
│  Dashboard · Template Library · Template Editor ·           │
│  Process · Results · Review · Settings                      │
│  — Qt widgets/models only. Zero PDF logic.                  │
└───────────────┬─────────────────────────────────────────────┘
                │ Qt signals / slots (thread-safe queue)
┌───────────────┴─────────────────────────────────────────────┐
│  Application / Orchestration                                │
│  JobManager · BatchRunner · ProgressAggregator ·            │
│  CancellationToken · RunManifest                            │
│  — QThread supervisor over a ProcessPool.                   │
└───────────────┬─────────────────────────────────────────────┘
                │ plain dataclasses, picklable, no Qt types
┌───────────────┴─────────────────────────────────────────────┐
│  Domain / Engine  (pure Python, no Qt, unit-testable)       │
│  ┌──────────────┬──────────────┬──────────────────────────┐ │
│  │ PageModel    │ Matcher      │ Extractor                │ │
│  │ (text+boxes) │ (fingerprint │ (anchors, regions,       │ │
│  │              │  + scoring)  │  tables, validators)     │ │
│  ├──────────────┼──────────────┼──────────────────────────┤ │
│  │ OcrProvider  │ Confidence   │ ExcelWriter              │ │
│  │ (pluggable)  │ Scorer       │ (openpyxl, write_only)   │ │
│  └──────────────┴──────────────┴──────────────────────────┘ │
└───────────────┬─────────────────────────────────────────────┘
┌───────────────┴─────────────────────────────────────────────┐
│  Infrastructure                                             │
│  PathResolver · AtomicFileStore · TemplateRepository ·      │
│  IndexCache (SQLite) · Logger · SettingsStore               │
└─────────────────────────────────────────────────────────────┘
```

**Hard rule:** nothing below the Application layer may import `PySide6`. Enforce it with a test that greps the domain package for Qt imports. Without this rule, you will not be able to unit-test extraction, and extraction accuracy is the whole product.

### 1.3 The canonical intermediate representation

Everything hinges on one abstraction: a **normalised page model** produced identically by the text path and the OCR path.

```
Document
  └── Page[n]
        width_pt, height_pt, rotation
        source: "text" | "ocr" | "mixed"
        words: [ { text, x0, y0, x1, y1, conf, line_id, block_id } ]
        lines: [ { text, bbox, word_ids } ]
        rulings: [ horizontal/vertical vector lines ]   # for table detection
        raster: optional rendered PNG (lazily, for the editor & review crops)
```

Downstream code — matching, extraction, tables, confidence — only ever sees this. It never knows whether the words came from a text layer or Tesseract. This one decision is what makes OCR support a configuration detail rather than a parallel codebase.

Coordinates: normalise to a **top-left origin, points (1/72"), page-relative** system. pdfplumber and PDFium disagree on origin; fix it once at the boundary. Also store a `normalised` variant scaled to `[0,1] × [0,1]` so A4 and Letter templates interoperate.

---

## 2. Recommended folder structure

Your proposed layout has a fatal deployment flaw: **if the app is installed to `C:\Program Files\DocumentExtractor`, Windows will not let it write to `Templates/`, `Output/`, or `Logs/`.** Standard user accounts have no write permission there. Windows will either fail outright or silently redirect writes to the VirtualStore, where users cannot find their own output files. This is one of the most common ways Python desktop apps ship broken.

**Solution: resolve the data root at startup.**

```
1. If a file named "portable.txt" sits beside the EXE
   AND the EXE's directory is writable
     → DATA_ROOT = <exe_dir>\Data            (portable / USB stick mode)
2. Else if a data root is set in settings
     → DATA_ROOT = <that path>               (user chose a network share)
3. Else
     → DATA_ROOT = %LOCALAPPDATA%\DocumentExtractor
```

Probe writability by actually creating and deleting a temp file. Do not infer it from the path string. Show the resolved data root in Settings and in the Dashboard footer — support calls about "where did my files go" disappear.

**Recommended layout:**

```
<INSTALL_DIR>/                          # read-only, may be Program Files
├── DocumentExtractor.exe
├── _internal/                          # PyInstaller one-dir payload
│   ├── PySide6/ ...
│   └── vendor/
│       ├── tesseract/                  # tesseract.exe + tessdata/eng.traineddata
│       └── models/                     # optional RapidOCR ONNX models
├── LICENSES/                           # LGPL/Apache notices — required, see §18.6
└── portable.txt                        # present only in portable builds

<DATA_ROOT>/                            # always writable
├── Templates/
│   └── <vendor_slug>/
│       └── <doctype_slug>/
│           ├── template.json           # v-current, single file (see §6)
│           ├── versions/
│           │   ├── v1.json
│           │   └── v2.json
│           ├── samples/
│           │   ├── sample_001.pdf
│           │   └── sample_001.expected.json     # golden fixture, §12
│           └── thumbnail.png
├── Input/                              # optional watched drop folder
├── Work/
│   └── runs/<run_id>/
│       ├── manifest.json               # run config, counts, status
│       ├── journal.jsonl               # append-only, one line per file
│       └── results/<doc_id>.json       # full per-document extraction
├── Output/
│   └── <run_id>/
│       ├── DocumentExtractor_<run_id>.xlsx
│       └── data/*.csv                  # machine-readable sidecar
├── Review/
│   └── <run_id>/                       # PDFs + reason codes awaiting a human
├── Archive/
│   └── <vendor_slug>/<yyyy>/<mm>/      # processed originals (copy, not move)
├── Logs/
│   ├── app.log                         # rotating, 10MB × 5
│   └── runs/<run_id>.log
├── cache/
│   ├── index.db                        # SQLite — deletable, rebuildable
│   └── rasters/                        # rendered page images, LRU-evicted
└── settings.json
```

Changes from yours and why:

- **`Templates/` moved out of the install dir.** Users must be able to create templates without admin rights.
- **`Work/` added.** Run state is not output. Mixing them makes cleanup impossible.
- **`Archive/` replaces "move to vendor folders."** Never *move* a user's source files by default — a bug then destroys data you cannot recover. Copy, verify by hash, and only then optionally delete, and only if the user explicitly opted in.
- **`versions/` and `samples/*.expected.json`** — see §12. Non-negotiable if you want templates that survive a year.
- **`Output/` keyed by run, not vendor.** Your `Output/Vendor_A/Invoice.xlsx` design means every run either overwrites or appends to the same file. Overwriting loses data; appending creates an unbounded file with no run boundaries and no way to undo a bad batch. Per-run output is atomic and reversible. Add a "consolidate by vendor" export as a *separate action* if the user wants it.

**Slugs:** `<vendor_slug>` is a sanitised, ASCII, lowercase, length-capped identifier — never the raw extracted vendor name. See §18.2.

---

## 3. Recommended Python libraries

### 3.1 The licensing problem you have not spotted

**PyMuPDF (fitz) is AGPL-3.0.** If you distribute an EXE that links it, the AGPL requires you to release your entire application's source under AGPL — including to anyone who interacts with it over a network. For a commercial business tool this is almost certainly unacceptable. Artifex sells a commercial licence; it is not cheap.

This matters because PyMuPDF is by far the most commonly recommended library, and the trap is invisible until legal review.

**Permissive stack instead:**

| Concern | Library | Licence | Notes |
|---|---|---|---|
| Page rendering to image | **pypdfium2** | BSD-3 / Apache-2.0 | Google's PDFium. Fast, robust, permissive. Use for the template editor canvas, OCR rasterisation, and review crops. |
| Text + word boxes + rulings | **pdfplumber** | MIT | Built on `pdfminer.six` (MIT). Gives per-character boxes, line/rect vectors, and table primitives. Slower than PDFium — acceptable, it's not the bottleneck. |
| Fast text-only pre-pass | **pypdfium2** | BSD-3 | Use for the cheap "does this page have a text layer?" probe before committing to pdfplumber. |
| Page count / merge / split / encryption | **pypdf** | BSD-3 | For splitting multi-invoice PDFs and handling encrypted files. |
| OCR | **Tesseract 5** via subprocess, or `pytesseract` | Apache-2.0 (engine), MIT (wrapper) | See §5. |
| Optional better OCR | **RapidOCR-ONNXRuntime** | Apache-2.0 | PP-OCR models on ONNX Runtime. ~60MB, no PyTorch. |
| Image preprocessing | **opencv-python-headless** + **Pillow** | Apache-2.0 / MIT-CMU | Deskew, binarise, denoise. `headless` avoids pulling in Qt conflicts — important, since you already ship Qt. |
| GUI | **PySide6** | LGPL-3.0 | Correct choice. PyQt6 is GPL/commercial. LGPL compliance obligations in §18.6. |
| Excel | **openpyxl** | MIT | Use `write_only=True` mode for large sheets. |
| Schema validation | **pydantic v2** | MIT | Validate every template.json on load. Never trust a file on disk. |
| Fuzzy string matching | **rapidfuzz** | MIT | For vendor name matching and anchor tolerance. C++ backed, fast. |
| Date parsing | **dateutil** — *with restrictions* | Apache-2.0/BSD | See §14.3. Never call `dateutil.parser.parse()` unconstrained. |
| Logging | stdlib `logging` + **structlog** (optional) | — | JSON lines to file, human format to console. |
| Packaging | **PyInstaller** | GPL w/ bundling exception | The exception permits proprietary bundled apps. Alternative: Nuitka (see §17.3). |
| Tests | **pytest** | MIT | Plus `pytest-qt` for GUI smoke tests. |

**Deliberately excluded and why:**

- **PyMuPDF** — AGPL, above.
- **scikit-learn / PyTorch / transformers** — a 1.5–2.5 GB bundle and 10–30 s cold start. Not justified for MVP. Hand-rolled TF-IDF over page-1 text is ~40 lines and adequate (§9).
- **camelot / tabula** — camelot needs Ghostscript (AGPL, external install); tabula needs a JVM. Both are deployment poison for a self-contained EXE.
- **pandas** — tempting, but it adds ~50MB and encourages sloppy dtype coercion that will silently mangle invoice numbers and Indian-format amounts. Use plain dataclasses and `csv`. If you must, confine it to the Results grid model.

Pin every version in `requirements.lock`. Build in a clean venv on a clean VM. A dependency that quietly upgrades between builds is how you ship a regression you cannot reproduce.

---

## 4. Best PDF extraction strategy

### 4.1 Per-page triage (not per-document)

Real-world PDFs are mixed: a digital invoice with a scanned annexure, a born-digital doc where page 3 is a photographed signature page. Decide per page.

```
For each page:
  1. Extract text via pypdfium2 (fast).
  2. Compute text-layer quality metrics:
       - char_count
       - printable_ratio       (non-control, non-replacement chars / total)
       - unique_char_ratio     (detects CID/ToUnicode corruption)
       - coverage              (union of word bboxes area / page area)
  3. Classify:
       char_count < 50                      → SCANNED   → OCR
       printable_ratio < 0.85               → CORRUPT   → OCR
       unique_char_ratio < 0.15             → CORRUPT   → OCR   (garbled font map)
       coverage < 0.5% and page has images  → SCANNED   → OCR
       else                                 → TEXT      → pdfplumber
  4. If TEXT: pdfplumber for word boxes, char boxes, and vector rulings.
```

**The `unique_char_ratio` check catches a failure mode most people miss:** PDFs with broken or absent `ToUnicode` CMaps extract as text — but the text is garbage (`\x03\x0f\x11...` or all one repeated glyph). Naive pipelines happily extract a text layer and produce confidently wrong output. Detect it and fall back to OCR.

### 4.2 Extraction primitives, in priority order

Each field definition carries an ordered list of strategies. The first that returns exactly one candidate wins. Ambiguity is never resolved by "pick the first."

| Strategy | Use for | How it works |
|---|---|---|
| `anchor_relative` | **Default for all single values** | Locate anchor text → search a direction/offset window → apply pattern. |
| `regex_page` | Globally unique IDs (GSTIN, PAN, IBAN) | Regex over whole-page text, checksum-validated. Position-independent. |
| `region_absolute` | Genuinely fixed-position fields (pre-printed forms) | Normalised bbox + tolerance. Use sparingly. |
| `label_pair` | Two-column "Label: Value" blocks | Line-level split on `:` with label fuzzy match. |
| `table_cell` | Field inside a detected table | Row/column addressing after table extraction. |
| `computed` | Derived values | e.g. `total = subtotal + tax`. Also used as a cross-check. |
| `constant` | Values fixed per template | Vendor name, currency. Zero extraction risk. |

Every strategy returns `(value, bbox, page, confidence, evidence)` or `NOT_FOUND` or `AMBIGUOUS`. **There is no fourth option.** No strategy may return a "best guess."

---

## 5. Best OCR strategy

### 5.1 Recommendation: Tesseract 5, bundled, with RapidOCR as an opt-in upgrade

**Tesseract 5 (UB Mannheim Windows build), bundled as `tesseract.exe` + `tessdata/`.**

Why:
- Apache-2.0, fully offline, no runtime install for the user.
- Adds ~50 MB with `eng.traineddata` (use the *fast* or standard model, not `_best` — `_best` is 3–4× slower for marginal gains on clean documents).
- **`image_to_data` returns per-word bounding boxes and per-word confidence (0–100).** This is essential: it feeds directly into your normalised page model and into confidence scoring. Most OCR wrappers throw this away — do not.
- Mature, predictable, well-documented failure modes.

Invoke it as a **subprocess**, not via the `tesserocr` C bindings. Subprocess gives you a hard timeout and crash isolation — a malformed image that segfaults Tesseract kills a child process, not your app.

**Rasterisation:** pypdfium2 at **300 DPI grayscale**. 300 is the sweet spot; 150 loses small print (tax IDs, line-item text), 600 quadruples time for no accuracy gain on printed text.

**Preprocessing pipeline (OpenCV), applied in order:**
1. Grayscale.
2. Deskew — estimate angle via minimum-area rectangle over text pixels or a Hough transform; rotate if `|angle| > 0.3°`. **Skew is the #1 cause of bad OCR on scanned invoices**, and it is cheap to fix.
3. Denoise — median blur (kernel 3) only if estimated noise is high.
4. Binarise — Sauvola or adaptive Gaussian threshold. Avoid global Otsu on documents with shading or fax artifacts.
5. Border removal — crop scanner black edges.

Make preprocessing steps individually toggleable in Settings. Some scanners produce output where denoising *hurts*.

**Tesseract config:**
- `--psm 6` (uniform block) as default for full pages; `--psm 7` (single line) or `--psm 8` (single word) when OCRing a small crop for a specific field. Per-field crop OCR at the right PSM is markedly more accurate than reading it out of a full-page pass.
- `--oem 1` (LSTM only).
- For numeric-only fields, restrict with `tessedit_char_whitelist=0123456789.,-/` — this alone eliminates most `O`/`0`, `l`/`1`, `S`/`5` confusions.
- `preserve_interword_spaces=1` helps column alignment.

**Second pass for low-confidence fields:** if a field extracted from the full-page OCR has word confidence below threshold, re-render *just that region* at 400–600 DPI with a field-appropriate PSM and whitelist, and re-OCR. This targeted retry is cheap (one small crop) and recovers a large fraction of borderline reads.

### 5.2 Why not the alternatives

- **PaddleOCR / EasyOCR / docTR** — better on degraded scans, but drag in PyTorch or PaddlePaddle: 1.5–2.5 GB bundle, 10–30 s cold start, GPU/CPU wheel confusion, and frequent PyInstaller hook breakage. Wrong trade for a desktop EXE.
- **RapidOCR-ONNXRuntime** — the good middle path. PP-OCR detection+recognition models on ONNX Runtime CPU: ~60 MB, no PyTorch, meaningfully better than Tesseract on noisy/low-contrast scans and on rotated text. Ship it behind an `OcrProvider` interface as an optional download in v1.1. Design for it now (the interface), install it later.
- **Windows.Media.Ocr** (built into Windows 10/11) — genuinely zero-download and decent, but reachable only via WinRT interop (`winsdk`/`winrt` packages), fragile under PyInstaller, and its confidence output is coarse. Not worth the integration risk as primary.
- **Cloud OCR (Azure Document Intelligence, AWS Textract, Google Document AI)** — dramatically more accurate, and they solve tables. But they violate your offline requirement, and invoices contain commercially sensitive data. Correct call to exclude. Keep the `OcrProvider` interface clean so a customer who *wants* cloud can plug it in.

**Design the OCR layer as a plugin from day one:**

```
OcrProvider (interface)
  .is_available() -> bool
  .ocr_page(image, lang, psm_hint, whitelist) -> list[Word]   # text, bbox, conf
  .name, .version
```

Implementations: `TesseractProvider` (MVP), `RapidOcrProvider` (v1.1), `NullProvider` (OCR disabled). Selectable in Settings.

---

## 6. Best template representation format

**JSON, one file per template, validated by a pydantic schema, with an explicit `schema_version`.** This part of your design is right. Refinements:

- **Single `template.json`, not `template.json` + `fields.json`.** Two files that must stay consistent is a bug generator with no upside. Fields belong to the template.
- **`schema_version` at the top.** Ship v1 knowing you will need v2. Write a migration function per version bump and run it on load.
- **Never `pickle`, never `eval`, never YAML with `yaml.load`.** JSON only, parsed with `json.load`, then schema-validated. Template files are untrusted input the moment a user shares one by email (§18.3).
- **Store a `template_hash`** (SHA-256 of the canonicalised JSON) in every extraction result. This is how you answer "which exact template version produced this row?" a year later during an audit.

### 6.1 Schema sketch (structure only — not code)

```jsonc
{
  "schema_version": 1,
  "template_id": "acme-industries__invoice",       // stable, immutable
  "version": 3,
  "created_utc": "...", "modified_utc": "...",
  "vendor": {
    "display_name": "ACME Industries Pvt Ltd",
    "slug": "acme-industries",
    "aliases": ["ACME Ind.", "ACME INDUSTRIES PRIVATE LIMITED"],
    "identifiers": {                                // strongest matching signals
      "gstin": "29AABCA1234A1Z5",
      "pan": "AABCA1234A",
      "cin": null
    }
  },
  "document_type": "invoice",
  "page_geometry": { "width_pt": 595.3, "height_pt": 841.9, "label": "A4" },

  "fingerprint": {                                  // §9 — how we recognise this doc
    "required_tokens": ["ACME INDUSTRIES", "TAX INVOICE"],
    "forbidden_tokens": ["DELIVERY CHALLAN", "PROFORMA"],
    "identifier_regexes": [
      { "field": "gstin", "pattern": "29AABCA1234A1Z5", "weight": 0.50 }
    ],
    "layout_signature": {                           // coarse ink-density grid
      "grid": [8, 8],
      "vector": [0.0, 0.12, 0.44, "..."],
      "weight": 0.15
    },
    "text_profile": {                               // hashed TF-IDF of page 1
      "top_terms": { "gstin": 0.31, "hsn": 0.22, "irn": 0.18 },
      "weight": 0.20
    },
    "min_score": 0.72,
    "review_below": 0.85
  },

  "fields": [
    {
      "name": "invoice_number",
      "label": "Invoice Number",
      "kind": "single",                             // single | table | repeated | header | footer
      "data_type": "string",
      "required": true,
      "unique_key": true,                           // participates in dedup key
      "strategies": [
        {
          "type": "anchor_relative",
          "anchor": {
            "text": "Invoice No",
            "match": "fuzzy", "min_ratio": 0.85,
            "occurrence": "first",
            "search_scope": { "page": 1, "region_norm": [0.5, 0.05, 1.0, 0.30] }
          },
          "value_window": {
            "direction": "right",                   // right | below | left | above
            "max_distance_pt": 220,
            "vertical_tolerance_pt": 6,             // same-line band
            "stop_at_anchor": ["Invoice Date", "Date"]
          },
          "pattern": "^[A-Z]{2,4}[/-]?\\d{3,8}$",
          "post": ["strip", "collapse_ws", "upper"]
        },
        {
          "type": "region_absolute",                // fallback only
          "page": 1,
          "bbox_norm": [0.62, 0.11, 0.95, 0.15],
          "tolerance_pt": 8,
          "pattern": "^[A-Z]{2,4}[/-]?\\d{3,8}$"
        }
      ],
      "validators": [
        { "type": "regex", "pattern": "^[A-Z]{2,4}[/-]?\\d{3,8}$" },
        { "type": "length", "min": 4, "max": 24 }
      ],
      "on_ambiguous": "review",                     // review | first | fail
      "confidence_floor": 0.80
    }
  ],

  "tables": [ /* see §8 */ ],

  "cross_checks": [                                 // §14 — accuracy backbone
    { "id": "line_sum",
      "expr": "abs(sum(lines.amount) - subtotal) <= max(1.0, 0.005 * subtotal)",
      "on_fail": "review" },
    { "id": "grand_total",
      "expr": "abs(subtotal + tax_total - grand_total) <= 1.0",
      "on_fail": "review" }
  ],

  "output": {
    "sheet_group": "invoice",
    "column_order": ["invoice_number", "invoice_date", "po_number", "grand_total"]
  }
}
```

`cross_checks.expr` must be evaluated by a **restricted expression evaluator over a whitelist of names and operators** — an AST walker, not `eval()`. See §18.3.

---

## 7. How visual field selection should work

### 7.1 The core correction

Your spec says: *"click/drag a rectangular area around a field."* If the rectangle becomes the extraction rule, the template breaks on the second invoice, because:

- An invoice with 3 line items and one with 30 push the totals block hundreds of points down the page.
- Multi-page documents put the totals on page 2 or 4 depending on length.
- A4 vs Letter shifts everything.
- A rescanned page is offset 5 mm and rotated 0.8°.
- Address blocks of different lengths shift everything below them.

**The rectangle is an authoring gesture, not the rule.** When the user drags a box, the app *infers* a robust rule from what's inside and around it.

### 7.2 Authoring flow

```
User drags a box around "INV/2024/00871"
        ↓
App captures: words inside box, words in a 200pt neighbourhood, page, geometry
        ↓
App proposes candidate strategies, ranked:
   ① anchor_relative — nearest stable label to the left is "Invoice No."
        → value is right-of, same line band, ≤ 220pt
        → inferred pattern: ^[A-Z]{3}/\d{4}/\d{5}$   (generalised from the sample)
        confidence: high  ✓ recommended
   ② regex_page — this token matches a document-unique pattern; only 1 hit on page
        confidence: medium
   ③ region_absolute — fixed box  ⚠ "will break if the layout shifts"
        confidence: low
        ↓
User picks one (① preselected). App shows LIVE PREVIEW of what it extracted.
        ↓
User clicks "Test on other samples" → runs against every PDF in samples/
        ↓
Result table: sample_001 ✓ INV/2024/00871 · sample_002 ✓ INV/2024/00912
              sample_003 ✗ NOT_FOUND
        ↓
User adds sample_003, adjusts, retests until green.
```

**Insist on ≥3 sample PDFs per template before allowing "Save".** A template validated against one document is a guess. Warn loudly if only one sample exists — this is the single biggest predictor of a template that fails in production. Make it a warning, not a hard block, but make it prominent.

### 7.3 Anchor inference heuristics

When the user boxes a value, find the anchor by scoring nearby text:

- Prefer text to the **left on the same line band**, then **directly above**.
- Prefer text ending in `:` — a strong label signal.
- Prefer tokens present in **all** loaded samples at a similar relative position (stability test).
- Reject candidates that are themselves numeric/date-like (those are values, not labels).
- Reject candidates appearing >3 times on the page (not discriminative).
- Store the anchor with fuzzy matching enabled (`rapidfuzz`, ratio ≥ 0.85) so OCR noise — `Invoice N0.` — still matches.

### 7.4 Pattern generalisation

From the boxed sample value, generalise a regex conservatively and **show the user the regex in plain language**, editable:

```
Sample: INV/2024/00871
Inferred: 3 letters, slash, 4 digits, slash, 5 digits
Regex:    ^[A-Z]{3}/\d{4}/\d{5}$
[ ] Allow variable digit counts        [ ] Allow lowercase
```

Two or three samples let you widen safely (`\d{4,6}` instead of `\d{5}`). One sample forces you to guess — another argument for the 3-sample minimum.

### 7.5 Editor UI requirements

- `QGraphicsView`/`QGraphicsScene` with pypdfium2-rendered pages as pixmaps. **Not** a `QLabel` with a pixmap — you need scene coordinates, item selection, and resize handles.
- Render at 2× device pixel ratio and honour Windows display scaling (`Qt::AA_EnableHighDpiScaling`), or boxes will land in the wrong place on 150% DPI laptops. This is a real and frequently-shipped bug.
- Render lazily and cache: a 200-page PDF must not render 200 pages on open.
- Word-level snapping: dragging near text snaps the box to whole word bounds. Removes pixel-hunting.
- Overlay all defined fields as coloured, labelled rectangles simultaneously.
- Live extraction preview panel, updating on every edit.
- Zoom, pan, page navigation, rotate.
- **Undo/redo** in the editor. Users will draw 20 boxes and mis-click one.

---

## 8. How tables should be represented

Be clear-eyed: **line-item table extraction is the hardest part of this project and will consume more effort than everything else combined.** Budget accordingly. It is also where "just draw a rectangle" fails most completely.

### 8.1 Table definition model

```jsonc
{
  "name": "line_items",
  "start": {                                   // where the table begins
    "type": "header_row",
    "header_tokens": ["Sr", "Description", "HSN", "Qty", "Rate", "Amount"],
    "min_tokens_matched": 4,                   // tolerate OCR losing one
    "match": "fuzzy"
  },
  "end": {                                     // where it stops — CRITICAL
    "type": "any_of",
    "conditions": [
      { "type": "anchor", "text": ["Total", "Sub Total", "Grand Total", "Taxable Value"] },
      { "type": "blank_rows", "count": 2 },
      { "type": "columns_lost", "min_columns": 3 },
      { "type": "page_end" }
    ]
  },
  "columns": [
    { "name": "sr_no",       "x_range_norm": [0.04, 0.10], "type": "int",     "required": false },
    { "name": "description", "x_range_norm": [0.10, 0.46], "type": "string",  "required": true,
      "multiline": true },
    { "name": "hsn",         "x_range_norm": [0.46, 0.56], "type": "string",  "pattern": "^\\d{4,8}$" },
    { "name": "quantity",    "x_range_norm": [0.56, 0.66], "type": "decimal", "align": "right" },
    { "name": "unit_price",  "x_range_norm": [0.66, 0.80], "type": "decimal", "align": "right" },
    { "name": "amount",      "x_range_norm": [0.80, 0.96], "type": "decimal", "align": "right",
      "required": true }
  ],
  "column_detection": "rulings_then_gaps",     // rulings | gaps | fixed | rulings_then_gaps
  "row_grouping": {
    "strategy": "anchor_column",               // rows start where sr_no or amount appears
    "anchor_columns": ["sr_no", "amount"],
    "y_tolerance_pt": 3,
    "wrapped_lines": "append_to_previous"      // handles multi-line descriptions
  },
  "multipage": {
    "continues": true,
    "repeat_header": true,                     // skip repeated headers on page 2+
    "skip_rows_matching": ["^Continued", "^Carried Forward", "^B/F"]
  },
  "row_filters": {
    "drop_if_matches": ["^Sub ?Total", "^Total", "^CGST", "^SGST", "^IGST", "^Round(ing)? Off"],
    "drop_if_all_numeric_columns_empty": true
  },
  "validation": {
    "row_check": "abs(quantity * unit_price - amount) <= max(1.0, 0.01 * amount)",
    "table_check": "abs(sum(amount) - $subtotal) <= max(1.0, 0.005 * $subtotal)"
  }
}
```

### 8.2 Detection algorithm

```
1. Find header row       — fuzzy-match header_tokens across lines; take best-scoring line.
2. Establish columns:
     a. If ≥3 vertical rulings span the table's y-range (pdfplumber gives you vector
        rects/lines) → use them as column boundaries. Most reliable.
     b. Else → derive from header token x-centres, then refine with a vertical
        whitespace-gap projection over the body region.
     c. Else → fall back to the template's stored x_range_norm.
3. Collect body words between header_y and the end condition.
4. Group into rows:
     - cluster words by y-centre within y_tolerance_pt
     - a row *starts* where an anchor column (sr_no / amount) has content
     - lines with content only in `multiline` columns append to the previous row
5. Assign words to columns by x-centre containment; right-align numeric columns.
6. Apply row_filters, then per-row validation.
7. If multipage: repeat from step 1 on the next page, skipping the repeated header.
```

### 8.3 Row-grouping is the real difficulty

The naive "cluster by y" fails on:

- **Wrapped descriptions** — one logical row spanning 3 physical lines. Fixed by `anchor_column` row starts.
- **Tax rows interleaved** with item rows. Fixed by `drop_if_matches`.
- **Rows split across a page break.** Detect: last row on page N has `description` but no `amount`, first row on page N+1 has `amount` but no `sr_no` → merge.
- **Nested/grouped items** (a parent SKU with sub-lines). Out of scope for MVP; flag for review.
- **Right-aligned numbers overflowing their column** on wide amounts. Fixed by using x-centre containment with a tolerance, plus alignment hints.

### 8.4 The critical guard

**`sum(line_items.amount)` must reconcile with `subtotal`.** If it does not, the table extraction is wrong — a dropped row, a merged row, a misread digit. This check catches nearly every table failure mode, and it is nearly free. Route mismatches to Review with the delta shown. Do not export a reconciliation failure as if it were clean data.

### 8.5 Output shape

Line items are **1:N with the document**. They cannot share a flat sheet with header fields without either duplicating header data on every row or leaving ragged blanks. See §15 for the sheet design.

---

## 9. How template matching should work

### 9.1 Two-stage: cheap candidate generation, then expensive verification

Scoring 100 templates × 1,000 documents by running full extraction is 100,000 extraction passes. Don't.

**Stage 1 — Candidate generation (fast, indexed, aims for recall):**

```
Extract from page 1 (and page 2 if page 1 is a cover):
  - all identifier-pattern hits: GSTIN, PAN, CIN, VAT, IBAN, phone, email domain, URL
  - normalised token set (uppercased, punctuation-stripped, stopwords removed)
  - 8×8 ink-density grid (coarse layout signature)
  - embedded image perceptual hashes (logos)  [v1.1]

Look up an inverted index (built at startup from all templates, cached in index.db):
  identifier_value → [template_ids]
  distinctive_token → [template_ids]

Take the top ~10 candidates.
```

**A GSTIN/VAT/tax-ID match is close to decisive** for vendor identity and should be weighted accordingly. It is machine-printed, format-constrained, checksum-verifiable, and unique per vendor. This is your strongest single signal — build the whole matcher around it where available.

**Stage 2 — Verification (accurate, aims for precision):**

For each candidate, compute a weighted score:

| Signal | Weight | Notes |
|---|---|---|
| Vendor identifier exact match (GSTIN/PAN/VAT) | 0.40 | Checksum-validated. Near-decisive. |
| Required fingerprint tokens present | 0.20 | All must be present, or the candidate is disqualified. |
| Anchor resolution rate | 0.25 | **What fraction of the template's field anchors actually resolve on this document?** The best behavioural signal. |
| Text profile cosine similarity | 0.10 | Hashed TF-IDF over page-1 tokens. |
| Layout signature distance | 0.05 | Ink-density grid. Weak on its own; useful tiebreaker. |

Forbidden tokens → hard disqualify. A "PROFORMA INVOICE" must not match the "TAX INVOICE" template even though 95% of the layout is identical. **Forbidden tokens are how you separate near-identical document types from the same vendor**, and they are easy to forget.

**Decision:**
```
best ≥ 0.85 and (best − second) ≥ 0.10   → ACCEPT, process
0.72 ≤ best < 0.85                        → LOW_CONFIDENCE → Review (extraction still runs, results shown)
best < 0.72                               → NO_MATCH        → Review
(best − second) < 0.10                    → AMBIGUOUS       → Review, show both candidates
```

That **margin check is as important as the absolute threshold.** Two vendors using the same accounting package produce near-identical invoices; a 0.91 vs 0.90 result is a coin flip that must not be silently resolved.

### 9.2 Why not ML for MVP

A classifier needs labelled data you don't have on day one, it can't explain itself ("why did this become Vendor B?"), and it bloats the bundle. The rule-based scorer above is explainable, debuggable, tunable per template, and works from a single sample. **Keep the option open**: log every match decision with its feature vector, so in a year you have a labelled training set if you want one.

### 9.3 Multi-document PDFs

A scanned batch is often one PDF containing 40 invoices. Detect it: if page 2+ scores as a *new* document start (its own header block, its own fingerprint match, a new invoice number), split. Offer a "split multi-document PDFs" toggle. **Flag this as a real requirement now** — it's missing from your spec and it is extremely common with scanner output.

---

## 10. How vendor identification should work

Separate two questions your spec conflates:

- **Which vendor is this?** — an entity resolution problem.
- **Which template applies?** — a layout matching problem.

They differ: one vendor may have 4 templates (invoice, PO, delivery note, statement) and may have changed layout twice. Model vendors as first-class, with templates belonging to them.

**Resolution order:**

1. **Tax identifier** (GSTIN/PAN/VAT/EIN) with checksum validation → deterministic. Maintain `Templates/_registry/vendors.json` mapping identifier → vendor_slug.
2. **Registered name exact match** against the vendor's `display_name` + `aliases`, after normalisation (uppercase, strip `PVT LTD`/`PRIVATE LIMITED`/`LLP`/`&`/punctuation).
3. **Fuzzy name match** (`rapidfuzz.token_set_ratio` ≥ 90) → accept but log; ≥ 80 → propose to user, don't auto-accept.
4. **Fallback:** the matched template's `vendor.display_name` (a constant, zero extraction risk).

**Never derive the vendor from the largest text on the page or the top-left block.** Invoices routinely put the *customer's* name and logo in the most prominent position, or use a shared print-house header. This heuristic looks obvious and is wrong often enough to corrupt your filing.

**Never write an extracted vendor string directly to disk as a folder name.** Resolve to a registered `vendor_slug` first. See §18.2.

**GSTIN validation (relevant to your field list):** 15 characters — 2-digit state code, 10-character PAN, 1 entity number, `Z`, then a check digit computed by a documented mod-36 weighted algorithm. Implement the checksum. It converts a fuzzy OCR read into a verified identity or a definite error, with no middle ground. Same for PAN structure (`AAAAA9999A`) and, if you go international, IBAN mod-97.

---

## 11. How confidence scoring should work

### 11.1 Principles

- **Explainable, not a magic number.** Every score carries the list of components that produced it. The Review screen must answer "why is this 0.62?" in one glance.
- **Composed of independent evidence**, combined multiplicatively for hard gates and as a weighted mean for soft signals.
- **Calibrated by testing**, not invented. Run your golden fixtures, plot score vs. correctness, set thresholds where the curve separates. A threshold picked by feel is worthless.
- **Never used to fill a gap.** Confidence describes an extracted value; it never manufactures one.

### 11.2 Per-field score

```
field_confidence = template_match_score
                 × strategy_score          (anchor_exact 1.00 · anchor_fuzzy 0.85–0.99
                                            · regex_page_unique 0.95 · region_absolute 0.70
                                            · fallback_strategy × 0.85)
                 × ocr_score               (min per-word Tesseract conf/100; 1.0 for text layer)
                 × pattern_score           (regex match 1.00 · partial 0.60 · none 0.30)
                 × uniqueness_score        (exactly 1 candidate 1.00 · 2 candidates 0.50
                                            · 3+ 0.25)
                 × type_score              (parsed cleanly 1.00 · coerced 0.80 · failed 0.20)
                 × cross_check_score       (all checks pass 1.00 · none applicable 0.95
                                            · any fail 0.40)
```

Multiplicative is deliberate: any single strong negative signal should drag the score down. A perfectly-positioned value that fails its regex is *not* 80% confident.

### 11.3 Status taxonomy

Your five statuses are good. Sharpen them into a mutually exclusive enum with mandatory reason codes:

| Status | Meaning | Action |
|---|---|---|
| `EXTRACTED` | Value found, validated, above `confidence_floor` | Export |
| `LOW_CONFIDENCE` | Value found, below floor | Export **flagged** + queue for review |
| `AMBIGUOUS` | >1 candidate, no clear winner | **Do not export a value.** Review, show all candidates |
| `MISSING` | Field is optional; no candidate found | Export as empty |
| `NOT_FOUND` | Field is **required**; no candidate found | Review |
| `INVALID` | Found but failed validator/checksum | Review, show raw value + which validator failed |
| `FAILED` | Extraction raised an exception | Review + full traceback in log |

Document-level status is derived: `OK` (all fields EXTRACTED, all cross-checks pass) · `NEEDS_REVIEW` (any LOW_CONFIDENCE / AMBIGUOUS / NOT_FOUND / INVALID, or any cross-check failure) · `FAILED` (unreadable, matcher failed, or crashed).

**Reason codes are mandatory** — machine-readable strings, not prose: `ANCHOR_NOT_FOUND`, `REGEX_MISMATCH`, `OCR_CONF_LOW`, `MULTIPLE_CANDIDATES`, `LINE_SUM_MISMATCH`, `GSTIN_CHECKSUM_FAIL`, `TEMPLATE_MARGIN_LOW`. These make the processing report aggregable — "37 documents failed with `LINE_SUM_MISMATCH`" tells you exactly which template to fix.

### 11.4 Thresholds

Per-template, overridable per-field, with sensible defaults (`confidence_floor: 0.80`, `template min_score: 0.72`, `review_below: 0.85`). Expose them in Settings with a warning that lowering them trades accuracy for throughput. Log the threshold values used in every run manifest — otherwise a run's results are not reproducible.

---

## 12. How to handle changed vendor PDF layouts

This will happen, on a timescale of months, and it is where most extraction products quietly rot.

### 12.1 Template versioning

Templates carry a `version` integer and are archived to `versions/vN.json` on every save. Every extraction result records `template_id`, `version`, and `template_hash`. Support rollback from the Template Library.

### 12.2 Drift detection

Track per-template rolling statistics in the SQLite index (or a JSONL stats file): anchor resolution rate, per-field success rate, mean confidence, cross-check pass rate — over the last N documents.

**Alert when a batch deviates from the template's historical baseline:** e.g. `invoice_date` has a 99% success rate over 4,300 documents, but 12% in today's batch of 200. That's a layout change, and the app should say so explicitly on the Dashboard rather than making the user notice 188 review items and infer the cause.

This is a genuinely differentiating feature and it is cheap to build once you have the stats table. It is also the strongest single argument for the SQLite index.

### 12.3 Multi-variant templates

Rather than forcing one template to cover both old and new layouts (which weakens the fingerprint and invites mismatches), allow **variants under one template_id**:

```
acme-industries__invoice
  ├── variant "2023-layout"   effective_to:   2024-03-31
  └── variant "2024-layout"   effective_from: 2024-04-01
```

The matcher scores variants independently and picks the best. Old archived documents still process correctly. Shared field definitions can be inherited by a variant and selectively overridden.

### 12.4 Golden regression fixtures — do not skip this

Each template keeps `samples/*.pdf` alongside `samples/*.expected.json` (the verified-correct extraction). A **"Validate Template"** button re-runs every sample and diffs against expected.

Consequences:
- Editing a template to fix vendor B's new layout can no longer silently break the 3,000 old documents — you find out in 4 seconds.
- New users get a working "is my template any good?" answer.
- You can ship a CI job that validates all bundled templates on every build.

Without this, template editing is Russian roulette. This is the most important recommendation in §12.

### 12.5 Layout-change UX

When drift is detected, offer: **"Clone this template and update it against a new sample."** Pre-load the failing document in the editor, highlight which anchors failed in red, let the user re-draw only those. Re-teaching a template must take 2 minutes, not 20, or users will simply stop maintaining them and start correcting spreadsheets by hand — at which point your product has failed.

---

## 13. How to process 1,000+ PDFs safely

### 13.1 Concurrency model

PDF parsing and OCR are **CPU-bound**. Threads will not help — the GIL serialises them. You need processes.

```
Main thread (Qt event loop)  ─── never blocks, never touches a PDF
        │  signals/slots
QThread  (BatchSupervisor)   ─── owns the ProcessPoolExecutor, drains a result queue,
        │                        emits progress signals
ProcessPoolExecutor          ─── N = min(cpu_count - 1, 6) workers, each fully independent
   ├── worker 1 → parse → match → extract → write Work/runs/<id>/results/<doc>.json
   ├── worker 2 → ...
   └── worker N → ...
```

Workers write their own result JSON and return a **small** status dict. Do not pass extracted page models back through the pickle boundary — 1,000 documents × full word lists will exhaust memory and pickling dominates runtime.

**Excel writing happens once, in a single process, at the end** — after the journal is complete. Never let workers touch the workbook.

### 13.2 The PyInstaller + multiprocessing trap

On Windows, `multiprocessing` uses `spawn`: it re-executes the program to create each worker. In a PyInstaller EXE, that means **re-launching your EXE, which shows the splash screen, which spawns more workers, recursively.** A fork bomb, from a first-time user's double-click.

```python
# The FIRST executable statement in the entry point, before any imports that matter,
# before Qt, before anything:
if __name__ == "__main__":
    multiprocessing.freeze_support()
    ...
```

Also set `multiprocessing.set_start_method("spawn")` explicitly, and ensure worker functions are module-level (picklable) and import nothing Qt-related.

### 13.3 Robustness requirements

| Risk | Mitigation |
|---|---|
| One malformed PDF hangs forever | **Per-file hard timeout** (default 120 s, configurable). `future.result(timeout=...)`; on timeout, kill the worker and restart the pool. Non-negotiable — malformed PDFs *do* cause infinite loops in parsers. |
| Worker crashes (segfault in a native lib) | `BrokenProcessPool` → mark that file `FAILED`, rebuild the pool, continue. Never abort the batch. |
| Memory exhaustion | Cap workers by available RAM (OCR at 300 DPI is ~50–150 MB/page peak). Recycle workers every ~50 tasks (`max_tasks_per_child`) to bound leaks in native libs. |
| Decompression / page-count bombs | Reject PDFs > `max_pages` (default 200) and > `max_file_mb` (default 100) with a clear reason. |
| Crash mid-batch | Append-only `journal.jsonl` after each file. On restart, offer **Resume** — skip files already journalled. |
| Duplicate processing | Content hash (SHA-256) of each input; skip or flag if already processed. Also a business-level dedup on `(vendor, invoice_number)`. |
| Antivirus locking a file mid-read | Retry with backoff (3 attempts, 0.5/2/5 s) on `PermissionError`. Extremely common on Windows with real-time scanning. |
| Input files on a network share | Copy to local temp before processing. Do not parse over SMB — latency multiplies and connection drops mid-parse produce confusing errors. |
| User closes the app mid-run | Intercept `closeEvent`, offer Cancel/Pause/Continue-in-background. Terminate the pool cleanly; never orphan processes. |

### 13.4 Progress reporting

Emit at most **~10 updates/second** to the GUI. Naively signalling per-file completion from 6 workers can flood the Qt event queue and freeze the UI — a freeze caused by the progress bar, which is a particularly annoying bug to diagnose. Aggregate in the supervisor thread and emit on a timer.

Show: files done / total, current file name, per-status counts (OK / review / failed), elapsed, ETA, throughput (files/min), and a **Cancel** and **Pause** button that actually work.

### 13.5 Expected throughput (plan around these)

| Path | Per page | 1,000 single-page docs, 6 workers |
|---|---|---|
| Text PDF, header fields only | 50–150 ms | **~30 s** |
| Text PDF, header + table | 150–400 ms | **~1–2 min** |
| OCR, 300 DPI, Tesseract | 1.5–4 s | **~8–15 min** |
| OCR + preprocessing + retry pass | 3–7 s | **~15–25 min** |

**Design the UI around the OCR numbers, not the text numbers.** A 20-minute run needs a persistent, minimisable progress view and resumability — not a modal dialog.

---

## 14. How to prevent incorrect data extraction

This section is where your product either earns trust or destroys it. A missing value costs a minute of a clerk's time. A **wrong** value that looks right can post a wrong payment. Design for the asymmetry: **the cost of a false extraction vastly exceeds the cost of a review flag.**

### 14.1 Hard rules for the engine

1. **No inference, ever.** If the anchor isn't found, the answer is `NOT_FOUND`. Never "the number nearest where it usually is."
2. **Ambiguity is not resolved by ordering.** Two regex matches in the search window → `AMBIGUOUS`. Not "take the first." (Allow a per-field `on_ambiguous: "first"` override, off by default, with a warning in the editor.)
3. **No cross-field borrowing.** Never fill `invoice_date` from a date found near the PO number.
4. **No LLM/generative fallback.** Do not add "let the model guess" as a rescue path. It produces fluent, plausible, wrong invoice numbers — the worst possible failure mode for this product.
5. **Empty is a valid, honest answer.** Ship it as such.
6. **Every value carries provenance:** page, bbox, strategy used, raw text before post-processing. The Review screen shows the cropped image next to the value so a human verifies in one second.

### 14.2 Validation layers (all of them, in order)

1. **Type parse** — decimal, date, int, string. Failure → `INVALID`.
2. **Format regex** — per field.
3. **Checksum** — GSTIN (mod-36), PAN structure, IBAN (mod-97), EAN/GTIN. Converts fuzzy reads into verified facts.
4. **Range/sanity** — date within `[today − 5 y, today + 1 y]`; quantity > 0; amount ≥ 0 unless the doc type is a credit note; unit price < 10⁹.
5. **Cross-field arithmetic** — the big one:
   - `sum(line_items.amount) ≈ subtotal`
   - `subtotal + tax_total − discount ≈ grand_total`
   - `quantity × unit_price ≈ line.amount` (per row)
   - `sum(cgst + sgst) ≈ igst_equivalent` where applicable
   Use tolerance `max(1.0, 0.5% × value)` to absorb legitimate rounding.
6. **Cross-document** — duplicate `(vendor, invoice_number)` → warn. Invoice date before the vendor's earliest known date → warn.
7. **Historical pattern** — invoice numbers for this vendor have always matched `^INV/\d{4}/\d{5}$`; this one doesn't → flag. Learned from the index, no ML needed.

Arithmetic cross-checks are **the highest-value accuracy mechanism available to you and they are entirely absent from your spec.** They catch OCR digit errors (`8`→`3`), dropped table rows, merged rows, and column misalignment — the failure modes that a per-field confidence score cannot see. Build them in from day one.

### 14.3 Locale traps — directly relevant to your field list

**Indian digit grouping.** `1,23,456.00` is twelve lakh… no — one lakh twenty-three thousand four hundred fifty-six. Standard parsers, `float()`, `locale.atof`, and pandas all mishandle it or silently produce `123456.0` only by luck of comma-stripping. Write an explicit amount parser:
- Strip currency symbols (`₹`, `Rs.`, `INR`, `$`) and whitespace.
- Handle trailing `CR`/`DR` and parenthesised negatives `(1,234.00)`.
- Detect the decimal separator by position (last separator with exactly 2 following digits).
- Strip all grouping separators regardless of grouping *pattern* — this makes Indian and Western grouping both work.
- Reject anything left with a non-numeric character. Do not coerce.

**Date ambiguity.** `03/04/2024` is 3 April in India and 3 March in the US. **Never call `dateutil.parser.parse()` without constraints** — it will silently pick one. Instead:
- Store an ordered list of accepted formats per template (`["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d.%m.%Y"]`).
- Try them in order; take the first that parses.
- If the value is ambiguous under the template's formats (day ≤ 12 and month ≤ 12 with two competing formats accepted), flag `AMBIGUOUS` rather than choosing.
- Default `dayfirst=True` for an India-first deployment, but make it a template property, not a global.

**Also:** `₹` may extract as `?`, `Rs`, or `\uf0b9` depending on the font. Normalise. Unicode `NFKC` every extracted string. Strip zero-width and non-breaking spaces — they routinely break regex matches invisibly.

### 14.4 The Review workflow

- Side-by-side: rendered page (with the field's bbox highlighted) | editable value | reason code.
- Keyboard-driven: `Tab` between fields, `Enter` accept, `Esc` reject. A clerk reviewing 200 items must not touch the mouse.
- **"Fix template from this correction"** — when a user corrects a value, offer to update the template's anchor/pattern. This turns review labour into template improvement, which is the flywheel that makes the product get better instead of staying equally annoying.
- Every correction is written to an audit trail: who, when, old value, new value, reason. Corrected values are marked in the Excel output (see §15) so downstream consumers know a human touched them.
- Reviewed documents re-export into the run's workbook, or into a supplementary "corrections" workbook.

---

## 15. How Excel output should be structured

### 15.1 The 1:N problem

Header fields (one per document) and line items (many per document) cannot share a flat sheet. Your `Output/Vendor_A/Invoice.xlsx` implies one flat sheet, which forces either header duplication on every line row or ragged blanks. Both are painful downstream.

### 15.2 Recommended workbook

One workbook per run, with these sheets:

**`Summary`** — run metadata. Run ID, timestamp, app version, settings/thresholds used, input folder, counts by status, per-template breakdown, duration. Makes the run auditable and reproducible.

**`Documents`** — one row per document, wide.
```
doc_id | source_file | source_hash | page_count | template_id | template_version |
vendor | doc_type | match_score | status | review_reasons |
invoice_number | invoice_date | po_number | customer_name | gst_number |
subtotal | tax_total | grand_total | currency | line_item_count | processed_utc
```
`doc_id` is a stable UUID and is the join key.

**`LineItems`** — one row per line item.
```
doc_id | line_no | sr_no | description | hsn | quantity | unit_price | amount |
tax_rate | line_status | page | confidence
```
`doc_id` joins to `Documents`. This is a proper relational shape; users can PivotTable it, `XLOOKUP` it, or load it into Power Query without reshaping.

**`FieldDetail`** *(optional, off by default)* — one row per extracted field: `doc_id | field | value | status | confidence | strategy | page | bbox | reason`. Verbose but invaluable for tuning templates and for audit. Toggle in Settings.

**`Exceptions`** — every field that needs attention: `doc_id | source_file | field | raw_value | status | reason_code | confidence | suggested_action`. **This is the sheet the user actually opens first.** Put it second in tab order, after Summary.

**`Failed`** — documents that could not be processed: file, reason, error class, log pointer.

### 15.3 Formatting rules (Excel will corrupt your data if you let it)

- **Invoice numbers, PO numbers, GSTINs, HSN codes: write as text**, with `number_format = '@'`. Otherwise `0012345` becomes `12345`, `1-2` becomes `2-Jan`, and `1E5` becomes `100000`. This destroys data silently and is the most common complaint about extraction tools.
- **Amounts as real numbers** (`float`), formatted `#,##0.00`. Never write `"1,234.00"` as a string.
- **Dates as `datetime.date` objects** with `number_format = 'DD-MMM-YYYY'` — unambiguous, unlike `DD/MM/YYYY`.
- Freeze the header row, add an AutoFilter, set sensible column widths.
- **Conditional formatting:** amber fill for `LOW_CONFIDENCE`, red for `AMBIGUOUS`/`INVALID`/`NOT_FOUND`, a distinct fill for human-corrected values.
- Cell comments carrying the confidence score and reason on flagged cells.
- `openpyxl` in `write_only=True` mode for anything over ~10,000 rows — otherwise memory and time both blow up. Note it disables some styling APIs; apply named styles instead.
- Guard the 1,048,576-row limit: if `LineItems` would exceed it, split into `LineItems_1`, `LineItems_2`, and say so in `Summary`.

### 15.4 Also emit machine-readable sidecars

Write `Output/<run_id>/data/documents.csv`, `line_items.csv`, and `results.jsonl` alongside the workbook. Excel is a *report* for humans. Anyone integrating this into an ERP will want CSV/JSONL, and you get it for near-zero cost. It also gives you a clean re-export path if the workbook needs regenerating.

### 15.5 On per-vendor split

Your proposed `Output/Vendor_A/Invoice.xlsx` layout should be an **export option**, not the primary structure — a "Split by vendor" checkbox that produces the same sheet structure filtered per vendor. Cross-vendor analysis (which is most of what finance teams actually do) is far easier from one consolidated workbook.

---

## 16. How logging and error handling should work

### 16.1 Logging

- **Two sinks:** rotating human-readable `Logs/app.log` (10 MB × 5) and structured JSONL `Logs/runs/<run_id>.jsonl` for machine analysis.
- **Correlation IDs** on every record: `run_id`, `doc_id`, `template_id`. Without these, a 6-worker parallel log is unreadable.
- Multiprocess logging: workers must not write the same file concurrently. Use a `QueueHandler` in workers → a `QueueListener` in the supervisor process that owns the file handle. Concurrent appends from 6 processes produce interleaved, corrupt lines.
- **Levels:** DEBUG (per-field decisions, off by default) · INFO (per-document outcome) · WARNING (review-worthy) · ERROR (document failed) · CRITICAL (run aborted).
- **Retention policy** with a default (90 days) and a "Clear logs" button. Logs grow without bound otherwise.
- **PII:** invoice logs contain customer names, addresses, tax IDs, and amounts. At INFO level, log identifiers and statuses, not values. Full values only at DEBUG, and say so plainly in Settings. Never log full page text at INFO.

### 16.2 Error handling

Three tiers:

| Tier | Example | Behaviour |
|---|---|---|
| **Document** | Corrupt PDF, no template match, extraction exception | Catch, record status + traceback, continue batch. **Never propagates.** |
| **Batch** | Output folder unwritable, disk full, all workers dead | Halt gracefully, preserve journal, offer resume, tell the user what to fix. |
| **Application** | Unhandled exception in the GUI thread | Global `sys.excepthook` + `threading.excepthook` → error dialog with the log path and a **"Copy diagnostics"** button. Never a silent close. |

**Disk-space check before starting a batch** — estimate output size, refuse to start below a threshold. Filling the disk mid-run corrupts everything downstream and is entirely preventable.

**Every user-facing error must state: what happened, which file, why, and what to do next.** "Extraction failed" is useless. "`invoice_2024_003.pdf`: page 1 has no text layer and OCR is disabled. Enable OCR in Settings → Processing, or convert the file." is actionable.

**The processing report** (your requirement) should exist in three forms: the `Summary` sheet, an HTML report in `Output/<run_id>/report.html` (shareable, no Excel needed), and the Dashboard view.

---

## 17. Packaging into an EXE

### 17.1 One-dir, not one-file

**Use `--onedir`.** One-file mode extracts the entire ~300 MB payload to `%TEMP%` on **every single launch** — 5–20 s cold start, heavy disk churn, and a large antivirus red flag (self-extracting-to-temp-then-executing is precisely the behaviour heuristic scanners hunt for). One-dir launches in 1–3 s and is far friendlier to AV.

Ship the one-dir output inside an **Inno Setup** installer. Users get a normal installer, Start Menu entry, and uninstaller. Also publish a portable ZIP for locked-down environments.

### 17.2 Build specifics

- **`freeze_support()` first**, per §13.2.
- Bundle `tesseract.exe` + `tessdata/eng.traineddata` as `datas`. Resolve paths through a helper that checks `sys._MEIPASS` when frozen and the source tree otherwise. Hard-coded relative paths break the moment you freeze.
- `--noconsole` sets `sys.stdout`/`sys.stderr` to `None`. Libraries that print will raise `AttributeError`. Redirect both to the logger at startup, before importing anything else.
- **Exclude unused Qt modules** — this is the difference between a 500 MB and a 250 MB bundle. Exclude `QtWebEngine`, `Qt3D*`, `QtCharts`, `QtDataVisualization`, `QtQuick*`, `QtMultimedia`, `QtQml`, `QtNetworkAuth`, `QtBluetooth`, `QtSql` unless actually used. Verify with `pyi-archive_viewer`.
- Exclude `tkinter`, `test`, `unittest`, `pydoc`, `distutils`.
- Set version info (`--version-file`), an icon, and a manifest declaring DPI awareness (`permonitorv2`) — without it your app is blurry on scaled displays.
- Build on the **oldest Windows version you intend to support** (Win10 x64); forward compatibility works, backward doesn't. Ship the VC++ redistributable DLLs or require the redist.
- Deterministic builds: clean venv, `requirements.lock`, build script in CI, embed the git SHA and build timestamp in the About dialog and in every run manifest.

### 17.3 Code signing — budget for this

**Unsigned PyInstaller EXEs trigger SmartScreen ("Windows protected your PC") and are frequently flagged by antivirus as false positives.** For a business tool distributed to office users, this alone can kill adoption — the app will look like malware to exactly the cautious IT department you need to convince.

- Buy an **OV or EV code-signing certificate**. EV grants immediate SmartScreen reputation; OV builds reputation over weeks/months of downloads.
- Sign the EXE **and** the installer.
- If AV false positives persist, submit samples to the major vendors' false-positive portals.
- **Alternative: Nuitka** compiles to real C, produces substantially fewer AV false positives and faster startup, at the cost of longer build times and occasionally awkward PySide6 support. Worth evaluating in a spike if signing doesn't resolve the AV noise. Note Nuitka's commercial edition has different licensing.

Expected final size: **250–400 MB** one-dir with PySide6 + Tesseract + OpenCV. Set that expectation with stakeholders now — someone will ask why a "small utility" is 300 MB.

### 17.4 Updates

MVP: "Check for updates" that opens a download page. Building a silent auto-updater is a project in itself and touches file permissions, running-process replacement, and signing. Defer it.

---

## 18. Security considerations

### 18.1 PDFs are hostile input

Treat every input PDF as attacker-controlled — invoices arrive by email from outside your organisation.

- **Parse in worker processes** with hard timeouts. Native PDF parsers have a long CVE history; process isolation means a crash is a failed file, not a compromised app.
- **Never enable JavaScript, embedded file extraction, or external resource loading.** pdfminer/PDFium don't execute JS by default — keep it that way, and don't add a "render with a browser engine" feature later.
- **Resource limits:** max pages (200), max file size (100 MB), max render pixels (guard against a page declaring 200,000×200,000 points — a decompression bomb that will OOM your machine).
- **Encrypted PDFs:** detect, prompt for a password once, never store passwords in plaintext or logs. If no password, fail cleanly with `ENCRYPTED`.

### 18.2 Path handling — a real vulnerability in your design

Your spec moves documents into folders named after the extracted vendor. **The vendor name comes from the PDF, which is untrusted.** A vendor name of `..\..\Windows\System32` or `CON` or a 400-character string causes path traversal, write failures, or worse.

Mandatory:
- **Never use extracted text directly in a path.** Resolve to a registered `vendor_slug` from the template registry; if unresolved, use `unknown_vendor`.
- Slug rules: ASCII, lowercase, `[a-z0-9_-]` only, max 48 chars, no leading/trailing dots or spaces, reject Windows reserved names (`CON PRN AUX NUL COM1-9 LPT1-9`), collision-suffix with a short hash.
- After building any path, **verify it resolves inside the intended root** (`Path.resolve().is_relative_to(root)`). Belt and braces.
- Respect the 260-character `MAX_PATH` limit or use `\\?\` extended paths. Deep vendor/date hierarchies plus long original filenames hit this in production.

### 18.3 Template files are untrusted

A shared `template.json` is code-adjacent — it contains regexes and expressions.

- **JSON only.** No pickle, no YAML `unsafe_load`, no `eval`.
- Validate against the pydantic schema on load; reject with a clear error rather than partially applying.
- **Regex DoS:** user-supplied patterns can catastrophically backtrack (`(a+)+$`). Enforce a match timeout (run regex in the worker with the per-file timeout as backstop), cap pattern length, and reject nested unbounded quantifiers with a linter warning in the editor.
- **`cross_checks.expr` must never touch `eval()`.** Implement a restricted AST evaluator: whitelist `ast.BinOp`, `ast.Compare`, `ast.Name` (against a bound namespace), numeric literals, and a fixed function set (`abs`, `sum`, `min`, `max`, `round`). Reject everything else. `eval()` on a file a user received by email is remote code execution.
- Warn on template import: "This template came from outside. Review its patterns before use."

### 18.4 Data at rest

Extracted invoice data is commercially sensitive and contains personal data (customer names, addresses, possibly tax IDs → GDPR/DPDP Act relevance).

- Document that the app stores plaintext locally and that folder ACLs are the security boundary.
- Offer a retention/purge policy for `Work/` and `cache/rasters/`.
- Cached page rasters are full images of confidential documents — include them in purge, and evict by LRU with a size cap.
- If a customer requires encryption at rest, point them to BitLocker/EFS rather than building your own crypto.

### 18.5 Network

Default to **zero outbound connections.** No telemetry, no update pings, no font/CDN fetches, no analytics. State this plainly in the docs and in Settings — it is a selling point for finance departments, and any exception will be found and will cost you trust.

### 18.6 Licence compliance (a security-adjacent obligation)

- **PySide6 is LGPL-3.0.** Distributing it requires that users can relink against a modified Qt. One-dir packaging with separate Qt DLLs materially helps this; one-file makes the argument harder. Ship the LGPL text and a written offer, and list all third-party licences in an `About → Licences` dialog and a `LICENSES/` folder.
- **PyMuPDF is AGPL** — excluded, per §3.1.
- Tesseract (Apache-2.0), pdfplumber/pdfminer.six (MIT), pypdfium2 (BSD-3/Apache-2.0), openpyxl (MIT), OpenCV (Apache-2.0) all require attribution only.
- Generate the notices file automatically at build time (`pip-licenses`) so it can't drift.

---

## 19. MVP vs future

### MVP (target: a genuinely useful v1)

**In scope:**
1. Data-root resolution, folder bootstrap, settings.
2. Template editor: load PDF, render, draw box, infer anchor-relative rule, name field, choose type, live preview, save.
3. Single-value fields only — anchor-relative + regex_page + region_absolute strategies.
4. **One simple table per template** (ruled or clearly-columnar, single-page).
5. Text-layer extraction (pdfplumber + pypdfium2). **No OCR.**
6. Manual template selection for a batch ("process this folder with template X").
7. Batch processing with a process pool, progress, cancel, per-file timeout, journal, resume.
8. Validation: type, regex, GSTIN checksum, arithmetic cross-checks.
9. Confidence scoring + the seven-status taxonomy + reason codes.
10. Review screen: crop + value + accept/edit, keyboard-driven.
11. Excel export: Summary / Documents / LineItems / Exceptions / Failed, correctly typed.
12. Logging, error tiers, processing report.
13. PyInstaller one-dir + Inno Setup + signing.
14. Golden fixtures + "Validate Template" button.

**Deliberately excluded from MVP:** automatic template matching, OCR, multi-page tables, template variants, drift detection.

Yes — automatic template identification is a headline feature, and I'm suggesting you ship without it. The reason: extraction accuracy is the product. If extraction is unreliable, auto-matching just distributes errors faster. Get extraction right against a manually-chosen template, then add matching in v1.1 when you have real documents to tune the scorer against. Manual selection is a completely acceptable v1 workflow for a user with 5 templates.

### v1.1
- Automatic template matching (§9), with the "review if low confidence / low margin" gate.
- OCR (Tesseract) with per-page triage, preprocessing, targeted field retry.
- Multi-page tables and page-break row merging.
- Template variants and effective dates.
- Duplicate detection across runs.

### v1.2
- Drift detection and alerting (§12.2).
- "Fix template from correction" flywheel.
- Multi-document PDF splitting.
- Watch-folder / scheduled processing.
- RapidOCR provider.
- CLI for headless/scheduled use.

### v2 and beyond
- Optional cloud OCR/Document-AI providers behind the same interface.
- Learned matching (using the logged feature vectors from v1.1 onward).
- Multi-user shared template repository with locking.
- ERP/accounting connectors (Tally, Zoho Books, SAP).
- Non-English OCR.
- Handwriting (needs cloud or a much heavier local model — set expectations low).

---

## 20. Major flaws in your proposed architecture — consolidated

1. **"No database" applied to runtime state.** Templates as JSON: correct. Run journals, results indexes, dedup lookups, and drift statistics as JSON: a performance and correctness problem. SQLite as a rebuildable cache costs nothing and is still "just a file." (§1.1)
2. **Writable folders next to the EXE.** Breaks under Program Files/UAC. (§2)
3. **Rectangle-based extraction as the primary mechanism.** Breaks on the second invoice. Anchors, not coordinates. (§7)
4. **PyMuPDF's AGPL licence.** A commercial-distribution blocker discovered late is expensive. (§3.1)
5. **Tables underestimated.** They are the majority of the work and need their own subsystem. (§8)
6. **No arithmetic cross-validation.** The highest-value accuracy mechanism, entirely absent. (§14.2)
7. **No template versioning, no regression fixtures.** Templates rot; without golden tests every edit is a blind change. (§12.4)
8. **`Output/Vendor/Invoice.xlsx` as the primary structure.** Overwrite-vs-append with no run boundaries, no undo, and a 1:N shape that doesn't fit one flat sheet. (§15)
9. **"Move documents to vendor folders."** Destructive by default. Copy, verify, then optionally delete — and only on explicit opt-in. (§2)
10. **Vendor name from PDF → folder name.** Path traversal and invalid-filename vector. (§18.2)
11. **`multiprocessing` + PyInstaller without `freeze_support()`.** Fork bomb on first launch. (§13.2)
12. **No per-file timeout.** One malformed PDF hangs the batch indefinitely. (§13.3)
13. **No resume.** A 20-minute OCR run that dies at 80% restarts from zero. (§13.3)
14. **Locale handling absent.** Indian digit grouping and `DD/MM` vs `MM/DD` will silently produce wrong amounts and wrong dates. Wrong, not missing. (§14.3)
15. **Corrupt text layers unaddressed.** PDFs with broken `ToUnicode` extract garbage that looks like success. (§4.1)
16. **Missing requirements not in your spec at all:** multi-invoice PDFs; encrypted PDFs; credit notes/negative amounts; multiple tax rates per line; rotated pages; audit trail for review corrections; template import/export; duplicate invoice detection; disk-space checks; high-DPI display scaling; code signing; concurrent access if `DATA_ROOT` is on a shared drive.
17. **Confidence scoring described but not specified.** An unspecified confidence number is a random number with a reassuring name. It must be composed of named evidence and *calibrated against a labelled set*. (§11)

**What you got right**, and should not be talked out of: offline-first; PySide6 over PyQt6 (licensing); JSON templates as human-readable source of truth; separating the Review queue from Output; refusing to auto-guess values; per-status taxonomy; process-level parallelism; the requirement that a failed PDF must not kill a batch. The skeleton is sound. The corrections above are about the joints.

---

## Final recommended architecture (implementation brief)

This section is written to be handed to a coding agent.

### Module layout

```
document_extractor/
├── __main__.py               # freeze_support() FIRST, then bootstrap
├── bootstrap.py              # PathResolver, logging setup, settings load, DI wiring
│
├── domain/                   # NO Qt IMPORTS — enforced by test
│   ├── models.py             # Word, Line, Page, Document, FieldResult, DocResult,
│   │                         #   Status enum, ReasonCode enum  (frozen dataclasses)
│   ├── pdf/
│   │   ├── loader.py         # open, page count, encryption, rotation, limits
│   │   ├── text_layer.py     # pypdfium2 probe + pdfplumber word/ruling extraction
│   │   ├── triage.py         # per-page TEXT | SCANNED | CORRUPT classification
│   │   └── raster.py         # pypdfium2 render at DPI, LRU cache
│   ├── ocr/
│   │   ├── provider.py       # OcrProvider interface
│   │   ├── tesseract.py      # subprocess, image_to_data → Word[], timeout
│   │   ├── preprocess.py     # grayscale, deskew, denoise, binarise, crop
│   │   └── retry.py          # targeted high-DPI re-OCR of low-confidence regions
│   ├── template/
│   │   ├── schema.py         # pydantic models, schema_version, migrations
│   │   ├── repository.py     # load/save/version/validate; atomic writes
│   │   ├── registry.py       # vendor registry, inverted index build
│   │   └── inference.py      # box → anchor/pattern inference for the editor
│   ├── matching/
│   │   ├── fingerprint.py    # identifiers, tokens, TF-IDF profile, layout grid
│   │   ├── candidates.py     # inverted-index lookup, top-K
│   │   └── scorer.py         # weighted score, margin check, decision
│   ├── extraction/
│   │   ├── strategies.py     # anchor_relative, regex_page, region_absolute,
│   │   │                     #   label_pair, table_cell, computed, constant
│   │   ├── anchors.py        # fuzzy anchor location, direction windows
│   │   ├── tables.py         # header find, column detect, row group, multipage
│   │   ├── types.py          # amount parser (Indian+Western), date parser, int
│   │   ├── validators.py     # regex, range, GSTIN/PAN/IBAN checksums
│   │   ├── crosscheck.py     # restricted AST evaluator — NEVER eval()
│   │   └── confidence.py     # composite score, status assignment, reason codes
│   └── export/
│       ├── workbook.py       # openpyxl, write_only, sheets, formats, cond. format
│       ├── sidecar.py        # CSV + JSONL
│       └── report.py         # HTML processing report
│
├── app/                      # orchestration — dataclasses in, dataclasses out
│   ├── job.py                # RunConfig, RunManifest, doc_id/run_id generation
│   ├── worker.py             # module-level process_one(path, cfg) -> StatusDict
│   ├── batch.py              # ProcessPoolExecutor, timeouts, pool restart, journal
│   ├── supervisor.py         # QThread bridge: drains queue, throttled progress
│   └── resume.py             # journal replay, skip-completed
│
├── gui/
│   ├── main_window.py
│   ├── dashboard.py
│   ├── library.py            # template list, validate, clone, version, import/export
│   ├── editor/
│   │   ├── canvas.py         # QGraphicsView, HiDPI, snapping, undo stack
│   │   ├── field_panel.py    # strategy picker, pattern editor, live preview
│   │   ├── table_panel.py    # header/columns/end-condition definition
│   │   └── validate_panel.py # run against samples, show diffs
│   ├── process.py            # folder pick, template pick, progress, cancel/pause
│   ├── results.py            # paged model over the index
│   ├── review.py             # crop + value + keyboard accept/edit + fix-template
│   └── settings.py
│
├── infra/
│   ├── paths.py              # data-root resolution, slugify, path containment check
│   ├── atomic.py             # tmp+os.replace, JSONL append, retry-on-locked
│   ├── index.py              # SQLite cache: build, query, rebuild-from-disk
│   ├── logging_setup.py      # rotating + JSONL, QueueHandler/QueueListener
│   └── settings.py
│
└── tests/
    ├── unit/                 # parsers, validators, scorers, path safety
    ├── fixtures/             # synthetic + real-shaped PDFs, expected JSON
    ├── golden/               # per-template regression runner
    └── smoke/                # pytest-qt: app launches, screens render
```

### Data contracts (stable, versioned)

- `template.json` — §6.1 schema, `schema_version` gated, pydantic-validated.
- `Work/runs/<run_id>/manifest.json` — run config, settings snapshot, thresholds, app version, git SHA, counts.
- `Work/runs/<run_id>/journal.jsonl` — one line per file: `{doc_id, source_path, source_hash, template_id, template_version, status, reason_codes, duration_ms, worker_pid, completed_utc}`.
- `Work/runs/<run_id>/results/<doc_id>.json` — full result: every field with value, status, confidence, strategy, page, bbox, raw text; all line items; all cross-check outcomes.
- `cache/index.db` — SQLite; tables `documents`, `fields`, `line_items`, `template_stats`, `vendor_index`. **Fully rebuildable** from `Work/` and `Templates/`.

### Build order

1. `infra` (paths, atomic, logging, settings) + `domain/models` + tests.
2. `domain/pdf` (loader, triage, text_layer, raster) + fixtures.
3. `domain/extraction` (types, validators, anchors, strategies for single values) + heavy unit tests. **This is the accuracy core — test it hardest.**
4. `domain/template` (schema, repository) + `domain/extraction/confidence`.
5. Minimal GUI: main window + template editor canvas + single-value field definition + live preview.
6. `app` (worker, batch, supervisor, resume) + process screen.
7. `domain/export` + Excel/CSV/report.
8. Review screen.
9. `domain/extraction/tables` + table panel. **Budget the most time here.**
10. Golden-fixture runner + "Validate Template".
11. Packaging, signing, installer.
12. *Then* v1.1: matching, OCR.

### Acceptance criteria for MVP

- 500 text PDFs across 3 templates process in under 3 minutes, GUI responsive throughout, cancel works within 2 seconds.
- Killing the process at 50% and restarting resumes without reprocessing completed files and without duplicate output rows.
- A deliberately corrupt PDF, a 300-page PDF, an encrypted PDF, and a zero-byte file each fail cleanly with distinct reason codes and do not stop the batch.
- Excel output opens in Excel with invoice numbers intact as text, amounts as numbers, dates as dates.
- On a template with 5 golden samples, "Validate Template" reports 5/5 pass; deliberately breaking one anchor reports the failure and names the field.
- Zero outbound network connections observed under Wireshark for a full run.
- `..\..\evil` as a vendor name produces no file outside `DATA_ROOT`.
- An amount of `1,23,456.00` parses to `123456.00`; `03/04/2024` under a `dayfirst` template parses to 3 April.
- Installed to `C:\Program Files\`, a standard (non-admin) user can create a template and run a batch.

---

## Open questions for you

1. **India-first, or multi-country from day one?** It changes date defaults, tax-ID validation, and amount parsing. I've assumed India-first with the locale handling parameterised per template.
2. **Single user, or a shared network `DATA_ROOT`?** Multi-user changes the concurrency and locking design substantially, and is much cheaper to design in now than to retrofit.
3. **What is the actual OCR share of your documents?** If it's under 10%, my MVP-without-OCR recommendation holds comfortably. If it's over half, OCR moves into MVP and the whole schedule stretches.
4. **How many line items on a typical document, and do tables ever span pages?** Multi-page tables roughly double the table subsystem's cost.
5. **Is there an ERP/accounting system downstream?** If so, its import schema should drive the Excel/CSV column design rather than the other way round.
6. **What accuracy bar makes this worth using?** "95% of fields correct with 5% flagged for review" and "99.9% with 15% flagged" are very different products and lead to different threshold defaults.
