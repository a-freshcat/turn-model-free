@echo off
rem 双击入口。本机地址（端口）写在 gateway\launch-web.cmd 里，默认 8199：
rem 8000 是本机另一个 Web 插件面板的位置，撞上去只会得到一句
rem "Cannot start the loopback listener"，很难分辨是谁起失败。
rem 想换端口：launch-web.cmd --port 8300
call "%~dp0gateway\launch-web.cmd" %*
exit /b %ERRORLEVEL%
