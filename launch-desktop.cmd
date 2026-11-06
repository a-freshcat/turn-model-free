@echo off
rem Root shortcut: the actual launcher and dependencies live under gateway/.
setlocal
call "%~dp0gateway\launch-desktop.cmd" %*
exit /b %ERRORLEVEL%
