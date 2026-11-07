"""Node 登录桥的 Python 侧：常驻子进程 + 行分隔 JSON-RPC。

设计取舍
--------
**为什么不重写。** 12 家渠道的登录逻辑是 `dsh-our-free-model` 的 `channel-pack`
里约两千行实测可用的代码（厂商端点、PKCE、DPoP、二维码页、轮询节奏都在里面）。
用 Python 抄一遍，等于把同一份协议实现维护两遍，并且从此要跟着厂商改两次。
这里只做转发。

**为什么是常驻进程而不是每次调用起一个。** 登录是两阶段的：`account.create`
返回 URL 并把待办状态放在进程内存里，`login.poll` 再回来问结果。每阶段起一个
新进程会让第二阶段看不到第一阶段的状态。

**为什么串行。** `pack.js` 的 pool 与 pending 表不是并发安全的。桥接进程内部
按到达顺序执行，Python 侧也用一把锁保证请求不乱序。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _hidden_child_kwargs() -> dict:
    """让 node 子进程**不要**弹出自己的控制台窗口。

    这是「exe 已经藏起来了、后台还挂着一个黑框」的真正原因：Python 侧用
    `create_subprocess_exec` 起 node 时没传 `creationflags`，Windows 给 node 新开
    一个控制台（各带一个 conhost.exe）。`pythonw.exe` 本身没有控制台，所以那个
    黑框完全是这一层凭空造出来的 —— 启动器再干净也挡不住它。

    CREATE_NO_WINDOW 让子进程沿用「无控制台」的环境；node 的流仍走管道，
    不受影响。非 Windows 平台上这个参数不存在，直接返回空。
    """
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


class BridgeError(RuntimeError):
    """桥接进程不可用，或 pack.js 返回了错误。"""


# 可以重试的失败：传输层被重置/超时，而不是厂商明确拒绝。
_RETRYABLE = (
    "fetch failed", "ECONNRESET", "ETIMEDOUT", "ECONNREFUSED", "UND_ERR",
    "socket hang up", "network", "EAI_AGAIN", "ENOTFOUND",
    "UNEXPECTED_EOF", "aborted", "timeout",
    "超时", "意外退出",
)


def _retryable(message: str) -> bool:
    text = message.lower()
    return any(token.lower() in text for token in _RETRYABLE)


class PackBridge:
    """把 `channel-pack` 的 RPC 暴露给网关，接口与 pack.js 的 handleMethod 一致。"""

    # 默认从用户目录读已安装的插件；可用 DSH_PACK_HOME 覆盖。
    DEFAULT_PACK_HOME = Path.home() / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-our-free-model"

    def __init__(self, *, node: str | None = None, script: Path | None = None,
                 pack_home: str | None = None, dsh_modules: str | None = None,
                 request_timeout: float = 130.0, start_timeout: float = 45.0,
                 proxy: str | None = None):
        self.node = node or shutil.which("node") or "node"
        self.script = script or ROOT / "scripts" / "pack_bridge.mjs"
        self.loader = ROOT / "scripts" / "pack_register.mjs"
        self.pack_home = pack_home or os.environ.get("DSH_PACK_HOME") or str(self.DEFAULT_PACK_HOME)
        self.dsh_modules = dsh_modules or os.environ.get("DSH_NODE_MODULES") or ""
        # 部分厂商（MiniMax、Cline）从当前网络直连会握手超时。pack.js 自己会读
        # HTTPS_PROXY/HTTP_PROXY，也会回落到 Windows 系统代理；这里把账号级代理
        # 显式传进去，避免「浏览器能打开、网关打不开」。
        self.proxy = proxy or os.environ.get("GATEWAY_UPSTREAM_PROXY") or None
        self.request_timeout = request_timeout
        self.start_timeout = start_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._ready: dict | None = None
        self._stderr = ""

    # ---- 生命周期 ----

    async def start(self) -> dict:
        """启动并等待 `ready`。已就绪时直接返回缓存。"""
        async with self._lock:
            return await self._start_locked()

    async def _start_locked(self) -> dict:
        if self._ready is not None and self._process is not None and self._process.returncode is None:
            return self._ready
        await self._stop_locked()
        if not Path(self.script).is_file():
            raise BridgeError("登录桥脚本缺失，请重新安装网关")
        env = dict(os.environ)
        env["DSH_PACK_HOME"] = self.pack_home
        if self.dsh_modules:
            env["DSH_NODE_MODULES"] = self.dsh_modules
        env.setdefault("PACK_BRIDGE_TIMEOUT_MS", str(int(self.request_timeout * 1000)))
        if self.proxy:
            # pack.js 读小写优先，两套都写避免大小写差异导致漏读。
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                env[name] = self.proxy
            # 回环必须绕过代理：loomy / raccoon 的登录页是插件自己起在
            # 127.0.0.1 的本地服务，走代理会连不上。
            bypass = "127.0.0.1,localhost,::1"
            existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
            merged = ",".join(p for p in (bypass, existing) if p)
            env["NO_PROXY"] = env["no_proxy"] = merged
        # `--import` 走 ESM 解析，Windows 绝对路径必须转成 file:// URL；
        # 直接给 `C:\...` 会被当成协议名 'c:' 而报 ERR_UNSUPPORTED_ESM_URL_SCHEME。
        loader_url = Path(self.loader).resolve().as_uri()
        try:
            process = await asyncio.create_subprocess_exec(
                self.node, "--import", loader_url, str(self.script),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, cwd=str(ROOT),
                **_hidden_child_kwargs(),
            )
        except (OSError, ValueError) as exc:
            raise BridgeError(f"无法启动登录桥：{exc}") from None
        self._process = process
        self._stderr = ""
        try:
            line = await asyncio.wait_for(process.stdout.readline(), self.start_timeout)
        except TimeoutError:
            await self._stop_locked()
            raise BridgeError("登录桥启动超时") from None
        if not line:
            detail = await self._collect_stderr()
            await self._stop_locked()
            raise BridgeError("登录桥未能启动" + (f"：{detail}" if detail else ""))
        try:
            hello = json.loads(line)
        except ValueError:
            await self._stop_locked()
            raise BridgeError("登录桥返回了无法解析的握手信息") from None
        if hello.get("ready") is not True:
            await self._stop_locked()
            raise BridgeError(str(hello.get("error") or "登录桥未就绪"))
        self._ready = hello
        return hello

    async def _collect_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return self._stderr
        try:
            data = await asyncio.wait_for(self._process.stderr.read(4096), 2)
        except (TimeoutError, OSError):
            return self._stderr
        if data:
            self._stderr = data.decode("utf-8", "replace").strip()[:800]
        return self._stderr

    async def _stop_locked(self) -> None:
        process, self._process, self._ready = self._process, None, None
        if process is None or process.returncode is not None:
            return
        try:
            if process.stdin is not None and not process.stdin.is_closing():
                process.stdin.close()
            await asyncio.wait_for(process.wait(), 3)
        except (TimeoutError, OSError):
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), 3)
            except (TimeoutError, OSError, ProcessLookupError):
                pass

    async def aclose(self) -> None:
        async with self._lock:
            await self._stop_locked()

    # ---- 调用 ----

    @property
    def ready(self) -> dict | None:
        return self._ready

    async def call(self, method: str, payload: dict | None = None, *, attempts: int = 5) -> object:
        """调用桥接进程。`account.create` 走厂商网络，会被瞬时重置打断，故默认重试。

        重试只在**传输层**失败时发生。厂商返回的业务错误（例如 scope 不被允许）
        立即上抛——重试一个确定性拒绝只会拖慢报错。

        次数按实测取：MiniMax 的 oauth2/device/code 在这条网络上约 1/3 会
        SSL EOF 或读超时（2.8s 到 22s 不等）。3 次仍有约 4% 的全败概率，
        5 次降到 0.4%，代价是失败时要多等几秒。
        """
        async with self._lock:
            last: BridgeError | None = None
            for attempt in range(max(1, attempts)):
                try:
                    return await self._call_locked(method, payload or {})
                except BridgeError as exc:
                    last = exc
                    if not _retryable(str(exc)) or attempt == attempts - 1:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            raise last or BridgeError("登录失败")

    async def _call_locked(self, method: str, payload: dict) -> object:
        process = self._process
        if process is None or process.returncode is not None:
            await self._start_locked()
            process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise BridgeError("登录桥不可用")
        self._seq += 1
        request_id = str(self._seq)
        line = json.dumps({"id": request_id, "method": method, "payload": payload},
                          ensure_ascii=False) + "\n"
        try:
            process.stdin.write(line.encode("utf-8"))
            await process.stdin.drain()
        except (OSError, RuntimeError):
            await self._stop_locked()
            raise BridgeError("登录桥进程已退出，请重试") from None
        try:
            raw = await asyncio.wait_for(process.stdout.readline(), self.request_timeout)
        except TimeoutError:
            await self._stop_locked()
            raise BridgeError("登录桥响应超时") from None
        if not raw:
            detail = await self._collect_stderr()
            await self._stop_locked()
            raise BridgeError("登录桥进程意外退出" + (f"：{detail}" if detail else ""))
        try:
            reply = json.loads(raw)
        except ValueError:
            raise BridgeError("登录桥返回了无法解析的响应") from None
        # 桥接进程可能夹带非请求响应（例如重启后的 ready 行），按 id 对齐跳过。
        if reply.get("id") != request_id:
            if reply.get("id") is None and "error" in reply:
                raise BridgeError(str(reply["error"].get("message") or "登录桥报错"))
            raise BridgeError("登录桥响应错位，请重试")
        if reply.get("ok") is not True:
            raise BridgeError(str((reply.get("error") or {}).get("message") or "登录失败"))
        return reply.get("value")


async def _selftest() -> int:  # pragma: no cover - 手工冒烟入口
    bridge = PackBridge()
    try:
        hello = await bridge.start()
        print("ready:", json.dumps(hello, ensure_ascii=False)[:200])
        for provider in sys.argv[1:] or ["buddy"]:
            value = await bridge.call("account.create", {"provider": provider})
            print(provider, "->", json.dumps(value, ensure_ascii=False)[:200])
    finally:
        await bridge.aclose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_selftest()))
