"""Double-click launcher: bind loopback, start Uvicorn, then open the channel UI."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _check_health(url: str) -> bool:
    try:
        # Ignore shell/system proxy settings for our own loopback probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/healthz", timeout=1) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def _open_browser(url: str) -> None:
    if os.name == "nt":
        os.startfile(url)
    elif not webbrowser.open(url, new=2):
        raise OSError("No browser accepted the URL")


API_KEY_FILE = ROOT / ".gateway-api-key"


def _load_or_create_api_key() -> str:
    """读取持久化的网关 key；不存在则生成一次并落盘。

    文件权限尽力收紧（Windows 上仅当前用户可读）；失败也不阻断启动 ——
    这把 key 是**本机回环**服务的凭据，收紧失败只是降低一点纵深。
    """
    try:
        existing = API_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except (OSError, UnicodeError):
        pass
    key = secrets.token_urlsafe(32)
    try:
        API_KEY_FILE.write_text(key + "\n", encoding="utf-8")
        if os.name == "nt":
            subprocess.run(
                ["icacls", str(API_KEY_FILE), "/inheritance:r",
                 "/grant:r", os.environ.get("USERNAME", "") + ":F"],
                capture_output=True, check=False,
            )
        else:
            API_KEY_FILE.chmod(0o600)
    except (OSError, UnicodeError):
        pass
    return key


async def _open_when_ready(server, base: str, *, no_browser: bool) -> None:
    for _ in range(150):
        if server.should_exit:
            return
        if server.started and await asyncio.to_thread(_check_health, base):
            url = base + "/"
            print(f"Channel dashboard: {url}", flush=True)
            bootstrap = os.environ.get("GATEWAY_UI_LAUNCH_TOKEN", "")
            browser_url = url + "#bootstrap=" + bootstrap if bootstrap else url
            if not no_browser:
                try:
                    await asyncio.to_thread(_open_browser, browser_url)
                except OSError:
                    print("Browser could not be opened. Open the URL above manually.", flush=True)
            return
        await asyncio.sleep(0.1)
    print("Browser startup check timed out. Inspect the server output above.", flush=True)


async def serve(port: int, *, no_browser: bool) -> None:
    import uvicorn

    # Own the socket before starting, so we never open an unrelated service's UI
    # when another program already occupies the requested port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(
            "main:app", host="127.0.0.1", port=port, workers=1, access_log=False,
        ))
        opener = asyncio.create_task(
            _open_when_ready(server, f"http://127.0.0.1:{port}", no_browser=no_browser),
        )
        try:
            await server.serve(sockets=[sock])
        finally:
            opener.cancel()
            await asyncio.gather(opener, return_exceptions=True)
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Use accounts.json or GATEWAY_ACCOUNTS")
    parser.add_argument("--port", type=int, default=8000, help="Loopback port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Start without opening a browser")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    # Local dependencies are installed in .vendor in this workspace.
    if (ROOT / ".vendor" / "fastapi").is_dir():
        sys.path.insert(0, str(ROOT / ".vendor"))
    os.chdir(ROOT)
    configured = os.environ.get("GATEWAY_ACCOUNTS")
    real_path = Path(configured) if configured else ROOT / "accounts.json"
    account_path = real_path if args.real or real_path.is_file() else ROOT / "accounts.demo.json"
    if not account_path.is_file():
        print("Accounts file is missing. See accounts.example.json and README.md.", file=sys.stderr)
        return 1
    os.environ["GATEWAY_ACCOUNTS"] = str(account_path.resolve())
    bootstrap = secrets.token_urlsafe(32)
    os.environ["GATEWAY_UI_BOOTSTRAP"] = bootstrap
    os.environ["GATEWAY_UI_LAUNCH_TOKEN"] = bootstrap
    # API key 要跨重启稳定：外部客户端（以及 DSH 的 OpenAI 兼容配置）把它写进
    # 配置文件后，每次重启都换一把会让那些客户端全部 401，而报错看起来像
    # 「网关坏了」。优先环境变量，其次落盘的 key 文件，最后才新生成。
    if not os.environ.get("GATEWAY_API_KEY"):
        os.environ["GATEWAY_API_KEY"] = _load_or_create_api_key()

    print("Mode: " + ("configured upstream accounts" if account_path != ROOT / "accounts.demo.json" else "offline demo account pool"))
    print("Gateway API key (manual dashboard login or /docs -> Authorize):")
    print(os.environ["GATEWAY_API_KEY"])
    print("Keep this window open. Press Ctrl+C to stop the gateway.", flush=True)
    try:
        asyncio.run(serve(args.port, no_browser=args.no_browser))
    except ModuleNotFoundError:
        print("Dependencies are missing. Install requirements.txt first.", file=sys.stderr)
        return 1
    except OSError as exc:
        # Do not print exception text that might include deployment details.
        print(f"Cannot start the loopback listener (OS error {exc.errno}); try another --port.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
