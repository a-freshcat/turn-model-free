# free-m-clone 本机网关

Python 3.10+ / FastAPI / Uvicorn / httpx。保留原渠道页面风格，提供账号池、Antigravity Google OAuth、OpenAI Chat Completions 与独立配额看板。

## 安装与启动

在仓库根目录打开终端，先安装一次依赖：

```powershell
python -m venv gateway/.venv
gateway/.venv/Scripts/python.exe -m pip install -r gateway/requirements.txt
.\launch-web.cmd
```

启动后自动打开 `http://127.0.0.1:8000/`。更换端口：`launch-web.cmd --port 8001`。
在 macOS/Linux 中从 gateway 目录安装虚拟环境并运行 `.venv/bin/python launch_web.py`。

无 accounts.json 时使用离线演示号池；在 Antigravity 卡片添加并完成 Google 授权后，自动保存真实配置。已有配置时下次启动直接加载。
授权回调使用当前端口的 localhost 地址，含一次性 state 与 PKCE S256。首次尚无项目的账号需先在官方客户端完成初始化。
管理页连接与 Google 登录独立，模型和配额取自账号目录，不伪造登录或型号。

## 13 家渠道的登录

| 渠道 | 登录方式 |
|---|---|
| Antigravity | 本网关自己的 Google OAuth（PKCE + S256），凭据存 `.gateway-auth/` |
| 其余 12 家 | 委托本机已安装的 `dsh-our-free-model` 插件 `channel-pack`，登录页落在各厂商域名 |

12 家走 Node 子进程桥（`scripts/pack_bridge.mjs`）：网关发一行 JSON，桥调插件自己的
RPC，原样返回。插件装在用户目录下，**桥只读它、不改任何文件**；裸包名
`@deepseek-ai/*` 由 `scripts/pack_loader.mjs` 的 ESM 解析钩子指向宿主实现。

前提与默认路径：

- Node 运行时（`node` 在 PATH 上）。
- 插件位于 `~/.dsh/profiles/desktop/node_modules/dsh-our-free-model`，
  可用 `DSH_PACK_HOME` 覆盖。
- 宿主的 `@deepseek-ai/*` 默认取 `D:/ai_ds/resources/app/dsh/node_modules`，
  可用 `DSH_NODE_MODULES` 覆盖。

缺失时网关照常启动，只有这 12 家的「添加账号」会报出具体原因，不会静默退回导入页。
已有兼容上游配置仍可通过 `account.importWindow` 打开导入页添加。

部分厂商（MiniMax、Cline）从部分网络直连会握手失败。桥会先用
`GATEWAY_UPSTREAM_PROXY`，其次 `HTTPS_PROXY`/`HTTP_PROXY`，再回落到 Windows
系统代理；回环地址始终绕过代理，因为 Loomy / Raccoon 的登录页由插件起在本机。

## 号池与凭据

轮询调度、单号并发限制、默认 600 秒冷却；默认初次请求加最多 3 次重试，同一请求不重复使用失败账号。
全部适用账号不可用返回 503，仍有健康号但重试预算耗尽返回 502。
SSE 首个有效输出前允许切号，输出后失败发送错误帧，不重播答案、不发送成功 [DONE]。

accounts.json 示例见 accounts.example.json；token/cookie 可从环境变量读取。
Antigravity 令牌只保存到 `.gateway-auth/`，配置仅含 credential_id 引用；Windows 当前用户 DPAPI 加密，POSIX 仅限制目录/文件权限。
Windows 凭据不能直接复制给其他电脑或系统用户。令牌临近到期自动续期，撤销授权会要求重新登录。
这些私有文件、依赖和本机缓存均不包含在本版本中。

可选环境变量：`GATEWAY_API_KEY`、`GATEWAY_ACCOUNTS`、`GATEWAY_UPSTREAM_PROXY`、`ANTIGRAVITY_CLIENT_ID`、`ANTIGRAVITY_CLIENT_SECRET`、`ANTIGRAVITY_USER_AGENT`。
默认仅监听 loopback，保持 TLS 校验。管理写操作校验同源，API 使用 Bearer 网关密钥；启动窗口会显示随机生成的密钥。

## 三个配额池

从侧栏「配额看板 · 三个池」或账号「查看配额」打开 `/quota`：

1. Gemini 4 Argon 独立池。
2. Claude / GPT 共用池。
3. 其余 Gemini / 内部型号共用池。

按账号分别显示百分比和进度条，明细默认折叠、重复别名合并。不跨池累计或平均；池内值不一致时取最低已知值并显示范围，未知值不显示为 0%。
打开看板读取本机缓存，点击刷新才查询上游。渠道与聊天列表默认折叠第三池，仍可展开选择所有已启用型号。

## OpenAI 兼容接口

`POST /v1/chat/completions`、`GET /v1/models`，支持 JSON 和 SSE。`/docs` 提供交互文档。

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_GATEWAY_KEY' \
  -H 'Content-Type: application/json' \
  --data-binary '{"model":"demo-chat","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

Windows 使用 `curl.exe`；Antigravity 的 model 请从 `/v1/models` 读取真实别名。
原生驱动支持文本、基础工具历史/签名及内联 base64 图片转换；具体模型参数受上游限制。无效或不支持的输入返回 400。

## 验证与边界

```powershell
cd gateway
.venv/Scripts/python.exe -m unittest discover -s tests -v
node tests/quota-models.test.mjs
.venv/Scripts/python.exe tests/smoke_http.py
```

103 项 Python 测试（含 12 项子测试）、配额分组测试和本机 HTTP/SSE/curl 冒烟测试已通过。
其中 14 项起真实 Node 子进程、对 12 家厂商逐一取登录地址，断言落在厂商域名且不是导入页。
Google HTTP 在测试中被模拟；不据此声称真实模型生成、完整工具/多模态兼容性或长期稳定性已验证。
当前单进程一个 worker，健康状态在进程内。多主机使用独立账号池；相同账号跨实例共享需要另加 Redis 等租约和全局并发状态。
反代须关闭 SSE 缓冲和 POST 自动重试。

协议与许可来源见 [NOTICE.md](NOTICE.md)。

仓库原演示项目回归为 78/79；唯一失败项是本机已安装插件升级后与原来源快照的 client.js 哈希不同。原功能与旧快照保留，详细结果见根目录 CHANGELOG。
