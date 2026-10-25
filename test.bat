@echo off
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto run_python
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto run_python
)
echo.
echo [ERROR] Python 3.12+ was not found in PATH.
goto finished

:run_python
echo === Python tests ===
%PYTHON_CMD% -m unittest discover -s tests -t .
if errorlevel 1 goto failed

echo.
echo === Frontend tests ===
where node >nul 2>nul
if errorlevel 1 (
    echo [SKIP] Node.js not found in PATH, frontend tests skipped.
    goto passed
)
node --test "web/tests/*.test.js"
if errorlevel 1 goto failed

:passed
echo.
echo All tests passed.
goto finished

:failed
echo.
echo [FAILED] Tests did not pass.

:finished
echo.
echo This window stays open. Press any key to close it.
pause >nul
