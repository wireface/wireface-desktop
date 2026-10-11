@echo off
rem Start Wireface (installs what it needs the first time).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto setup
if not exist "deps\shell\node_modules\electron\dist\electron.exe" goto setup
rem Speech models missing (a fresh clone, or an interrupted download): fetch them. A failure here is not fatal,
rem the face then runs with captions only and says what is missing.
".venv\Scripts\python.exe" -m wireface.models --check >nul 2>&1 || ".venv\Scripts\python.exe" -m wireface.models --install
goto run
:setup
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 || exit /b 1
:run
start "" ".venv\Scripts\pythonw.exe" -m wireface %*
