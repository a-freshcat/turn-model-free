@echo off
rem ---------------------------------------------------------------------------
rem Local address. Kept ASCII on purpose: cmd.exe reads this file as the OEM
rem code page (936/GBK on Chinese Windows), so UTF-8 Chinese comments get
rem decoded into garbage, swallow the next line, and run as commands. Notes in
rem Chinese live in gateway\README.md and CHANGELOG.md instead.
rem
rem Default 8199 because 8000 is taken by another local web panel. If it is
rem still busy (a previous window left running), the launcher now says which
rem port and what to do. Change the port here: launch-web.cmd --port 8300
rem ---------------------------------------------------------------------------
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

set "GATEWAY_PORT=8199"

set "GATEWAY_PYTHON=python"
if not exist "%~dp0.vendor\fastapi\__init__.py" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "GATEWAY_PYTHON=%~dp0.venv\Scripts\python.exe"
    )
)

"%GATEWAY_PYTHON%" "%~dp0launch_web.py" --port %GATEWAY_PORT% %*
set "GATEWAY_EXIT=%ERRORLEVEL%"
popd
if not "%GATEWAY_EXIT%"=="0" (
    echo.
    echo Gateway startup failed. Read the error above.
    pause
)
exit /b %GATEWAY_EXIT%