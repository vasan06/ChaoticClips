@echo off
setlocal
cd /d "%~dp0"
title ChaoticClips
if not exist "workspace\setup-temp" mkdir "workspace\setup-temp"
set "TEMP=%CD%\workspace\setup-temp"
set "TMP=%TEMP%"
set "TMPDIR=%TEMP%"
set "PIP_CACHE_DIR=%CD%\workspace\pip-cache"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "VENV_PY=%CD%\.venv\Scripts\python.exe"

if /i "%~1"=="--setup" goto setup
if not exist "%VENV_PY%" (
    echo Run run.bat --setup to create the environment first.
    exit /b 1
)
goto check

:setup
if not exist "%VENV_PY%" (
    where py >nul 2>&1
    if errorlevel 1 (
        python -B -m venv .venv
    ) else (
        py -3 -B -m venv .venv
    )
    if errorlevel 1 exit /b 1
)
"%VENV_PY%" -B -c "import sys; sys.exit(sys.version_info < (3, 12))"
if errorlevel 1 (
    echo Python 3.12 or newer is required.
    exit /b 1
)
"%VENV_PY%" -B -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

:check
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo A full FFmpeg build with libass must be on PATH.
    exit /b 1
)
where ffprobe >nul 2>&1
if errorlevel 1 (
    echo FFprobe must be on PATH.
    exit /b 1
)
"%VENV_PY%" -B -c "import sys, flask, faster_whisper, tkinter, yt_dlp; sys.exit(sys.version_info < (3, 12))"
if errorlevel 1 (
    echo Check Python 3.12+ and Tk support, then run run.bat --setup.
    exit /b 1
)
"%VENV_PY%" -B app.py
exit /b %ERRORLEVEL%
