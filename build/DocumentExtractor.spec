# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification.

One-dir, not one-file. A one-file build extracts the whole bundle to a temp
directory on every launch, which adds seconds to startup, breaks on machines
where %TEMP% is locked down by policy, and is the single most reliable way to
get flagged by antivirus.

Run from the repository root:
    pyinstaller build\\DocumentExtractor.spec --noconfirm --clean
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
APP_NAME = "DocumentExtractor"


def _find_stdlib_dll(*filenames: str) -> Path | None:
    """Locate a DLL that a compiled extension module depends on.

    pyexpat.pyd and _elementtree.pyd both dynamically link against a shared
    libexpat DLL rather than each containing their own copy. Where that DLL
    lives depends on how Python itself was built and packaged, and that
    varies more than it should:

      - python.org's official installer puts it directly in DLLs\\.
      - conda / Anaconda builds commonly place shared native libraries
        under Library\\bin instead, following conda's own packaging
        convention rather than CPython's.

    Guessing one fixed location and failing if it's wrong already went
    through one bad guess. Instead of guessing again, this checks the
    locations known to be common, and if neither has it, searches the whole
    interpreter tree once. A few extra seconds at build time is a better
    trade than another round trip.
    """
    base = Path(sys.base_prefix)
    quick_dirs = [base / "DLLs", base / "Library" / "bin", base]
    for directory in quick_dirs:
        for name in filenames:
            candidate = directory / name
            if candidate.exists():
                return candidate

    # Fallback: walk the whole interpreter install. Slower, but it does not
    # depend on knowing conda's exact packaging convention in advance.
    for name in filenames:
        for candidate in base.rglob(name):
            return candidate
    return None


_extra_binaries = []
if sys.platform == "win32":
    # Bundle it explicitly if it exists - this is what the Anaconda build
    # needed, since PyInstaller's own automatic dependency walk missed it
    # there. But NOT finding it is not automatically an error: some Python
    # builds (this project has now seen one, on GitHub's own hosted runners)
    # compile expat directly into pyexpat.pyd and _elementtree.pyd rather
    # than linking a separate DLL, in which case there is nothing to find
    # and nothing wrong. Guessing "not found" always means "broken" is what
    # caused this exact build to fail once already, on a machine that did
    # not actually have the problem this check exists to catch.
    #
    # Whether it is truly needed and truly missing is settled by build.bat's
    # own step 6, which runs the packaged EXE's `selftest` command - that
    # actually imports openpyxl in the real, frozen build and fails loudly,
    # with a full traceback, if this was ever really a problem. That is a
    # more reliable judge than a guess made here at spec-parse time.
    libexpat = _find_stdlib_dll("libexpat.dll")
    if libexpat is not None:
        _extra_binaries.append((str(libexpat), "."))
    else:
        print(
            "NOTE: libexpat.dll not found under "
            f"{Path(sys.base_prefix)} - proceeding without bundling it "
            "explicitly. This Python build may compile expat directly into "
            "pyexpat.pyd instead of linking it separately, which needs "
            "nothing extra. If that assumption is wrong, step [6/6] "
            "(selftest) will catch it with a full traceback."
        )

# Qt ships far more than this application uses. Excluding these cuts the
# distribution roughly in half and removes a large amount of attack surface,
# most notably the embedded Chromium in QtWebEngine.
EXCLUDED_QT = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtQmlModels",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtRemoteObjects",
    "PySide6.QtOpcUa", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtHelp", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
]

EXCLUDED_OTHER = [
    "tkinter", "test", "unittest", "pydoc", "doctest", "lib2to3",
    "matplotlib", "numpy.distutils", "setuptools", "pip", "wheel",
    "IPython", "jupyter", "notebook", "scipy", "pandas",
    "PyQt5", "PyQt6", "PySide2",
]

hidden = [
    "pdfplumber", "pypdfium2", "pypdf", "openpyxl", "openpyxl.cell._writer",

    # OCR support. document_extractor.domain.pdf.ocr and pytesseract are both
    # imported lazily, inside function bodies - deliberately, so a normal run
    # that never touches OCR never pays for it. That same laziness is
    # invisible to PyInstaller's static import walk, which only follows
    # module-level imports starting from launcher.py - the identical blind
    # spot that missed pyexpat earlier in this project, in a new place.
    "document_extractor.domain.pdf.ocr", "pytesseract",
    "et_xmlfile", "rapidfuzz", "pydantic", "pydantic_core", "dateutil",

    # XML. openpyxl parses its built-in style table with ElementTree at import
    # time, and ElementTree reaches pyexpat through a lazy import that static
    # analysis does not always follow. Without these the frozen build fails on
    # first launch with "No module named expat", long after the build reported
    # success.
    "pyexpat", "_elementtree",
    "xml", "xml.parsers", "xml.parsers.expat",
    "xml.etree", "xml.etree.ElementTree", "xml.etree.cElementTree",
    "xml.dom", "xml.dom.minidom", "xml.sax", "xml.sax.expatreader",

    # Compression and encoding backends reached indirectly by openpyxl (xlsx is
    # a zip archive) and by pdfplumber.
    "zlib", "zipfile", "encodings.idna", "encodings.utf_8", "encodings.cp1252",
]
hidden += collect_submodules("pydantic")
hidden += collect_submodules("openpyxl")
hidden += collect_submodules("pdfplumber")
hidden += collect_submodules("pdfminer")

a = Analysis(
    # launcher.py, NOT document_extractor/__main__.py. PyInstaller runs the
    # entry script with no parent package, so it must use absolute imports.
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    # pypdfium2_raw loads its PDF engine (pdfium.dll) via ctypes.CDLL at
    # runtime, resolved by file path next to its own __init__.py. That is
    # invisible to PyInstaller's static import scanner - the same blind spot
    # that missed pyexpat - so it must be collected explicitly or the frozen
    # build fails the first time it tries to open a PDF.
    binaries=collect_dynamic_libs("pypdfium2_raw") + _extra_binaries,
    datas=[
        # Bundle nothing writable. Everything the user edits lives in DATA_ROOT.
        (str(ROOT / "LICENSES"), "LICENSES"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDED_QT + EXCLUDED_OTHER,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX compression is a strong antivirus heuristic trigger
    console=False,      # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "build" / "app.ico") if (ROOT / "build" / "app.ico").exists() else None,
    version=str(ROOT / "build" / "version_info.txt") if (ROOT / "build" / "version_info.txt").exists() else None,
    manifest=str(ROOT / "build" / "app.manifest") if (ROOT / "build" / "app.manifest").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
