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

if not exist "a2a-hub.exe" (
  echo.
  echo   [ERROR] a2a-hub.exe was not found next to this script.
  echo           Keep start-hub.bat in the same folder as a2a-hub.exe.
  echo.
  pause
  exit /b 1
)

echo.
echo   a2a-hub
echo   ------------------------------------------------------------------
echo   console : http://127.0.0.1:%PORT%/console
echo   stop    : press Ctrl-C, or just close this window
echo   ------------------------------------------------------------------
echo.

REM Open the browser a few seconds from now, from a minimized helper window.
REM The hub needs a moment to bind the port; opening the browser too early shows
REM a connection error and the user has to refresh manually.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%PORT%/console"

REM Run the hub in THIS window, so its log stays visible and Ctrl-C stops it.
a2a-hub.exe serve --host 127.0.0.1 --port %PORT%

echo.
echo   hub stopped.
pause
