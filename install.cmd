@echo off
rem Fily installer. Double-click this file, or run it from a terminal.
rem It runs install.ps1 without changing PowerShells execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "FILY_EXIT=%ERRORLEVEL%"
rem Keep the window open when double-clicked, so the result can be read.
echo %cmdcmdline% | find /i "%~nx0" >nul && (echo. & pause)
exit /b %FILY_EXIT%
