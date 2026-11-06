"""Desktop client launcher using pywebview.

Runs the local FastAPI gateway backend in a daemon worker thread, and presents a
standalone native Windows desktop application window (powered by system WebView2)
without any browser chrome, tabs, or background terminal clutter.

When the native window is closed, the application and backend terminate cleanly.
"""

import argparse
import logging
import os
import sys
import threading
import time
import traceback
from copy import copy
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if (ROOT / ".vendor" / "fastapi").is_dir():
    sys.path.insert(0, str(ROOT / ".vendor"))

import uvicorn
import webview

from launch_web import (
    DEFAULT_PORT,
    _check_health,
    _load_or_create_api_key,
    _port_available,
    _try_takeover_previous_gateway,
    port_in_use_message,
)


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        safe_record = copy(record)
        safe_record.exc_text = None
        return super().format(safe_record)

    def formatException(self, exc_info):
        def format_branch(error, tb, depth):
            indent = "  " * depth
            yield indent + type(error).__name__
            for frame in traceback.extract_tb(tb)[-12:]:
                yield f"{indent}{Path(frame.filename).name}:{frame.lineno} {frame.name}"
            if isinstance(error, BaseExceptionGroup) and depth < 3:
                for child in error.exceptions[:4]:
                    yield from format_branch(child, child.__traceback__, depth + 1)

        return "\n".join(format_branch(exc_info[1], exc_info[2], 0))


# 启动提示用的独立 logger。
#
# 为什么不复用 uvicorn 的：uvicorn 配的是 `log_level="warning"`，`info` 会被
# 直接过滤掉。之前为了让它显出来，就借 `warning` 级别打印，结果控制台出现
# `WARNING: Desktop diagnostic log enabled...` —— 用户以为是启动失败来问。
# 状态提示就该用 info 级别、并且不带醒目的级别前缀。
LAUNCHER_LOGGER = 'gateway.desktop'

# 级别必须**显式**设成 INFO。不设的话 effective level 继承 root（默认
# WARNING），启动提示这类 INFO 记录会被整条丢掉 —— 实测日志文件里 0 字节。
# 不冒泡：控制台那句由 `_notice()` 自己 print，冒泡会让它重复出现。
_LAUNCHER = logging.getLogger(LAUNCHER_LOGGER)
_LAUNCHER.setLevel(logging.INFO)
_LAUNCHER.propagate = False


def launcher_logger():
    return _LAUNCHER


class ServerThread(threading.Thread):
    def __init__(self, port: int):
        super().__init__(daemon=True)
        self.port = port
        config = uvicorn.Config(
            "main:app",
            host="127.0.0.1",
            port=port,
            workers=1,
            access_log=False,
            log_level="warning",
        )
        self.server = uvicorn.Server(config)

    def run(self):
        # The desktop launcher may have no attached console.
        log_path = ROOT / '.gateway-desktop.log'
        try:
            handler = RotatingFileHandler(log_path, maxBytes=512_000,
                                          backupCount=2, encoding='utf-8')
        except OSError:
            self._notice(f'诊断日志无法写入（{log_path}），本次启动不记录日志', level=logging.WARNING)
            self.server.run()
            return
        handler.setFormatter(DiagnosticFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        loggers = [logging.getLogger(name)
                   for name in ('antigravity_driver', 'uvicorn.error', LAUNCHER_LOGGER)]
        for logger in loggers:
            logger.addHandler(handler)
        try:
            # 状态提示，不是故障。
            #
            # 这里**不用 logging 输出到控制台**：uvicorn 启动时会重配 logging，
            # 把已有 handler 换掉，消息就静默丢了（实测控制台一个字都没有，只有
            # 日志文件里有记录）。`print` 直接写 stderr，不会被那套配置影响。
            #
            # 也不打 "WARNING:" 前缀：这条曾经用 warning 级别借道显示，结果
            # 用户看到 "WARNING:" 以为启动失败了。
            self._notice(f'诊断日志已启用 · 端口 {self.port} · 写入 {log_path}')
            self._notice(f'打开 http://127.0.0.1:{self.port}/ 即可使用')
            self.server.run()
        finally:
            for logger in loggers:
                logger.removeHandler(handler)
            handler.close()

    @staticmethod
    def _notice(message, level=logging.INFO):
        """同时送到控制台与诊断日志；控制台那句不带级别前缀。"""
        stream = sys.stderr if sys.stderr is not None else sys.__stderr__
        if stream is not None:
            try:
                print(message, file=stream, flush=True)
            except (OSError, ValueError, UnicodeError):
                # 无控制台（pythonw）或编码不支持时，忽略即可 —— 日志文件仍有记录。
                pass
        try:
            logging.getLogger(LAUNCHER_LOGGER).log(level, message)
        except Exception:  # noqa: BLE001 - 提示失败绝不能影响启动
            pass

    def stop(self):
        self.server.should_exit = True


def _print_startup_banner(port: int, api_key: str) -> None:
    """打印客户端接入需要的两个值。

    直接用 `print` 而不经 logging：uvicorn 启动时会重配 logging，之前那条
    状态提示就是这样被吞掉的。`print` 不受那套配置影响。
    """
    lines = [
        '',
        f'本机网关地址：http://127.0.0.1:{port}/v1',
        '网关 Key（客户端接入用；/docs 里点 Authorize 也填它）：',
        api_key,
        f'控制台：http://127.0.0.1:{port}/',
        '',
    ]
    stream = sys.stdout if sys.stdout is not None else sys.__stdout__
    for line in lines:
        if stream is not None:
            try:
                print(line, file=stream, flush=True)
            except (OSError, ValueError, UnicodeError):
                # 无控制台（pythonw）或编码不支持：界面里同样能复制，不算错误。
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Loopback port (default: {DEFAULT_PORT})")
    parser.add_argument("--real", action="store_true", help="Use accounts.json")
    args = parser.parse_args()

    os.chdir(ROOT)
    configured = os.environ.get("GATEWAY_ACCOUNTS")
    real_path = Path(configured) if configured else ROOT / "accounts.json"
    account_path = real_path if args.real or real_path.is_file() else ROOT / "accounts.demo.json"
    if not account_path.is_file():
        print("Accounts file is missing. See accounts.example.json.", file=sys.stderr)
        return 1

    os.environ["GATEWAY_ACCOUNTS"] = str(account_path.resolve())
    import secrets
    bootstrap = secrets.token_urlsafe(32)
    os.environ["GATEWAY_UI_BOOTSTRAP"] = bootstrap
    os.environ["GATEWAY_UI_LAUNCH_TOKEN"] = bootstrap

    if not os.environ.get("GATEWAY_API_KEY"):
        os.environ["GATEWAY_API_KEY"] = _load_or_create_api_key()

    # 把 Key 与地址打到控制台：客户端接入、/docs 的 Authorize 都要用它。
    #
    # `launch-web.cmd` 那条路径一直有这段输出，桌面端漏了 —— 同样是启动网关，
    # 用户在这里看不到 Key，只能去界面里翻。这里补齐，措辞与网页端保持一致。
    #
    # 无控制台时（pythonw）print 会静默失败，不影响启动：桌面窗口里同样能看到
    # 并复制这两个值。
    _print_startup_banner(args.port, os.environ["GATEWAY_API_KEY"])

    # 端口自愈检测
    if not _port_available(args.port):
        if not _try_takeover_previous_gateway(args.port):
            print(port_in_use_message(args.port), file=sys.stderr)
            return 1

    server_thread = ServerThread(args.port)
    server_thread.start()

    base_url = f"http://127.0.0.1:{args.port}"
    for _ in range(100):
        if _check_health(base_url):
            break
        time.sleep(0.1)

    app_url = f"{base_url}/#bootstrap={bootstrap}"

    # 创建原生桌面应用窗口
    window = webview.create_window(
        title="白嫖模型接入 - 桌面控制台",
        url=app_url,
        width=1380,
        height=860,
        min_size=(960, 600),
        text_select=True,
    )

    def on_closed():
        server_thread.stop()

    window.events.closed += on_closed

    # 启动 GUI 消息循环（阻塞至窗口关闭）
    webview.start(debug=False)
    server_thread.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
