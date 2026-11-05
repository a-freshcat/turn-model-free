"""不联网、不用凭据的测试上游。真实账号不会经过这里。"""

import json
import time
from uuid import uuid4

import anyio
import httpx

from config import Account
from upstream_client import DONE, encode_event


class DemoStream(httpx.AsyncByteStream):
    def __init__(self, frames: list[bytes]):
        self.frames = frames

    async def __aiter__(self):
        for frame in self.frames:
            await anyio.sleep(0.06)
            yield frame


def transport_for(account: Account) -> httpx.AsyncBaseTransport | None:
    if account.driver != "demo":
        return None

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        meta = {"id": f"chatcmpl-{uuid4().hex}", "created": int(time.time()), "model": payload["model"]}
        denied = {"error": {"code": 11128, "message": "Unapproved channel (demo)"}}
        blocked = account.id == "demo-blocked"
        text = "本地模拟上游已收到请求。"
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if not payload["stream"]:
            if blocked:
                return httpx.Response(400, json=denied)
            return httpx.Response(200, json={
                **meta, "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": usage,
            })

        def chunk(delta, finish=None):
            return encode_event({
                **meta, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                **({"usage": None} if include_usage else {}),
            })

        include_usage = (payload.get("stream_options") or {}).get("include_usage") is True
        frames = [chunk({"role": "assistant", "content": ""})]
        if blocked:
            frames.append(encode_event(denied))
        else:
            frames.extend((chunk({"content": text[:5]}), chunk({"content": text[5:]}), chunk({}, "stop")))
            if include_usage:
                frames.append(encode_event({
                    **meta, "object": "chat.completion.chunk", "choices": [], "usage": usage,
                }))
            frames.append(DONE)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=DemoStream(frames))

    return httpx.MockTransport(handle)
