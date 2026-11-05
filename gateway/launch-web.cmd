@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1

rem 本机地址。8000 被另一个 Web 插件面板占着，撞上去只会得到一句
rem "Cannot start the loopback listener"，很难分辨是谁起失败。这里默认 8199，
rem 并显式传给 launch_web.py —— 地址写在命令里，看一眼就知道面板在哪。
rem 想换端口：launch-web.cmd --port 8300
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
