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
        try:
            handler = RotatingFileHandler(ROOT / '.gateway-desktop.log', maxBytes=512_000,
                                          backupCount=2, encoding='utf-8')
        except OSError:
            logging.getLogger(__name__).warning('Unable to open desktop diagnostic log')
            self.server.run()
            return
        handler.setFormatter(DiagnosticFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        loggers = [logging.getLogger(name) for name in ('antigravity_driver', 'uvicorn.error')]
        for logger in loggers:
            logger.addHandler(handler)
        try:
            loggers[1].warning('Desktop diagnostic log enabled on port %s', self.port)
            self.server.run()
        finally:
            for logger in loggers:
                logger.removeHandler(handler)
            handler.close()

    def stop(self):
        self.server.should_exit = True


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
