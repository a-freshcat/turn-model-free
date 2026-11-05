"""启动真实 Uvicorn loopback 服务，验证 HTTP、逐帧 SSE 和 curl 后自动退出。"""

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn

from main import app


async def main():
    key = secrets.token_urlsafe(24)
    with patch.dict(os.environ, {"GATEWAY_API_KEY": key, "GATEWAY_ACCOUNTS": str(ROOT / "accounts.demo.json")}):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False,
                                         headers={"Authorization": f"Bearer {key}"}) as client:
                for _ in range(100):
                    if task.done():
                        await task
                        raise RuntimeError("Uvicorn stopped during startup")
                    if server.started:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise TimeoutError("Uvicorn startup timed out")

                health = await client.get("/healthz")
                assert health.status_code == 200
                docs = await client.get("/docs")
                assert docs.status_code == 200 and "swagger-ui" in docs.text
                schema = (await client.get("/openapi.json")).json()
                assert schema["components"]["securitySchemes"]["GatewayBearer"]["scheme"] == "bearer"
                assert schema["paths"]["/v1/chat/completions"]["post"]["security"] == [{"GatewayBearer": []}]
                print("Web /docs and Authorize schema PASS")
                times, lines = [], []
                start = time.monotonic()
                async with client.stream("POST", "/v1/chat/completions",
                                         content=(ROOT / "examples/stream.json").read_bytes(),
                                         headers={"Content-Type": "application/json"}) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            times.append(time.monotonic() - start)
                            lines.append(line)
                assert lines[-1] == "data: [DONE]"
                assert sum('"role":"assistant"' in line for line in lines) == 1
                assert times[-1] - times[0] >= 0.1, "SSE should arrive progressively over HTTP"
                assert app.state.pool.states[0].fail_count == 1
                assert app.state.pool.states[0].status == "cooldown"
                assert all(s.in_flight == 0 for s in app.state.pool.states)
                print(f"HTTP SSE PASS: {len(lines)} events; first={times[0]:.3f}s last={times[-1]:.3f}s")

                response = await client.post("/v1/chat/completions",
                                             content=(ROOT / "examples/chat.json").read_bytes(),
                                             headers={"Content-Type": "application/json"})
                assert response.status_code == 200
                assert response.json()["object"] == "chat.completion"
                print("HTTP JSON PASS; 11128 failover PASS")

                curl = shutil.which("curl.exe") or shutil.which("curl")
                if curl:
                    for filename in ("chat.json", "stream.json"):
                        result = await asyncio.to_thread(subprocess.run, [
                            curl, "--noproxy", "*", "--silent", "--show-error", "--fail-with-body",
                            "--no-buffer", "--max-time", "10", "-H", f"Authorization: Bearer {key}",
                            "-H", "Content-Type: application/json", "--data-binary",
                            "@" + str(ROOT / "examples" / filename), base + "/v1/chat/completions",
                        ], capture_output=True, timeout=12,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                        assert result.returncode == 0, f"curl failed (exit {result.returncode})"
                        assert result.stdout.strip()
                        if filename == "stream.json":
                            assert b"data: [DONE]" in result.stdout
                        print(f"curl {filename} PASS")
                else:
                    print("curl unavailable; HTTP checks above completed")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            sock.close()


if __name__ == "__main__":
    asyncio.run(main())
