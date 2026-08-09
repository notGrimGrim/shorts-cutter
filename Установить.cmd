@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

echo === shorts-cutter: setup ===
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH.
    echo Install Python 3.10+ from https://python.org
    echo ^(tick "Add python.exe to PATH" during install^), then run this again.
    pause
    exit /b 1
)

if not exist "%~dp0.venv" (
    echo Creating virtual environment...
    python -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo Failed to create .venv - see the error above.
        pause
        exit /b 1
    )
)

echo Installing dependencies (yt-dlp, faster-whisper, ctranslate2)...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"%~dp0.venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo Dependency install failed - see the error above.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    where winget >nul 2>nul
    if errorlevel 1 (
        echo.
        echo ffmpeg not found, and winget is unavailable on this machine.
        echo Install it manually from https://ffmpeg.org and either add it
        echo to PATH or pass --ffmpeg PATH when running cut.py.
    ) else (
        echo Installing ffmpeg via winget...
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    )
) else (
    echo ffmpeg already on PATH - skipping.
)

echo.
echo === Done ===
echo Next: double-click the other two .cmd shortcuts in this folder
echo (console version, or window version - either one works).
echo.
echo Optional, for smarter local selection without internet:
echo   1. install Ollama - https://ollama.com
echo   2. run: ollama pull qwen2.5:7b-instruct-q4_K_M
echo Optional, for the cloud model instead: get a free key at
echo console.groq.com and paste it into the app window (or into
echo work\groq-key.txt). Without either, the app still works, it
echo just picks moments by a formula instead of ranking them with AI.
pause
