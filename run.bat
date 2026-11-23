
@echo off
title Viral Clip Studio - Setup and Launch
cd /d "%~dp0"

echo.
echo ==========================================
echo       VIRAL CLIP STUDIO - STARTUP
echo ==========================================
echo.

REM Remove broken virtual environment if pip is missing
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo Repairing broken virtual environment...
        rmdir /s /q ".venv"
    )
)

REM Create virtual environment if needed
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        echo Make sure Python 3 is installed and added to PATH.
        pause
        exit /b 1
    )
)

REM Activate environment
call ".venv\Scripts\activate.bat"

REM Restore pip
echo.
echo Checking pip...
python -m ensurepip --upgrade
if errorlevel 1 (
    echo ERROR: Could not restore pip.
    pause
    exit /b 1
)

REM Upgrade installation tools
echo.
echo Updating pip and setup tools...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo ERROR: Could not update pip.
    pause
    exit /b 1
)

REM Install dependencies
echo.
echo Installing project requirements...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

REM Launch application
echo.
echo Starting Viral Clip Studio...
python app.py

echo.
echo Application stopped.
pause
