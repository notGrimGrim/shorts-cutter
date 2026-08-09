@echo off
chcp 65001 > nul
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
