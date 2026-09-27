@echo off
rem ===================================================================
rem  Video Pipeline - local progress dashboard (read-only)
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat
rem  using the active console code page, so any non-ASCII text here would
rem  break on some machines (GBK vs UTF-8). All Chinese UI lives in the
rem  Python code, which emits UTF-8 itself.
rem
rem  Double-click to start, then open the printed URL. Options are passed
rem  through to `python main.py dashboard`, e.g.:
rem      dashboard.bat --port 9000
rem      dashboard.bat --interval 5
rem      dashboard.bat --allow-remote
rem ===================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run run.bat once to set up the environment.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py dashboard --open %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [FAILED] exit code %RC% - see the message above.
    pause
)
exit /b %RC%