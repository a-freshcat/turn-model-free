@echo off
rem Double-click entry point. The local address (port) is set in
rem gateway\launch-web.cmd, default 8199. This file stays ASCII-only because
rem cmd.exe decodes .cmd as the OEM code page and UTF-8 Chinese becomes mojibake.
rem Notes in Chinese live in gateway\README.md and CHANGELOG.md.
rem Change the port: launch-web.cmd --port 8300
call "%~dp0gateway\launch-web.cmd" %*
exit /b %ERRORLEVEL%