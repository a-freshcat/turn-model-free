@echo off
call "%~dp0gateway\launch-web.cmd" %*
exit /b %ERRORLEVEL%
