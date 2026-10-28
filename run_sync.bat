@echo off
REM ============================================================
REM  run_sync.bat - run the Ollama web-vs-local sync tool
REM  Creates a timestamped log under logs\ for every run.
REM  Usage: run_sync.bat [--web-only] [--no-close] [--pull-only]
REM ============================================================
setlocal EnableExtensions

cd /d "%~dp0"

REM Find a usable Python 3 interpreter.
set "PY="
for %%C in (python py python3) do (
    if not defined PY (
        where %%C >nul 2>&1 && %%C --version >nul 2>&1 && set "PY=%%C"
    )
)
if not defined PY (
    echo.
    echo  Python was not found. Install Python 3 and ensure it is on PATH.
    echo.
    goto :fail
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Python 3.8 or newer is required.
    echo.
    goto :fail
)

REM Both dependencies are required for web scraping.
%PY% -c "import requests, bs4" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Missing Python dependencies.
    echo  Run:  %PY% -m pip install -r requirements.txt
    echo.
    goto :fail
)

REM --web-only does not touch local Ollama, so the CLI is not needed then.
set "WEBONLY="
for %%A in (%*) do if /i "%%~A"=="--web-only" set "WEBONLY=1"
if not defined WEBONLY (
    where ollama >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  The `ollama` command was not found on PATH.
        echo  Install Ollama from https://ollama.com/download and re-open this window,
        echo  or run with --web-only to skip local inventory.
        echo.
        goto :fail
    )
)

if not exist logs mkdir logs

REM Run the tool; pass through any args (--web-only, --no-close, --pull-only).
%PY% ollama_sync.py %*
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ------------------------------------------------------------
echo  Run finished with exit code %EXITCODE%.
if not "%EXITCODE%"=="0" echo  The run did not complete successfully; check the messages above.
echo  Full report was also written to logs\ (newest at bottom):
echo ------------------------------------------------------------
for /f "delims=" %%F in ('dir /b /o-d /a-d "logs\ollama_sync_*.log" 2^>nul') do (
    echo   logs\%%F
)

echo.
if not defined CI (
    echo  Press any key to close this window...
    pause >nul
)

REM Expand EXITCODE before endlocal discards it.
endlocal & exit /b %EXITCODE%

:fail
if not defined CI (
    echo  Press any key to close this window...
    pause >nul
)
endlocal & exit /b 1