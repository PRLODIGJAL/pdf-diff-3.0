@echo off
rem ============================================================
rem  PDF diff tool - Windows launcher
rem
rem  This file is deliberately pure ASCII. Batch files are decoded
rem  by cmd.exe with the console codepage, which differs between
rem  machines; all logic and all Chinese messages live in
rem  setup_env.py, where the encoding is guaranteed.
rem
rem  No parenthesised blocks are used on purpose: "if errorlevel"
rem  inside a block is a classic source of surprises.
rem ============================================================
setlocal
pushd "%~dp0"

set "VENVPY=%~dp0.venv\Scripts\python.exe"

rem ---- 1) if the bundled venv already exists, run the app directly ----
if exist "%VENVPY%" goto RUN_APP

rem ---- 2) otherwise, need a system Python to create the venv first ----
set "PYEXE="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if defined PYEXE goto SETUP

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYEXE=python"
if defined PYEXE goto SETUP

echo.
echo   [!] Python 3 was not found on this computer.
echo.
echo       Install it from:
echo         https://www.python.org/downloads/windows/
echo.
echo       During setup, TICK this box:
echo         [x] Add python.exe to PATH
echo.
echo       Then double-click this file again.
echo.
pause
popd
exit /b 1

:SETUP
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
%PYEXE% "%~dp0setup_env.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto FAIL
if not exist "%VENVPY%" goto FAIL

rem ---- 3) venv is ready, run the app with the venv python (foreground) ----
:RUN_APP
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"%VENVPY%" -B "%~dp0diff_app3.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto FAIL
goto DONE

:FAIL
echo.
echo   [!] Stopped with exit code %RC%. See the messages above.
echo.
pause

:DONE
popd
exit /b %RC%
