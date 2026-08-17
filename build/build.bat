@echo off
REM ===================================================================
REM  Document Extractor - Windows build
REM
REM  Produces:  dist\DocumentExtractor\DocumentExtractor.exe
REM
REM  Requires:  Python 3.11 or 3.12 (64-bit) on PATH.
REM             Python 3.13 is not recommended yet: several wheels in the
REM             dependency set still lag a release behind.
REM
REM  Usage:     open a Command Prompt in the repository root and run
REM                 build\build.bat
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo.
echo === Document Extractor build =======================================
echo Working directory: %CD%
echo.

REM --- 1. Locate a suitable Python ------------------------------------
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3.12"
    py -3.12 --version >nul 2>nul || set "PY=py -3.11"
    !PY! --version >nul 2>nul || set "PY=py -3"
) else (
    set "PY=python"
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo Using Python %PYVER%
echo %PYVER% | findstr /b "3.1" >nul || (
    echo.
    echo ERROR: Python 3.11 or newer is required.
    echo Download it from https://www.python.org/downloads/windows/
    echo and tick "Add python.exe to PATH" during installation.
    exit /b 1
)

REM --- 2. Clean previous artefacts ------------------------------------
echo.
echo [1/6] Cleaning previous build...
if exist build\build rmdir /s /q build\build
if exist dist rmdir /s /q dist
if exist DocumentExtractor.egg-info rmdir /s /q DocumentExtractor.egg-info
for /d /r %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d"

REM --- 3. Virtual environment -----------------------------------------
echo.
echo [2/6] Preparing build environment...
if not exist .venv-build (
    %PY% -m venv .venv-build
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        exit /b 1
    )
)
call .venv-build\Scripts\activate.bat

REM Verify the ACTIVE interpreter, not just the one initially selected above.
REM A .venv-build left over from an earlier run is reused as-is by the check
REM above, so this catches that case too, regardless of how it happened.
for /f "delims=" %%b in ('python -c "import sys; print(sys.base_prefix)"') do set "BASEPREFIX=%%b"
echo %BASEPREFIX% | findstr /i "conda" >nul
if not errorlevel 1 (
    echo.
    echo ERROR: this build environment is backed by Anaconda/conda Python:
    echo    %BASEPREFIX%
    echo.
    echo Conda lays out some files differently from a standard Python
    echo install - libexpat.dll already broke one build over this - so this
    echo project needs a plain install from python.org, not conda.
    echo.
    echo Fix:
    echo   1. Delete the .venv-build folder in this project. It was created
    echo      against the conda Python and will keep reusing it otherwise.
    echo   2. Install Python from python.org if you have not already:
    echo      https://www.python.org/downloads/windows/
    echo      Tick "Add python.exe to PATH" during installation.
    echo   3. Open a NEW, plain Command Prompt - not "Anaconda Prompt". If a
    echo      regular Command Prompt still shows "(base)" at the start of
    echo      the line, conda is set to auto-activate on every terminal; run
    echo         conda config --set auto_activate_base false
    echo      then open another new Command Prompt.
    echo   4. Confirm it worked:
    echo         py -3.12 -c "import sys; print(sys.base_prefix)"
    echo      That path must NOT contain "anaconda3" or "miniconda3".
    echo   5. Run build\build.bat again.
    exit /b 1
)

python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo.
    echo ERROR: pip could not reach the package index.
    echo If you are behind a corporate proxy, set HTTPS_PROXY first, or
    echo build once on a connected machine and copy the wheels across.
    exit /b 1
)

echo.
echo [3/6] Installing dependencies (this takes a few minutes the first time)...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: dependency installation failed. See the output above.
    exit /b 1
)

REM --- 4. Smoke-test before packaging ---------------------------------
echo.
echo [4/6] Verifying the application imports and the engine runs...
python -c "import document_extractor.cli, document_extractor.main, document_extractor.gui.main_window; print('   imports OK')"
if errorlevel 1 (
    echo ERROR: the application does not import cleanly. Not packaging a broken build.
    exit /b 1
)
REM Run the entry script exactly as the frozen EXE will: as a bare top-level
REM script. This catches relative-import errors before packaging, not after.
python launcher.py info >nul
if errorlevel 1 (
    echo ERROR: launcher.py failed. The EXE would fail the same way.
    python launcher.py info
    exit /b 1
)
echo    launcher OK
if errorlevel 1 (
    echo ERROR: the application does not import cleanly. Not packaging a broken build.
    exit /b 1
)

REM --- 5. Package ------------------------------------------------------
echo.
echo [5/6] Packaging with PyInstaller...
pyinstaller build\DocumentExtractor.spec --noconfirm --clean --distpath dist --workpath build\build
if errorlevel 1 (
    echo ERROR: PyInstaller failed. See the output above.
    exit /b 1
)

if not exist dist\DocumentExtractor\DocumentExtractor.exe (
    echo ERROR: the executable was not produced.
    exit /b 1
)

REM --- 6. Smoke-test the PACKAGED executable ---------------------------
REM This is the check that matters. Importing in the virtual environment
REM proves nothing about what PyInstaller actually bundled: a missing C
REM extension such as pyexpat only shows up when the frozen EXE runs.
REM "selftest" launches the real MainWindow imports, starts an actual Qt
REM application (offscreen), renders a PDF, and writes an xlsx file - the
REM exact chain that broke last time, not just the command-line path.
echo.
echo [6/6] Smoke-testing the packaged executable...
set "SELFTEST=%TEMP%\dx_selftest"
if exist "%SELFTEST%" rmdir /s /q "%SELFTEST%"
mkdir "%SELFTEST%"
set "DOCEXTRACTOR_DATA=%SELFTEST%"
REM A windowed EXE detaches immediately, so cmd must be told to wait for it.
start /wait "" "dist\DocumentExtractor\DocumentExtractor.exe" selftest
set SELFTEST_RC=%errorlevel%
set "DOCEXTRACTOR_DATA="
rmdir /s /q "%SELFTEST%" 2>nul

if not "%SELFTEST_RC%"=="0" (
    echo.
    echo ERROR: the packaged executable failed to start ^(exit code %SELFTEST_RC%^).
    echo.
    echo This almost always means a dependency was not bundled. Check the log:
    echo    %%LOCALAPPDATA%%\DocumentExtractor\Logs\app.log
    echo or run it directly to see the error dialog:
    echo    dist\DocumentExtractor\DocumentExtractor.exe
    exit /b 1
)
echo    packaged executable OK

echo.
echo ====================================================================
echo  BUILD SUCCEEDED
echo.
echo  Executable : %CD%\dist\DocumentExtractor\DocumentExtractor.exe
echo.
echo  Next steps:
echo    1. Run it once from this machine to confirm it starts.
echo    2. Sign it. Without an EV code-signing certificate, Windows
echo       SmartScreen will warn every user on first run, and some
echo       antivirus products will quarantine it outright:
echo          signtool sign /fd SHA256 /tr http://timestamp.digicert.com ^
echo                        /td SHA256 /a dist\DocumentExtractor\DocumentExtractor.exe
echo    3. Build the installer with Inno Setup:
echo          iscc build\installer.iss
echo.
echo  Do NOT ship the loose dist folder by zip. The installer creates the
echo  writable Data folder with the correct permissions; a zip does not.
echo ====================================================================
echo.
endlocal
