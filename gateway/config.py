"""启动时校验号池；凭据留在部署环境，不进入日志和客户端响应。"""

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

ROOT = Path(__file__).resolve().parent
Status = Literal["active", "cooldown", "disabled"]


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    channel: Literal["codearts", "buddy", "workbuddy", "lobsterai", "qoder", "qodercn",
                     "trae", "cline", "loomy", "raccoon", "minimax", "zcode", "antigravity"] = "buddy"
    nickname: str = Field(default="", max_length=64)
    driver: Literal["openai", "demo", "antigravity"] = "openai"
    credential_id: str | None = Field(default=None, pattern=r"^ag-[a-f0-9]{24}$")
    endpoint: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    disabled_models: list[str] = Field(default_factory=list)
    token: SecretStr | None = None
    token_env: str | None = None
    cookie: SecretStr | None = None
    cookie_env: str | None = None
    # 使用已获准渠道提供的标识；开关独立，避免改写其他渠道的消息。
    user_agent: str | None = None
    device_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    tencent_prompt_level: Literal[0, 1, 2, 3] = 0
    status: Status = "active"
    fail_count: int = Field(default=0, ge=0)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    # 每个账号固定一条出站代理，空表示直连。**一个账号只能有一个出口**：
    # 同一凭据在多个 IP 间漂移是最典型的多开特征，比共用出口更可疑。
    # 未配置时回落到全局 GATEWAY_UPSTREAM_PROXY。
    proxy: str | None = None
    # 请求间随机抖动（毫秒）。人工操作的间隔从不整齐，固定间隔是自动化指纹。
    jitter_ms: int = Field(default=0, ge=0, le=5000)

    @field_validator("channel", mode="before")
    @classmethod
    def migrate_google_channel(cls, value):
        return "antigravity" if value == "gemini" else value

    @model_validator(mode="after")
    def validate_account(self):
        if self.driver != "antigravity" and not self.models:
            raise ValueError("model mappings must be nonempty")
        if any(not key.strip() or not value.strip() for key, value in self.models.items()):
            raise ValueError("model mappings must be nonempty")
        if set(self.disabled_models) - self.models.keys():
            raise ValueError("disabled models must be configured aliases")
        for name in ("token", "cookie"):
            literal = getattr(self, name)
            reference = getattr(self, name + "_env")
            if literal is not None and reference is not None:
                raise ValueError(f"configure either {name} or {name}_env")
            if reference is not None and self.status != "disabled":
                value = os.environ.get(reference)
                if not value:
                    raise ValueError(f"missing credential referenced by {name}_env")
                setattr(self, name, SecretStr(value))
            secret = getattr(self, name)
            if secret is not None:
                self._check_header(secret.get_secret_value())

        if self.driver == "antigravity":
            if (self.channel != "antigravity" or not self.credential_id or self.endpoint is not None
                    or any(getattr(self, name) is not None for name in ("token", "cookie", "token_env", "cookie_env"))):
                raise ValueError("Antigravity requires a local OAuth credential reference")
        elif self.driver == "demo":
            if self.endpoint is not None or self.token is not None or self.cookie is not None:
                raise ValueError("demo accounts do not accept endpoints or credentials")
        else:
            url = urlsplit(self.endpoint or "")
            local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (
                not (url.scheme == "https" or local_http) or not url.hostname
                or url.username is not None or url.password is not None or url.fragment
                or not url.path.startswith("/") or url.path == "/"
                or not 0 < (url.port if url.port is not None else 443) <= 65535
                or any(ord(c) <= 32 or ord(c) == 127 for c in (self.endpoint or ""))
            ):
                raise ValueError("endpoint must be a full HTTPS URL or loopback HTTP URL")

        reserved = {
            "authorization", "cookie", "host", "content-length", "content-type",
            "accept", "accept-encoding", "transfer-encoding", "connection", "user-agent",
            "proxy-authorization", "upgrade",
        }
        seen = set()
        for key, value in self.device_headers.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                raise ValueError("invalid device header name")
            lower = key.lower()
            if lower in reserved or lower in seen:
                raise ValueError("reserved or duplicate device header name")
            seen.add(lower)
            self._check_header(value)
        if self.user_agent is not None:
            self._check_header(self.user_agent)
        if self.proxy is not None:
            self.proxy = self._check_proxy(self.proxy)
        return self

    @staticmethod
    def _check_proxy(raw: str) -> str | None:
        """只接受带端口的 http/https/socks5 代理；空串归一成 None（= 直连）。

        `socks5h` 一并归一成 `socks5`：httpx 只认后者，留着会让客户端在
        构造时才抛 ImportError，那时已经离配置错误很远了。
        """
        text = raw.strip()
        if not text:
            return None
        with_scheme = text if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text) else "http://" + text
        url = urlsplit(with_scheme.replace("socks5h://", "socks5://", 1))
        if url.scheme not in {"http", "https", "socks5"}:
            raise ValueError("proxy must be http, https or socks5")
        if not url.hostname or url.port is None:
            raise ValueError("proxy needs host and port (e.g. http://127.0.0.1:7890)")
        if not 0 < url.port <= 65535:
            raise ValueError("proxy port out of range")
        if url.fragment or url.path not in {"", "/"}:
            raise ValueError("proxy must not carry a path")
        # 用**解析后**的 scheme 重建，而不是回原始字符串：`socks5h://` 必须归一成
        # `socks5://`（httpx 只认后者）。早先这里 return with_scheme，归一化等于没做。
        authority = url.netloc.rpartition("@")
        auth, hostport = (authority[0] + "@", authority[2]) if authority[1] else ("", url.netloc)
        return f"{url.scheme}://{auth}{hostport}"

    @staticmethod
    def _check_header(value: str) -> None:
        if not value or any(not 32 <= ord(c) <= 126 for c in value) or value != value.strip():
            raise ValueError("header values must be nonempty printable ASCII without outer whitespace")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)

    accounts: list[Account] = Field(default_factory=list)
    cooldown_seconds: float = Field(default=600, gt=0, le=86400)
    network_cooldown_seconds: float = Field(default=20, gt=0, le=300)
    network_cooldown_max_seconds: float = Field(default=120, gt=0, le=3600)
    max_retries: int = Field(default=3, ge=0, le=3)
    pool_wait_seconds: float = Field(default=1, ge=0, le=30)
    first_output_timeout_seconds: float = Field(default=30, gt=0, le=300)
    stream_timeout_seconds: float = Field(default=300, gt=0, le=3600)

    @model_validator(mode="after")
    def check_pool(self):
        if self.network_cooldown_max_seconds < self.network_cooldown_seconds:
            raise ValueError('network cooldown maximum must be at least its initial duration')
        if len({a.id for a in self.accounts}) != len(self.accounts):
            raise ValueError("account ids must be unique")
        drivers = {a.driver for a in self.accounts}
        if "demo" in drivers and len(drivers) > 1:
            raise ValueError("demo and real accounts must be in separate pools")
        return self


def load_settings() -> Settings:
    path = Path(os.environ.get("GATEWAY_ACCOUNTS", str(ROOT / "accounts.json")))
    try:
        return Settings.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except ValidationError as exc:
        # 不输出 Pydantic 的原始 input 或可能由 URL 解析异常带出的文本。
        problems = ", ".join(
            ".".join(str(part) if isinstance(part, int) or part in {
                "accounts", "id", "driver", "endpoint", "models", "token", "token_env",
                "cookie", "cookie_env", "user_agent", "device_headers", "status", "fail_count",
                "max_concurrency", "tencent_prompt_level", "cooldown_seconds", "max_retries",
                "proxy", "jitter_ms",
                "pool_wait_seconds", "first_output_timeout_seconds", "stream_timeout_seconds",
                "network_cooldown_seconds", "network_cooldown_max_seconds",
            } else "<field>" for part in err["loc"]) or "<root>"
            for err in exc.errors(include_input=False, include_context=False, include_url=False)
        )
        raise RuntimeError(f"Invalid accounts configuration ({problems})") from None
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot read accounts file; check GATEWAY_ACCOUNTS") from None
