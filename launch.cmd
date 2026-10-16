@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
    python usage_widget.py
) else (
    start "" pythonw.exe "%~dp0usage_widget.py"
)
