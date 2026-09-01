@echo off
rem Launches BATC Pauser with pythonw.exe so no console window appears.
setlocal
set "PYW="
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
if not defined PYW if exist "C:\Python314\pythonw.exe" set "PYW=C:\Python314\pythonw.exe"
if not defined PYW (
  echo Could not find pythonw.exe on PATH.
  echo Install Python 3, or edit this file and set PYW to its full path.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0batc_pauser.py"
