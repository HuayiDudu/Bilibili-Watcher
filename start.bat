@echo off
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto use_py_launcher

where python >nul 2>nul
if not errorlevel 1 goto use_python

echo.
echo [ERROR] Python 3.12+ was not found in PATH.
echo Install Python from https://www.python.org/downloads/
echo and enable "Add python.exe to PATH" during setup.
goto finished

:use_py_launcher
py -3 app.py %*
goto finished

:use_python
python app.py %*
goto finished

:finished
echo.
echo This window stays open. Press any key to close it.
pause >nul
