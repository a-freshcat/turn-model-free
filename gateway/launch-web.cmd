@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

set "GATEWAY_PYTHON=python"
if not exist "%~dp0.vendor\fastapi\__init__.py" (
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "GATEWAY_PYTHON=%~dp0.venv\Scripts\python.exe"
    )
)

"%GATEWAY_PYTHON%" "%~dp0launch_web.py" %*
set "GATEWAY_EXIT=%ERRORLEVEL%"
popd
if not "%GATEWAY_EXIT%"=="0" (
    echo.
    echo Gateway startup failed. Read the error above.
    pause
)
exit /b %GATEWAY_EXIT%
