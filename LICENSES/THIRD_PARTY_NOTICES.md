# Third-party notices

Document Extractor is distributed with the components below. This file must
ship with the application; several of these licences require it.

| Component | Licence | Obligation on you |
|---|---|---|
| PySide6 / Qt 6 | LGPL-3.0 | **Action required.** See "Qt / PySide6" below. |
| pdfplumber | MIT | Include the notice. |
| pdfminer.six (via pdfplumber) | MIT | Include the notice. |
| pypdfium2 | BSD-3-Clause / Apache-2.0 | Include the notice. |
| PDFium | BSD-3-Clause | Include the notice. |
| pypdf | BSD-3-Clause | Include the notice. |
| openpyxl | MIT | Include the notice. |
| pydantic / pydantic-core | MIT | Include the notice. |
| RapidFuzz | MIT | Include the notice. |
| python-dateutil | Apache-2.0 / BSD-3-Clause | Include the notice. |
| Pillow | MIT-CMU | Include the notice. |
| Python standard library | PSF-2.0 | Include the notice. |

## Qt / PySide6 — read this before distributing

PySide6 and Qt are used here under the **LGPL-3.0**, not a commercial Qt
licence. Distributing a commercial product on those terms is entirely normal,
but it carries three conditions that are easy to miss:

1. **Dynamic linking only.** The PyInstaller configuration in `build/` ships
   Qt as separate DLLs, which satisfies this. Do not switch to a static Qt
   build without buying a commercial licence.
2. **Users must be able to substitute their own Qt.** A one-dir build satisfies
   this, because the Qt DLLs sit beside the executable and can be replaced.
   This is a further reason the spec file does not use `--onefile`.
3. **The licence text must be shipped**, and the application must tell users
   which LGPL components it uses and where to get the source.

If any of that is unacceptable for your distribution model, buy a commercial
Qt licence from The Qt Company. Do not simply omit these notices.

## Deliberate exclusion: PyMuPDF

PyMuPDF (`fitz`) is a common choice for PDF rendering and is faster than the
alternatives used here. It is licensed **AGPL-3.0**. Linking it into an
application you distribute obliges you to release this application's entire
source under the AGPL, including to anyone who merely uses it over a network.

For a commercial internal tool this is almost always unacceptable, and it is
routinely discovered only at the point of sale. `pypdfium2` (BSD-3-Clause) and
`pdfplumber` (MIT) are used instead throughout, and no code path imports
`fitz`. Keep it that way.

## Full licence texts

Place the complete text of each licence in this folder before distributing.
`pip-licenses --format=plain-vertical --with-license-file` will collect them:

    pip install pip-licenses
    pip-licenses --format=plain-vertical --with-license-file \
                 --no-license-path --output-file LICENSES\ALL_LICENSES.txt
