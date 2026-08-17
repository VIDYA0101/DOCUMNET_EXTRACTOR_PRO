"""Frozen-application entry point.

PyInstaller runs its entry script as a top-level module called ``__main__``
with ``__package__`` unset. Any relative import in that script therefore fails
with "attempted relative import with no known parent package", even though the
same import is perfectly valid inside the package. So this file uses an
absolute import and does nothing else.

``multiprocessing.freeze_support()`` must be the first statement executed. A
frozen executable re-runs this script in every worker process it spawns;
without freeze_support the worker relaunches the full GUI, which spawns more
workers, which relaunch more GUIs.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from document_extractor.main import main

    sys.exit(main())
