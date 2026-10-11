@echo off
rem Download the model lists again (Settings > Agent > Model and Settings > Voice > Model): the models.dev catalog for
rem the agents, and the realtime voice services' own lists for this face's API keys. A running face shows the new
rem lists the next time the panel is opened. Another face: RefreshModels.cmd --face NAME
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Wireface.cmd first: it installs what this needs.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m wireface.catalog --refresh %*
set code=%errorlevel%
rem double-clicked: leave the window open long enough to read it
echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~0" >nul && pause
exit /b %code%
