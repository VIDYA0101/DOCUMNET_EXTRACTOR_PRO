"""Support for `python -m document_extractor` when running from source.

The frozen build does not use this file; it starts at launcher.py in the
repository root. Both are thin wrappers around document_extractor.main so the
two paths cannot drift apart.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from document_extractor.main import main

    sys.exit(main())
