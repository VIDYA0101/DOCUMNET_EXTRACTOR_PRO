"""Application startup logic.

This lives inside the package, not in the entry script. PyInstaller executes
its entry script as a top-level module with no parent package, so relative
imports there fail with "attempted relative import with no known parent
package". Keeping the logic here and starting it from launcher.py keeps the
relative imports valid in both the frozen build and `python -m`.
"""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path


class _NullWriter:
    """Stand-in for a missing stream.

    A frozen windowed build has no console, so sys.stdout and sys.stderr are
    None. Any print() then raises AttributeError on NoneType, which kills the
    CLI path inside the packaged executable for a reason that has nothing to do
    with the task being run.
    """

    encoding = "utf-8"

    def write(self, text):
        return len(text) if text else 0

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no file descriptor")


def _ensure_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()


def _excepthook(exc_type, exc_value, exc_tb):
    """Log unhandled exceptions and tell the user where the log is.

    Without this, a frozen --noconsole build dies silently and the user reports
    only that "it closed".
    """
    logging.getLogger("unhandled").critical(
        "unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None, "Unexpected error",
                "Document Extractor hit an unexpected error and may not be in "
                "a usable state.\n\nThe details have been written to the log "
                "file. Please send it to support.\n\n" + text[-1200:])
    except Exception:
        pass


def _fix_windows_dll_search() -> None:
    """Make sure PySide6's own Qt DLLs are found first, not some other Qt.

    A virtual environment isolates which Python *packages* get imported. It
    has no say over the operating system's DLL search order, which is a
    process-level, PATH-driven mechanism venv cannot touch. On a machine
    with any other Qt install reachable via PATH - Anaconda's own Qt, bundled
    for its Spyder editor, is exactly such a case - Windows can resolve
    QtCore.pyd's dependency on Qt6Core.dll to that other copy instead of the
    one PySide6 actually ships. That fails not as "file missing" but as "the
    specified procedure could not be found": a file loaded, just not the one
    with the function this code expects.

    os.add_dll_directory() (Windows-only, Python 3.8+) tells the loader to
    check a specific directory first, regardless of PATH. It must run before
    PySide6 is imported at all - which is why this is a free function called
    at the top of run_gui(), one line before that import, rather than logic
    living inside PySide6 itself or inside gui/.
    """
    if sys.platform != "win32":
        return
    import importlib.util
    import os

    spec = importlib.util.find_spec("PySide6")
    if spec and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            try:
                os.add_dll_directory(location)
            except (FileNotFoundError, OSError):
                pass


def run_gui(argv: list[str]) -> int:
    _fix_windows_dll_search()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .domain.template.repository import TemplateRepository
    from .gui.main_window import MainWindow
    from .infra import logging_setup
    from .infra.paths import DataRoot, resolve_data_root
    from .infra.settings import Settings
    from .infra.store import InstanceLock, MemoryIndex

    # Qt reads these before QApplication is constructed.
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(argv)
    app.setApplicationName("Document Extractor")
    app.setOrganizationName("Document Extractor")

    try:
        root = DataRoot(resolve_data_root()).ensure()
    except RuntimeError as exc:
        QMessageBox.critical(
            None, "Cannot start",
            f"{exc}\n\nDocument Extractor needs a folder it can write to. "
            "If it was installed under Program Files, reinstall it to "
            "C:\\DocumentExtractor instead.")
        return 2

    settings = Settings.load(root.settings_file)
    logging_setup.setup(root.logs, settings.log_level)
    sys.excepthook = _excepthook
    log = logging.getLogger(__name__)
    log.info("starting; data root = %s", root.root)

    swept = root.sweep_tmp()
    if swept:
        log.info("removed %d orphaned temp files from an interrupted write", swept)
    logging_setup.purge_old(root.logs, settings.log_retention_days)

    # One writer only. Two instances sharing a data root corrupt the journals.
    lock = InstanceLock(root.lock_file)
    if not lock.acquire():
        answer = QMessageBox.question(
            None, "Already running",
            "Another copy of Document Extractor appears to be using this data "
            "folder.\n\nRunning two copies at once can corrupt the results "
            "index.\n\nStart anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return 3
        lock.held = True

    repo = TemplateRepository(root.templates)
    repo.load_all()
    index = MemoryIndex(root.index, root.cache)
    try:
        index.load()
    except Exception:
        log.exception("index load failed; rebuilding from logs")
        index.rebuild()

    window = MainWindow(root, settings, repo, index)
    window.show()
    try:
        return app.exec()
    finally:
        lock.release()
        log.info("shutdown complete")


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    argv = list(sys.argv if argv is None else argv)

    # Anything past the executable name means the headless CLI was intended.
    if len(argv) > 1 and not argv[1].startswith("-"):
        from .cli import main as cli_main
        return cli_main(argv[1:])
    return run_gui(argv)
