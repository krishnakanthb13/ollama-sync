@echo off
REM ============================================================
REM  test_models.bat - test every installed Ollama model with "hi"
REM  Writes a timestamped log under logs\ for every run.
REM  Usage: test_models.bat [--only <model>] [--limit N]
REM         [--parallel N] [--no-close]
REM ============================================================
setlocal

cd /d "%~dp0"

set "PY=python"
%PY% --version >nul 2>&1 || set "PY=py"
%PY% --version >nul 2>&1 || set "PY=python3"
%PY% --version >nul 2>&1 || (
    echo.
    echo  Python was not found. Install Python 3 and ensure it is on PATH.
    echo  The scripts also need: pip install requests beautifulsoup4
    echo.
    pause >nul
    exit /b 1
)

REM The tool checks for the ollama CLI itself, but fail fast with a clear message.
where ollama >nul 2>&1 || (
    echo.
    echo  The `ollama` command was not found on PATH.
    echo  Install Ollama from https://ollama.com/download and re-open this window.
    echo.
    pause >nul
    exit /b 1
)

if not exist logs mkdir logs

%PY% test_models.py %*
set "EXITCODE=%ERRORLEVEL%"

echo.
echo ------------------------------------------------------------
echo  Test run finished with exit code %EXITCODE%.
echo  Full report was also written to logs\ (newest at bottom):
echo ------------------------------------------------------------
for /f "delims=" %%F in ('dir /b /o-d /a-d "logs\model_test_*.log" 2^>nul') do (
    echo   logs\%%F
)

echo.
echo  Press any key to close this window...
pause >nul

endlocal
exit /b %EXITCODE%
