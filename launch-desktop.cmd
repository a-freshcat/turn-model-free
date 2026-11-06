@echo off
rem ---------------------------------------------------------------------------
rem Standalone desktop application launcher using pywebview (WebView2).
rem Kept ASCII-only for safe cmd.exe OEM code page execution on Windows.
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

rem Prefer pythonw.exe if available to run without a background terminal window
set "DESKTOP_PYTHON=%GATEWAY_PYTHON%"
for %%I in ("%GATEWAY_PYTHON%") do (
    if exist "%%~dpIpythonw.exe" (
        set "DESKTOP_PYTHON=%%~dpIpythonw.exe"
    )
)

"%DESKTOP_PYTHON%" "%~dp0launch_desktop.py" --port %GATEWAY_PORT% %*
set "GATEWAY_EXIT=%ERRORLEVEL%"
popd
if not "%GATEWAY_EXIT%"=="0" (
    echo.
    echo Desktop application exited with code %GATEWAY_EXIT%.
    pause
)
exit /b %GATEWAY_EXIT%
