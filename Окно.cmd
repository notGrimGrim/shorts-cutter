@echo off
rem Window with checkboxes for every option. Uses pythonw: no console.
rem If it does not start, run "python gui.py" to see the error text.
chcp 65001 > nul
setlocal
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0gui.py"
