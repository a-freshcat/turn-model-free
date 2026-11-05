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

# 默认端口。8000 是本机另一个 Web 插件面板的地址，两边撞在一起时很难分辨
# 到底是谁起失败了，所以这里换一个不会被别的东西占的位置。
DEFAULT_PORT = 8199


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


class PortInUse(RuntimeError):
    """请求的端口已被别的进程监听。"""


def _port_owner(port: int) -> str:
    """尽力说出端口上现在是谁在监听；问不到就返回空串。

    只用来把报错写清楚。拿不到进程信息（权限不足、netstat 不可用）不是错误——
    那句话仍然告诉用户「端口被占」，只是少了进程名。
    """
    try:
        listing = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    pid = ""
    for line in (listing.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[-2].upper() != "LISTENING":
            continue
        if parts[1].endswith(f":{port}") and parts[-1].isdigit():
            pid = parts[-1]
            break
    if not pid:
        return ""
    try:
        probe = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=5)
        name = (probe.stdout or "").strip().split(",")[0].strip('"')
    except (OSError, subprocess.SubprocessError):
        name = ""
    return f"PID {pid}" + (f"（{name}）" if name else "")


def port_in_use_message(port: int) -> str:
    """端口被占用时的完整说明。

    只说 `try another --port` 等于让用户自己猜。实测最难分辨的一种情形是
    **上一次启动的网关窗口还开着**：端口在听、页面也能打开，看起来像这个面板
    起成功了，其实是旧进程。所以这里点名端口、点名占用者，并给出两条明确出路。
    """
    owner = _port_owner(port)
    lines = [f"端口 {port} 已被占用，无法启动。" + (f"占用者：{owner}。" if owner else "")]
    lines.append("常见原因：上一次启动的网关窗口还在运行，关掉那个窗口再试。")
    lines.append(f"或者换一个端口：launch-web.cmd --port {port + 1}")
    return "\n".join(lines)


def _port_available(port: int) -> bool:
    """端口现在能不能被我们独占。探测完立刻释放，真正的 bind 在 serve() 里。

    竞态窗口只有几毫秒，且另一边是「上一次没关的窗口」这种不会突然抢的情况，
    所以这里先探一次只为把报错提前，不代替 serve() 里的真 bind。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


async def serve(port: int, *, no_browser: bool) -> None:
    import uvicorn

    # Own the socket before starting, so we never open an unrelated service's UI
    # when another program already occupies the requested port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            # 10048 (WSAEADDRINUSE) / 98 (Linux EADDRINUSE) / 48 (macOS)：端口被占。
            # 这和「权限不足」「地址不可用」是不同的处置方式，必须分开说，
            # 否则用户只知道起不来，不知道该关掉什么。
            if exc.errno in {48, 98, 10048} or getattr(exc, "winerror", None) == 10048:
                raise PortInUse(port_in_use_message(port)) from None
            raise
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
    # 默认端口刻意不用 8000：那是很多本机 Web 工具抢的位置（本机另一个
    # 插件面板就占着它）。撞上时 `serve()` 会 bind 失败退出，用户看到的只是
    # 一句「Cannot start the loopback listener」——但那个 8000 上跑着的其实是
    # 别的服务，很容易被当成这个面板自己起坏了。
    # 8199 落在 8000~8999 的常规开发端口段，且与插件自身的 8326 错开。
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Loopback port (default: {DEFAULT_PORT})")
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

    # 先探端口，再打印「Mode / 密钥 / 请保持窗口打开」。
    #
    # 顺序很重要：原来是在 serve() 里 bind 失败，于是窗口先显示密钥、再报错，
    # 看起来像「已经起来了只是有点问题」。端口冲突是启动前的确定失败，应该在
    # 任何「看起来成功了」的输出之前就被拦下。
    if not _port_available(args.port):
        print(port_in_use_message(args.port), file=sys.stderr)
        return 1

    print("Mode: " + ("configured upstream accounts" if account_path != ROOT / "accounts.demo.json" else "offline demo account pool"))
    print("Gateway API key (manual dashboard login or /docs -> Authorize):")
    print(os.environ["GATEWAY_API_KEY"])
    print("Keep this window open. Press Ctrl+C to stop the gateway.", flush=True)
    try:
        asyncio.run(serve(args.port, no_browser=args.no_browser))
    except PortInUse as exc:
        # 端口被占：说清是哪条端口、被谁占、两条出路都给出。
        print(str(exc), file=sys.stderr)
        return 1
    except ModuleNotFoundError:
        print("Dependencies are missing. Install requirements.txt first.", file=sys.stderr)
        return 1
    except OSError as exc:
        # 其余的 bind 失败（权限、地址不可用等）。不打印异常正文，它可能带上
        # 部署细节；只给错误号，并指出换端口这条路不适用。
        print(f"Cannot start the loopback listener on 127.0.0.1:{args.port} "
              f"(OS error {exc.errno}).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
