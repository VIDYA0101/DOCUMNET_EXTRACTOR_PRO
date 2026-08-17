# Document Extractor

Offline Windows desktop application that reads PDFs, identifies which vendor
template applies, extracts predefined fields and line-item tables, and exports
to Excel. No database, no cloud, no network access.

---

## Building the EXE

You need a **Windows** machine with **Python 3.11 or 3.12 (64-bit)**. PyInstaller
cannot cross-compile, so the executable must be built on Windows.

```
git clone <this repo>      (or unzip it)
cd DocumentExtractor
build\build.bat
```

That is the whole build. It creates a virtual environment, installs pinned
dependencies, smoke-tests the imports, and packages the app. The result:

```
dist\DocumentExtractor\DocumentExtractor.exe
```

Then, in order:

```
:: 1. Sign it (see "Code signing" below - do not skip this)
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a ^
         dist\DocumentExtractor\DocumentExtractor.exe

:: 2. Build the installer
iscc build\installer.iss
```

### Running from source, without building

```
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m document_extractor                    # GUI
python -m document_extractor info               # CLI
python -m document_extractor process C:\Invoices
```

### Generating test data

```
python tests\make_fixtures.py tests\fixtures
```

Writes seven synthetic Indian GST invoices across two vendors, with varying
line-item counts so the totals sit at different heights on the page. That
variation is the point: it is the condition under which fixed-coordinate
extraction silently fails.

---

## Where things live after installation

```
C:\DocumentExtractor\
├── App\                      the executable and its DLLs (read-only)
└── Data\                     everything the user owns (writable)
    ├── Templates\<vendor>\<doctype>\
    │   ├── template.json     the current version
    │   ├── versions\         every previous version, for rollback
    │   └── samples\          golden PDFs + recorded expectations
    ├── Input\  Output\  Review\  Archive\
    ├── Logs\                 app.log + per-run JSONL
    ├── Work\runs\<run_id>\   journal.jsonl, manifest, per-document results
    ├── index\                append-only history (hashes, invoice keys)
    └── cache\                rebuildable; safe to delete at any time
```

**Do not install into `Program Files`.** The application writes beside itself,
and Program Files is read-only for standard users. The installer defaults to
`C:\DocumentExtractor` and refuses a Program Files path.

---

## How the code is organised

```
document_extractor/
├── __main__.py          entry point; freeze_support() FIRST, then GUI or CLI
├── cli.py               headless: process / validate / record / info
├── infra/
│   ├── paths.py         data-root resolution, slugify, traversal guard
│   ├── store.py         AtomicJson, AppendLog, MemoryIndex, InstanceLock
│   ├── settings.py      thresholds and formats
│   └── logging_setup.py rotating log + structured JSONL
├── domain/              pure Python, no Qt, fully testable headless
│   ├── models.py        Page/Word/FieldResult/DocResult; the status enums
│   ├── pdf/             loading, per-page triage, rendering
│   ├── extraction/      types, validators, anchors, strategies, tables,
│   │                    crosscheck, confidence, engine
│   ├── template/        pydantic schema + versioned repository
│   ├── matching/        two-stage template matcher
│   └── export/          Excel, CSV, JSONL, HTML report
├── app/batch.py         process pool, timeouts, journal, resume
└── gui/                 PySide6; thin, calls only into domain/ and app/
```

The domain layer has no Qt import anywhere. That is what makes the CLI, the
tests, and any future scheduled service possible without duplicating logic.

---

## The five decisions that matter

**1. Anchors, not rectangles.** A template stores "the value is to the right of
the text `Invoice No.`", not "the value is at (412, 96)". Coordinates are an
authoring gesture only. A 3-line invoice and a 30-line invoice from the same
vendor put the totals hundreds of points apart; a rectangle catches one of them.

**2. The system never invents a value.** Two candidates means `AMBIGUOUS` and no
exported value, not "pick the first". Missing anchor means `NOT_FOUND`. Every
uncertain outcome carries a machine-readable reason code.

**3. Arithmetic cross-checks.** `sum(line_items) ≈ subtotal` and
`subtotal + tax ≈ grand_total` catch what per-field confidence cannot see: a
dropped table row, a merged row, a column misalignment. This is the single
highest-value accuracy mechanism in the system, and it costs nothing to run.

**4. Templates are versioned and regression-tested.** Every save archives the
previous version. Every template keeps golden sample PDFs with recorded
expected output. "Validate" re-runs them and diffs. Without this, fixing one
vendor's new layout silently breaks the 3,000 documents that already worked.

**5. No AGPL dependencies.** PyMuPDF is the obvious PDF library and is AGPL-3.0;
shipping it would oblige you to release this source under the AGPL.
`pypdfium2` (BSD) and `pdfplumber` (MIT) are used instead. See
`LICENSES/THIRD_PARTY_NOTICES.md`.

---

## Code signing

Budget for this now: an EV certificate takes **weeks** of identity validation.

Without a signature, every user gets a full-screen SmartScreen warning on first
run, and PyInstaller executables are routinely quarantined by antivirus because
the bootloader pattern resembles a packer. A standard OV certificate helps but
still needs reputation to accumulate; an EV certificate gets SmartScreen
reputation immediately.

The build deliberately sets `upx=False` and `console=False` and ships a version
resource, all of which reduce false positives.

---

## Known limitations in this version

- **No OCR.** Scanned pages are triaged and reported as `NO_TEXT_LAYER`, never
  silently returned as empty. Tesseract integration is designed for but not
  built; add it once you know what fraction of your documents are scans.
- **Single user per data folder.** A lock file detects a second instance and
  warns. A shared network `Data` folder is not supported and will corrupt the
  journals.
- **Practical ceiling ~50,000 documents** in one data root, workable to about
  200,000. Beyond that the index logs need sharding by year. This is a
  consequence of the no-database requirement, not an accident.
- **No ad-hoc querying.** "Show me every invoice from vendor X over ₹50,000 in
  March" means loading the index into memory and filtering in Python. Fine at
  this scale, but it is the first thing that would justify revisiting SQLite.
- **The GUI has not been run on Windows by its author.** The engine is tested
  end to end (see below); the Qt layer is written against the documented API
  but needs a first-run pass on your machine.

---

## What has actually been verified

Tested on Linux against seven generated invoices across two vendors:

- Amount parsing: `₹1,23,456.00`, `1.234,56`, `(1,234.00)`, `1,234.00 CR`,
  space grouping, and rejection of non-numeric input
- Date parsing against an explicit format list, with genuine ambiguity flagged
  rather than guessed
- GSTIN / PAN / IBAN / Luhn checksums, verified against independently computed
  check characters
- Cross-check evaluator: correct arithmetic, dropped-row detection, and
  rejection of `__import__`, `open()`, lambdas and `__class__` access
- Field extraction: all fields correct on both 3-line and 7-line invoices from
  the same template, proving anchor-relative extraction survives layout shift
- Line items including wrapped descriptions
- Template matching: 7/7 correct, score 0.925, margin 0.550
- Full batch via multiprocessing: 7 PDFs, 2 workers, 7 clean, 0 failed
- Excel typing: invoice numbers and GSTINs as text (`@`), dates as real dates,
  amounts as `#,##0.00`
- Robustness: truncated, empty and non-PDF files fail individually and the
  batch continues

Not yet verified: the PySide6 GUI, and the PyInstaller build itself. Both need
a Windows machine.

---

## First run

1. Launch the app. It creates `Data\` and reports the location on every screen.
2. **Create Template** → open a representative PDF → drag a box around a value →
   accept the inferred anchor rule → add at least three samples → Save.
3. **Template Library** → select it → **Record baselines**. Now future edits are
   regression-tested.
4. **Process Documents** → choose a folder → Start.
5. **Results** → open the Excel file. Check the **Exceptions** sheet first; it
   lists every value the system was not confident about, with its reason.
