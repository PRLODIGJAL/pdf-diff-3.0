@echo off
rem ============================================================
rem  Force-rebuild the Python environment.
rem  Use this only when the tool suddenly stops starting.
rem  Pure ASCII on purpose - see the note in the launcher .bat.
rem ============================================================
setlocal
pushd "%~dp0"

set "PYEXE="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto RUN

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto RUN

echo.
echo   [!] Python 3 was not found. Install it first, then retry.
echo.
pause
popd
exit /b 1

:RUN
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
%PYEXE% "%~dp0setup_env.py" --rebuild --setup

echo.
echo   Done. Now double-click the launcher .bat as usual.
echo.
pause
popd
exit /b 0
