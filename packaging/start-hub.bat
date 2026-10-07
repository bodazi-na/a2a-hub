@echo off
REM ============================================================================
REM  a2a-hub launcher
REM
REM  Starts the hub and opens the console in your default browser.
REM  Double-click this file, or run:  start-hub.bat [port]
REM
REM  ---------------------------------------------------------------------------
REM  Why this file is ASCII-only (no Chinese text, no accents):
REM
REM  cmd.exe parses .bat files using the *system code page* -- GBK/cp936 on a
REM  Chinese Windows, cp1252 on a Western one. A UTF-8 .bat containing non-ASCII
REM  text therefore comes out as mojibake, and worse, a mangled multi-byte
REM  sequence can break the parser and make the script fail in confusing ways.
REM
REM  `chcp 65001` at the top does NOT reliably fix this: cmd has already started
REM  reading the file by then. So the robust answer is to keep it ASCII-only.
REM  (The rest of this project is UTF-8; this one file is a deliberate exception.)
REM  ---------------------------------------------------------------------------
REM ============================================================================

setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=9200"

REM ---------------------------------------------------------------------------
REM  Locate a2a-hub.exe.
REM
REM  This same script ships in two places: next to the exe (what users get from
REM  the build) and in packaging/ (the source template). Only the first has an
REM  exe beside it -- so double-clicking the one in packaging/ used to fail with
REM  "not found", which is a confusing dead end.
REM
REM  So: look next to the script first, then in the two obvious build-output
REM  locations relative to it. That makes the launcher work from a source
REM  checkout too.
REM ---------------------------------------------------------------------------
set "HUB_EXE="
if exist "a2a-hub.exe"                    set "HUB_EXE=%CD%\a2a-hub.exe"
if not defined HUB_EXE if exist "dist\a2a-hub\a2a-hub.exe"        set "HUB_EXE=%CD%\dist\a2a-hub\a2a-hub.exe"
if not defined HUB_EXE if exist "dist\a2a-hub.exe"                set "HUB_EXE=%CD%\dist\a2a-hub.exe"
if not defined HUB_EXE if exist "%~dp0..\dist\a2a-hub\a2a-hub.exe" set "HUB_EXE=%~dp0..\dist\a2a-hub\a2a-hub.exe"
if not defined HUB_EXE if exist "%~dp0..\dist\a2a-hub.exe"         set "HUB_EXE=%~dp0..\dist\a2a-hub.exe"

if not defined HUB_EXE (
  echo.
  echo   [ERROR] a2a-hub.exe was not found.
  echo.
  echo   Looked in:
  echo     %CD%\a2a-hub.exe
  echo     %CD%\dist\a2a-hub\a2a-hub.exe
  echo     %~dp0..\dist\a2a-hub\a2a-hub.exe
  echo.
  echo   Fix it by either:
  echo     1^) building it:   python tools\build_exe.py
  echo     2^) or putting start-hub.bat in the same folder as a2a-hub.exe
  echo.
  pause
  exit /b 1
)

REM Run from the exe's own folder: that is where the hub keeps data\ and
REM workspace\, and it is what makes "copy the folder anywhere" work.
for %%I in ("%HUB_EXE%") do cd /d "%%~dpI"

echo.
echo   a2a-hub
echo   ------------------------------------------------------------------
echo   exe     : %HUB_EXE%
echo   console : http://127.0.0.1:%PORT%/console
echo   stop    : press Ctrl-C, or just close this window
echo   ------------------------------------------------------------------
echo.

REM Open the browser a few seconds from now, from a minimized helper window.
REM The hub needs a moment to bind the port; opening the browser too early shows
REM a connection error and the user has to refresh manually.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%PORT%/console"

REM Run the hub in THIS window, so its log stays visible and Ctrl-C stops it.
"%HUB_EXE%" serve --host 127.0.0.1 --port %PORT%

echo.
echo   hub stopped.
pause
