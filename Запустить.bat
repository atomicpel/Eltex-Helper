@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m eltex_assistant gui
if errorlevel 1 (
  echo.
  echo Не удалось запустить. Установите Python 3.8+ с python.org и выполните: pip install .
  pause
)
