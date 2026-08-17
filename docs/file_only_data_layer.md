# Document Extractor — File-Only Data Layer

**Supersedes §1.1 of the architecture review.** No SQL, no database engine, no server. Every byte of state is a plain file on the local hard disk. Everything else in the original review stands unchanged.

The design that makes this work is **append-only logs + an in-memory index + a rebuildable snapshot**. That's it. Roughly 300 lines of infrastructure code, no dependencies beyond the standard library.

---

## 1. Installation layout (local hard disk)

Do **not** install to `C:\Program Files`. Standard users cannot write there, and your Templates/Output/Logs need to be writable.

```
C:\DocumentExtractor\
├── App\                          # installer writes this, read-only at runtime
│   ├── DocumentExtractor.exe
│   ├── _internal\                # PyInstaller one-dir payload
│   └── vendor\tesseract\         # (v1.1)
└── Data\                         # everything writable lives here
    ├── Templates\
    ├── Input\
    ├── Work\
    ├── Output\
    ├── Review\
    ├── Archive\
    ├── Logs\
    ├── index\
    ├── cache\
    └── settings.json
```

**Installer requirement (Inno Setup):** a folder created under `C:\` by an elevated installer inherits permissions from the drive root, which gives `Users` read-and-execute only — *not* write. You must explicitly grant Modify on the Data folder:

```ini
[Dirs]
Name: "{app}\Data"; Permissions: users-modify
```

Miss this and the app installs cleanly, launches cleanly, and fails the first time a non-admin user saves a template. Verify it as an acceptance test with a standard (non-admin) account.

At startup, probe writability by creating and deleting a temp file in `Data\`. If it fails, fall back to `%LOCALAPPDATA%\DocumentExtractor` and tell the user where the data went.

---

## 2. The three file patterns

Every piece of state uses exactly one of these. No exceptions, no ad-hoc file handling anywhere else in the codebase.

### Pattern A — Atomic document (for things that get rewritten)

Used for: `template.json`, `settings.json`, `manifest.json`, per-document result files.

```
write to  <target>.tmp     (same directory — must be the same volume)
flush + os.fsync()
os.replace(<target>.tmp, <target>)     # atomic on NTFS
```

**Never open a live file in `"w"` mode.** A crash mid-write leaves a truncated JSON file and the user loses a template they spent 20 minutes building. `os.replace` is atomic — the file is either the old version or the new version, never half of either.

On startup, sweep for orphaned `*.tmp` files and delete them.

### Pattern B — Append-only log (for facts that accumulate)

Used for: run journals, seen-hashes, invoice keys, template stats, audit trail.

```
open(path, "a", encoding="utf-8", newline="\n")
write one complete JSON object + "\n"
flush every write; fsync every N records or at batch boundaries
```

Rules:
- **Single writer per file, always.** Never let six worker processes append to the same log — interleaved partial writes will corrupt lines. Workers return small status dicts through the process pool; only the supervisor process appends.
- **Recovery on read:** if the final line doesn't end in `\n` or fails to parse, discard it. That's a crash mid-append, and the record is by definition incomplete. Log a warning, continue.
- **Ordering matters:** write the per-document result file (Pattern A) *first*, then append the journal line. A journal entry then always implies the result exists. Never the reverse.

### Pattern C — Snapshot cache (for fast startup)

Used for: the in-memory index.

```
Data\cache\index_snapshot.json     # the built index structures
Data\cache\index_offsets.json      # byte offset consumed per source log
```

On startup: load the snapshot, then `seek()` to the recorded offset in each log and replay only the tail added since. Rewrite the snapshot on clean shutdown, or every 5,000 appended records.

Both cache files are **fully derivable** from the logs. Delete them and the app rebuilds on next launch. Include a "Rebuild index" button in Settings and rebuild automatically if the snapshot's `schema_version` doesn't match the app's.

This snapshot-plus-tail-replay trick is what keeps startup at ~200 ms whether you have 500 documents of history or 200,000.

---

## 3. Concrete file inventory

### Templates — loaded fully into memory at startup

```
Data\Templates\<vendor_slug>\<doctype_slug>\template.json
                                           \versions\v1.json, v2.json
                                           \samples\*.pdf, *.expected.json
```

100 templates × ~20 KB = 2 MB of JSON. Parsing all of them takes ~150 ms. No cache required — just load them. The `TemplateRepository` holds them in a dict keyed by `template_id` for the whole session, with a file-watcher or a manual Refresh button to pick up external edits.

### Matching index — built in memory from the templates

Built at startup by iterating the loaded templates. No file at all:

```python
identifier_index: dict[str, str]        # "29AABCA1234A1Z5" -> template_id
token_index:      dict[str, set[str]]   # "ACME INDUSTRIES" -> {template_ids}
forbidden_index:  dict[str, set[str]]
```

Candidate generation for a document becomes a handful of dict lookups — O(1), not a scan of 100 template folders. Build time for 100 templates: under 50 ms.

### Run state

```
Data\Work\runs\<run_id>\manifest.json           # Pattern A — config, thresholds, counts
Data\Work\runs\<run_id>\journal.jsonl           # Pattern B — one line per completed file
Data\Work\runs\<run_id>\results\<xx>\<doc_id>.json   # Pattern A, per worker
```

**Shard the results directory** by the first two hex characters of `doc_id` (256 subdirectories). NTFS handles 10,000 files in one directory, but enumerating it is slow and Explorer becomes unusable — which matters, because your users *will* open these folders.

Journal line shape:

```json
{"doc_id":"a3f9...","src":"invoices\\ACME_0871.pdf","sha256":"9c1d...",
 "template_id":"acme-industries__invoice","tpl_version":3,"status":"OK",
 "reasons":[],"fields_ok":11,"fields_flagged":0,"lines":7,
 "ms":412,"ts":"2026-08-12T09:14:22Z"}
```

Keep it under ~400 bytes. This line is what the Results grid reads — the full result JSON is only opened when the user clicks a row.

### Cross-run history

```
Data\index\seen_hashes.jsonl      # {h16, doc_id, run_id, ts}
Data\index\invoice_keys.jsonl     # {vendor, invnum, doc_id, run_id, ts}
Data\index\template_stats.jsonl   # one aggregate line per template per run
Data\index\audit.jsonl            # review corrections: who, when, old, new
```

`seen_hashes` stores the first 16 hex characters of the SHA-256, not all 64 — collision probability at 1 million documents is negligible, and it cuts memory by 4×. Loaded into a Python `set`. 100,000 entries ≈ 8 MB resident.

`template_stats` gets **one line per template per run**, not per document — aggregate before writing. 100 templates × 1,000 runs = 100,000 lines is the lifetime ceiling. That's what feeds drift detection.

---

## 4. How each hard requirement is met without SQL

| Requirement | File-only mechanism | Cost |
|---|---|---|
| **Resume after crash** | Read `journal.jsonl`, build a set of completed `sha256` values, skip those files. Journal is append-only so it survives a hard kill. | ~50 ms for 1,000 lines |
| **Duplicate detection** | `seen_hashes` set + `invoice_keys` dict, both in memory from startup snapshot. `key in set` is O(1). | ~8 MB per 100k docs |
| **Results grid, 50k rows** | Load journal lines into a list of lightweight tuples, back a `QAbstractTableModel` with it. Sort and filter in memory. | 50k rows sorts in <80 ms, ~15 MB |
| **Template matching at 100+ templates** | In-memory inverted index built from templates at startup. Dict lookups. | <50 ms build |
| **Drift detection** | Aggregate `template_stats.jsonl` in memory; compare current run against a rolling baseline. | Trivial |
| **Audit trail** | `audit.jsonl`, append-only, never rewritten. Append-only is *better* than a table here — it's tamper-evident by construction. | — |
| **Atomicity** | `os.replace` for documents, single-writer append for logs, partial-line discard on read. | — |
| **Retention / purge** | Compaction: read log, filter, write to `.tmp`, `os.replace`, delete snapshot to force rebuild. | Seconds |

Nothing here needs a query planner. Your access patterns are: *load everything once, then look things up by key*. That's a dict, and a dict in RAM is faster than SQLite would have been.

---

## 5. Where this design genuinely runs out — and what to do

Be honest about the ceiling so you're not surprised by it in year three.

| Scale | Behaviour |
|---|---|
| Up to ~50,000 documents of history | Excellent. Startup ~200 ms, everything in memory, no perceptible cost. |
| 50,000 – 200,000 | Fine. Snapshot ~40 MB, resident memory ~60 MB, startup ~400 ms. |
| Above ~200,000 | Startup memory becomes noticeable. **Mitigation:** shard the index logs by year — `index\2026\seen_hashes.jsonl` — and load only the current and previous year eagerly, older years on demand. Extends the design comfortably to millions. |

**Two things this design cannot do**, by construction:

1. **Concurrent multi-user access to one Data folder.** Single writer only. Enforce it with a lock file: create `Data\.lock` exclusively at startup, write PID + heartbeat timestamp, delete on exit. Second instance sees a live lock and opens read-only with a clear message. If you later need genuine multi-user, that's an architectural change, not a tweak.

2. **Ad-hoc queries.** "All ACME invoices over ₹50,000 in March" means a full in-memory scan (fast enough at 100k rows) or an Excel export. There's no query language. In practice this is fine — users do that analysis in Excel anyway, which is why the workbook design in §15 matters.

---

## 6. Implementation module (`infra/store.py`)

One module owns all of this. Nothing else in the codebase touches the filesystem for state.

```
class AtomicJson:
    read(path) -> dict | None
    write(path, obj)                      # tmp + fsync + os.replace
    sweep_orphans(root)

class AppendLog:
    __init__(path)                        # single writer, asserts exclusivity
    append(record: dict)
    read_all(skip_bad=True) -> Iterator[dict]
    read_from(offset) -> Iterator[dict], new_offset
    compact(predicate)                    # filter + atomic replace
    size_bytes()

class IndexSnapshot:
    load() -> (structures, offsets) | None
    save(structures, offsets)
    invalidate()

class MemoryIndex:                        # the thing the app actually queries
    seen_hashes: set[str]
    invoice_keys: dict[tuple[str,str], str]
    template_stats: dict[str, RollingStats]
    load()                                # snapshot + tail replay
    record_document(journal_line)
    has_seen(sha) -> bool
    flush()                               # re-save snapshot

class InstanceLock:
    acquire() -> bool
    heartbeat()
    release()
```

**Test requirements for this module — write these before the rest of the app:**

- Kill the process mid-`AtomicJson.write` (patch `os.replace` to raise); the original file must be intact and readable.
- Truncate the last line of a `journal.jsonl` mid-record; `read_all` must return N−1 valid records and log one warning.
- Delete `cache\index_snapshot.json`; the app must rebuild identical structures from the logs.
- Two instances started simultaneously: exactly one acquires the lock.
- A 100,000-line log must load in under 1 second cold, under 250 ms with a snapshot.

Get this module right and correct, and the "no database" constraint costs you nothing that matters.

---

## 7. What doesn't change

Everything else from the architecture review stands: anchor-relative extraction over drawn rectangles, pypdfium2 + pdfplumber instead of AGPL PyMuPDF, arithmetic cross-checks, the seven-status taxonomy, process-pool batching with `freeze_support()` and per-file timeouts, the Documents/LineItems/Exceptions workbook shape, vendor-slug path sanitisation, one-dir PyInstaller with code signing, and MVP without automatic matching or OCR.

The data layer was the one place SQLite would have earned its keep. The design above buys back the atomicity, the resume, and the lookup speed for about 300 lines of standard-library code. That is a reasonable trade, and it keeps the whole system as a folder you can copy to a USB stick and read in Notepad.
