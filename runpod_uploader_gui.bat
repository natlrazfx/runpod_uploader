@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PY=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "APP=%SCRIPT_DIR%runpod_uploader_gui.py"

if exist "%VENV_PY%" (
  "%VENV_PY%" "%APP%"
) else (
  python "%APP%"
)

endlocal
