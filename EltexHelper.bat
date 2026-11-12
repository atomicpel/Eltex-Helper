@echo off
rem Eltex Helper: start the graphical interface from the source folder.
rem Needs Python 3.8+ with dependencies: pip install .
rem On Windows 7 use the portable package EltexHelper-<version>-win7-*.zip from Releases instead.
cd /d "%~dp0"
python -m eltex_assistant gui
if errorlevel 1 (
  echo.
  echo Failed to start. See the message above and the file eltex-helper-error.log in %TEMP%
  pause
)
