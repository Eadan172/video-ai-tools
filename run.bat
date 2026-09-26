@echo off
rem ===================================================================
rem  Video Pipeline - double-click launcher
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat
rem  using the active console code page, so any non-ASCII text here would
rem  break on some machines (GBK vs UTF-8). All Chinese UI/output lives in
rem  scripts\bootstrap.ps1, which PowerShell reads reliably (UTF-8 + BOM).
rem
rem  Just double-click this file, or run with options:
rem      run.bat --format mkv             output mkv instead of mp4
rem      run.bat --only <subdir>          only process input\<subdir>
rem      run.bat --dry-run                check environment only
rem      run.bat --selftest               run the test suite
rem      run.bat --no-prompt --format mp4 unattended, no questions
rem      run.bat --no-ai                  skip AI repair, transcode only
rem ===================================================================
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ERROR] PowerShell not found.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [DONE] All finished. Outputs: output\   Logs: logs\
) else (
    echo [FAILED] exit code %RC% - see logs\ and failed\
)

rem Double-click (no arguments) keeps the window open so results stay visible.
if "%~1"=="" (
    echo.
    pause
)
exit /b %RC%