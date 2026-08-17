@echo off
REM ===================================================================
REM  Document Extractor - run directly from source
REM
REM  This does NOT build an EXE, and that is the entire point. PyInstaller
REM  has to copy Python's native DLLs into a bundle, and a missed one is
REM  what causes the "DLL load failed" errors. Running normally, Python
REM  finds those DLLs itself, the way it always has - so that whole class
REM  of problem simply does not arise here.
REM
REM  Anaconda Python is fine for this. It was only a problem for packaging.
REM
REM  Usage:  double-click this file, or run  run-app.bat
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo === Document Extractor ============================================
echo.

REM --- Find any usable Python -----------------------------------------
REM No conda check here, unlike build.bat. Conda works perfectly well for
REM running; it only caused trouble when PyInstaller had to bundle its DLLs.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo ERROR: No Python found on PATH.
    echo.
    echo Install Python 3.11 or 3.12 from:
    echo    https://www.python.org/downloads/windows/
    echo Tick "Add python.exe to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo Using Python %PYVER%

REM --- Environment ------------------------------------------------------
REM A separate venv from .venv-build, so the two cannot interfere with
REM each other and a broken build environment does not block running.
if not exist .venv-run (
    echo.
    echo First run - creating the environment. This takes a few minutes.
    %PY% -m venv .venv-run
    if errorlevel 1 (
        echo ERROR: could not create the environment.
        pause
        exit /b 1
    )
    set FIRSTRUN=1
)

call .venv-run\Scripts\activate.bat

if defined FIRSTRUN (
    echo Installing dependencies...
    python -m pip install --upgrade pip --quiet
    python -m pip install -r requirements-run.txt --quiet
    if errorlevel 1 (
        echo.
        echo ERROR: could not install dependencies.
        echo If you are behind a corporate proxy, set HTTPS_PROXY first.
        pause
        exit /b 1
    )
    echo Done.
)

REM --- Verify before launching -----------------------------------------
REM Catches a broken environment here, with a readable message, rather than
REM as a dialog box after the window fails to appear.
python -c "import PySide6, openpyxl, pdfplumber, pypdfium2, pydantic" 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: dependencies are missing or broken.
    echo Delete the .venv-run folder and run this file again to rebuild it.
    echo.
    python -c "import PySide6, openpyxl, pdfplumber, pypdfium2, pydantic"
    pause
    exit /b 1
)

echo Starting...
echo.
python launcher.py
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo The application exited with code %RC%.
    echo If an error was shown above, copy the full text of it.
    pause
)
endlocal
