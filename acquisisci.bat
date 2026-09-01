@echo off
cd /d "%~dp0"
py -3 "%~dp0acquisisci.py" %*
if errorlevel 1 python "%~dp0acquisisci.py" %*
pause
