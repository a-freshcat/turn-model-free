# 本机 OpenAI Chat 网关

当前使用入口见[仓库首页](../README.md)。v1.2.8 新增的暂停、限流和 AI 排查提醒见[调用保护说明](docs/protection.md)。

Python 3.10+、FastAPI、Uvicorn、httpx 异步实现。支持离线模拟号池、OpenAI 兼容上游，以及 Antigravity Google OAuth 和原生请求驱动。保留 free-m-clone 原界面风格。

## 1. 来源与接入范围

已检查 [a-freshcat/free-m-clone](https://github.com/a-freshcat/free-m-clone)，参考树版本为
`03729233d039fff44ca34cf1c7cddb355c392099`：

- [README](https://github.com/a-freshcat/free-m-clone/blob/03729233d039fff44ca34cf1c7cddb355c392099/README.md)
  明确说明登录、余额等使用模拟渠道包；仓库没有可直接使用的真实渠道登录和网络驱动。
- [pipeline.mjs](https://github.com/a-freshcat/free-m-clone/blob/03729233d039fff44ca34cf1c7cddb355c392099/src/guard/pipeline.mjs)
  的发送函数由调用者注入。本项目另行实现 Antigravity 驱动；腾讯真实渠道协议仍需独立提供。
- [tencent-gate.js](https://github.com/a-freshcat/free-m-clone/blob/03729233d039fff44ca34cf1c7cddb355c392099/src/guard/tencent-gate.js)
  的内置规则用 Python 重新实现于 `tencent_guard.py`。处理 system/assistant，保留 user/tool、
  工具关联字段和识别到的路径；干净文本保持原样。
- 原仓库未给出可核实的真实请求头/UA、凭据刷新及渠道请求体协议。这些值由部署配置或后续真实驱动提供。
  默认 UA 为本服务自己的 `local-chat-gateway/1`。

当前 `openai` 驱动实际支持**已兼容 OpenAI Chat Completions 的上游**；
`demo` 驱动完全不联网。`antigravity` 驱动使用 Google OAuth、账号模型目录和原生生成接口，复用同一路由和号池。

Antigravity 候选项目、稳定性证据及尚未实测的边界见
[源码与文档对比记录](docs/research/2026-11-05-antigravity-stability.md)。

## 2. 完整项目目录

```text
.
├── account_manager.py        # 轮询、租约、并发上限、冷却、重试
├── upstream_client.py        # send_upstream_request、HTTP、SSE、错误分类
├── main.py                   # FastAPI、鉴权、OpenAI 错误响应、断开清理
├── web_api.py                # 原页面 RPC 适配、账号导入、管理会话与原子保存
├── antigravity_auth.py       # Google PKCE OAuth、续期、模型/配额发现
├── antigravity_driver.py     # 原生生成协议与 OpenAI JSON/SSE 转换
├── antigravity_defaults.json # 公开桌面 OAuth 应用标识，可由环境变量覆盖
├── credential_store.py      # Windows DPAPI 凭据存储
├── config.py                 # 配置与凭据校验
├── tencent_guard.py          # 分档提示词规则与路径保护
├── demo_upstream.py          # 模拟上游与渐进 SSE
├── accounts.example.json     # 真实配置样例
├── accounts.demo.json        # 可直接运行的模拟号池
├── accounts.json             # 部署时创建，Git 忽略
├── requirements.txt          # 可安装依赖范围
├── requirements.lock         # 本次 Windows/Python 3.14 验证版本
├── run.ps1                   # Windows 启动入口
├── launch-web.cmd            # 双击启动并打开原风格渠道页面
├── launch_web.py             # 就绪检测、浏览器打开与本机端口检查
├── web/
│   ├── index.html            # 本地渠道管理页
│   ├── app.mjs               # 原页面与网关的连接层
│   ├── app.css               # 宿主外壳与导入表单样式
│   ├── quota.html            # 独立三个池配额看板
│   ├── quota.mjs             # 账号筛选、刷新、状态条和型号折叠
│   ├── quota-models.mjs      # 三个池分组与同名别名合并
│   ├── quota.css             # 沿用原设计令牌的看板样式
│   ├── channel-runtime.js    # 保留原组件，仅替换 Gemini 渠道元数据
│   └── vendor/free-m-clone/  # 本地参考源码原件与哈希清单
├── scripts/build_ui.py       # 从原件生成运行包
├── examples/
│   ├── chat.json
│   └── stream.json
├── tests/
│   ├── test_gateway.py       # 离线 ASGI/号池/HTTP 驱动集成测试
│   └── smoke_http.py         # 临时启动 Uvicorn，实际 HTTP/SSE/curl 测试
├── task-reports/
│   └── 2026-11-05-local-api-gateway.md
├── NOTICE.md
└── README.md
```

## 3. 本机启动（PowerShell）

也可以直接双击 `launch-web.cmd`：已有 accounts.json 时加载真实配置，否则启动模拟号池，等本机服务就绪后打开
`http://127.0.0.1:8199/`，显示与本地 free-m-clone 相同的渠道卡、图标、折叠列表和昼夜风格。
启动器通过一次性入口自动建立管理会话；直接访问地址时，可输入启动窗口显示的网关密钥。
原件位于 `原 free-m-clone 源码快照目录`，
8 个复用文件按原字节保存在 web/vendor/free-m-clone，哈希清单见其 provenance.json。

点击「WorkBuddy 国内版 → 账号」可以看到两个演示账号，其他渠道按各自配置列出；
演示账号不是已登录的真实厂商账号。右侧可测试同一个流式网关。
Gemini 卡片已替换为 Antigravity，保持原卡片样式。点击「添加」生成 Google 授权链接，
在浏览器完成授权后回到本机回调；后端校验 state、交换令牌并保存账号。
本机管理页会话与 Google 登录独立；页面不会把连接本机误显示成 Google 已登录。
其他渠道的添加按钮可导入已有 OpenAI 兼容上游配置。

未设置 GATEWAY_API_KEY 时每次启动随机生成；关闭窗口/按 Ctrl+C 停止服务。
`/docs` 仍可从侧栏打开，供查看标准 API 文档。

```cmd
launch-web.cmd
launch-web.cmd --port 8200
launch-web.cmd --real
```

`--real` 使用 accounts.json，或环境变量 GATEWAY_ACCOUNTS 指定的真实配置；
尚无真实配置时直接默认启动，在 Antigravity 卡片完成首次授权即可自动保存 accounts.json、切换到真实号池。
端口被占用时会在打印密钥之前就报错，并点名端口和占用者
（`端口 8199 已被占用。占用者：PID 1234（python.exe）`），不会打开其他程序的网页。
最常见的原因是**上一次启动的窗口还开着**——那种情况下端口在听、页面也打得开，
看起来像起成功了，其实是旧进程；网络请求会打到一个已经过期的页面上。

`.cmd` 启动脚本只放 ASCII 字节。`cmd.exe` 按系统 OEM 代码页读取批处理文件，
中文 Windows 上是 936/GBK；UTF-8 写的中文注释会被解码成乱码，进而把 `rem` 行
拆散、与下一行拼起来当成命令执行，屏幕上的
`'…rem' 不是内部或外部命令` 就是这么来的。中文说明放在本文与 CHANGELOG 里。
浏览器无法自动打开时仍可手动访问控制台显示的 URL。`--no-browser` 可仅启动服务。
启动器不会自动安装依赖；当前工作区已有 .vendor，新机器按下方步骤安装。

新机器通常使用虚拟环境：

```powershell
cd path\to\free-m-clone\gateway
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:GATEWAY_API_KEY = "local-development-key"
.\run.ps1 -Demo
```

**当前工作区已在 `.vendor` 安装并验证依赖**，可以跳过安装：

```powershell
$env:GATEWAY_API_KEY = "local-development-key"
.\run.ps1 -Demo
```

`run.ps1` 优先使用现有 `.vendor`，其次使用 `.venv`。停止按 Ctrl+C。
若本机策略禁止执行本地 PowerShell 脚本，直接运行：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) ".vendor")
$env:GATEWAY_ACCOUNTS = (Join-Path (Get-Location) "accounts.demo.json")
$env:GATEWAY_API_KEY = "local-development-key"
python -m uvicorn main:app --host 127.0.0.1 --port 8199 --workers 1
```

虚拟环境中无需 PYTHONPATH，把 python 换成对应环境解释器即可。
示例密钥仅用于本机测试；对外部署时自行设置随机网关密钥。

模拟账号 `demo-blocked` 会返回 11128：非流式返回 HTTP 400；
流式先发角色元数据再发错误事件。网关冷却后切到 `demo-healthy`。
演示 usage 为 0，是占位计数。

## 4. curl 测试

另开 PowerShell 进入项目目录。Windows 使用 `curl.exe`，避免旧版 PowerShell 同名别名：

```powershell
curl.exe --noproxy "*" --fail-with-body --max-time 15 http://127.0.0.1:8199/healthz

curl.exe --noproxy "*" --fail-with-body --max-time 30 http://127.0.0.1:8199/v1/chat/completions -H "Authorization: Bearer local-development-key" -H "Content-Type: application/json" --data-binary "@examples/chat.json"

curl.exe --noproxy "*" --fail-with-body --max-time 30 -N http://127.0.0.1:8199/v1/chat/completions -H "Authorization: Bearer local-development-key" -H "Content-Type: application/json" --data-binary "@examples/stream.json"

curl.exe --noproxy "*" --fail-with-body --max-time 15 http://127.0.0.1:8199/admin/accounts -H "Authorization: Bearer local-development-key"
```

非流式返回 `chat.completion`；流式返回 `text/event-stream`，包含
`chat.completion.chunk`、可选 usage 帧，成功以 `data: [DONE]` 结束。
`/admin/accounts` 只返回状态、失败次数、占用槽位、剩余冷却时间，不公开凭据。

Linux/macOS：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
GATEWAY_API_KEY=local-development-key GATEWAY_ACCOUNTS=accounts.demo.json \
  .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8199 --workers 1

curl --noproxy '*' --fail-with-body --max-time 30 -N \
  http://127.0.0.1:8199/v1/chat/completions \
  -H 'Authorization: Bearer local-development-key' \
  -H 'Content-Type: application/json' \
  --data-binary @examples/stream.json
```

## 5. accounts.json 示例与真实号池

`accounts.example.json` 可以复制为 `accounts.json`。以下是两个账号的结构示例：

```json
{
  "cooldown_seconds": 600,
  "max_retries": 3,
  "pool_wait_seconds": 1,
  "first_output_timeout_seconds": 30,
  "stream_timeout_seconds": 300,
  "accounts": [
    {
      "id": "account-01",
      "driver": "openai",
      "endpoint": "https://provider.example/v1/chat/completions",
      "models": {"local-chat": "provider-model-id"},
      "token_env": "UPSTREAM_PRIMARY_TOKEN",
      "status": "active",
      "fail_count": 0,
      "max_concurrency": 1,
      "tencent_prompt_level": 0
    },
    {
      "id": "account-02",
      "driver": "openai",
      "endpoint": "https://provider.example/v1/chat/completions",
      "models": {"local-chat": "provider-model-id"},
      "cookie_env": "UPSTREAM_SECONDARY_COOKIE",
      "status": "active",
      "fail_count": 0,
      "max_concurrency": 1,
      "device_headers": {"X-Device-Id": "replace-with-your-issued-device-id"},
      "tencent_prompt_level": 0
    }
  ]
}
```

URL、模型 ID、设备 ID 都是占位值，不能直接连接真实服务。
模型映射的 key 是客户端使用的别名，value 是上游模型 ID。每个可承接该别名的账号都应配置映射。

- `token_env` 生成 `Authorization: Bearer ...`，`cookie_env` 生成 `Cookie: ...`。
  也可使用明文 `token` / `cookie`，同一项不能同时配置明文和环境变量。
- `device_headers` 保存账号独立设备参数；`user_agent` 保存真实渠道要求的 UA。
  这些字段不会自动生成或刷新。签名、token 刷新等应在真实驱动内实现。
- 不要把密钥放入 endpoint 查询参数。凭据应留在本机环境变量或私有配置文件。
- 只有确实需要这套提示词规则的渠道才设置 `tencent_prompt_level` 为 1、2、3，其他保持 0。
- 本实现遇到 11128 立即冷却、换号，不在同号上逐档重发；每次尝试从原始消息重建。
- HTTP 客户端按账号隔离 Cookie jar、连接池；关闭重定向和环境代理，保持 TLS 校验。
  endpoint 仅支持 HTTPS 或 loopback HTTP。客户端请求体不能覆盖服务器端地址与凭据。
- `channel` 指定页面分组（如 buddy、workbuddy、qoder、antigravity），默认 buddy；
  `nickname` 是显示名称，`disabled_models` 是关闭的对外模型别名。分组本身不证明该渠道已完成 OAuth。

启动真实号池：

```powershell
Copy-Item -LiteralPath accounts.example.json -Destination accounts.json
# 编辑 accounts.json，替换 URL / 模型映射，再设置凭据。
$env:UPSTREAM_PRIMARY_TOKEN = "<你的上游 token>"
$env:GATEWAY_API_KEY = "<你的网关随机密钥>"
$env:GATEWAY_ACCOUNTS = (Join-Path (Get-Location) "accounts.json")
.\run.ps1
```

文件中的第二个账号默认 disabled，启用前设置对应 cookie；不用时可删除。
两个账号都 active 时，要分别设置凭据。真实测试请求的 model 改为 `local-chat`。
真实渠道不是 OpenAI 协议时，需要先实现下一节驱动，不能仅替换 URL。

## 6. 调度、重试与流式边界

1. 锁内轮询并原子占用账号槽位；网络 I/O 不持锁。租约覆盖整个响应，完成、失败或客户端断开后关闭连接并释放。
2. 非 2xx、网络错误、首个有效输出超时、协议错误和结构化 11128 都进入冷却。
   普通 4xx 也按本任务要求重试，因此错误参数可能冷却多个账号；后续可按上游契约细分参数错误。
3. 默认冷却 600 秒；有效 Retry-After 可延长冷却，最长按 86400 秒处理。
   到期后在下次调度/状态查询时恢复 active，disabled 不自动恢复。
4. `max_retries=3` 表示初次 + 最多 3 次重试，共最多 4 次；需要总共 3 次时设置为 2。
   同一请求不会重复使用已经尝试过的账号。
5. 全部适用账号冷却/disabled，或槽位在有界等待后仍满时返回 **503**；可计算冷却时间时附 Retry-After。
   若仍有健康账号但耗尽重试预算则返回 **502**。未配置模型 404，输入错误 400，网关鉴权错误 401。
6. SSE 角色/空 delta 先暂存，遇到正文、工具调用、refusal 或结束原因才提交 HTTP 200；
   前置错误可以无感切号。输出提交后再发生错误，发送 error 结构的 SSE 帧并结束，不发成功 [DONE]，不重播答案。
   此时 HTTP 状态已经是 200，客户端必须识别错误帧或截断。
7. 支持拆包、UTF-8、BOM、CRLF/CR/LF、多行 data、注释、工具参数增量、usage、[DONE]。
   单个 SSE 事件最大 1 MiB，JSON 响应最大 8 MiB；仅元数据开头有数量与大小上限。
8. 上游标准字段及 JSON 扩展参数透传，保留工具调用关联信息、usage、logprobs 等。
   具体 tools/多模态/音频能力仍由上游决定。本项目实现 Chat Completions 与模型列表，不包含 Responses 等其他 API。

`accounts.json` 是启动配置。真实模式下，页面的账号添加/移除/启停及模型开关通过原子替换保存到该配置，
同时更新当前号池；磁盘写入失败时不更新运行状态。正在处理请求的账号不能移除。
演示模式的修改只在本次进程生效，不改写示例配置。
健康状态仍在进程内，fail_count 为本次运行累计失败次数；重启会丢失运行时冷却记录。
配置里的 cooldown 会重新等待一个完整周期。手工编辑文件仍需要重启。
`/healthz` 只证明进程存活，不代表真实上游可用；池状态可查看 `/admin/accounts`。

管理页面使用 HttpOnly、SameSite=Strict 会话 cookie；写操作校验同源。
UI 状态接口不返回账号 token、cookie 或设备头。兼容 API 仍要求 Bearer 密钥，不接受页面 cookie。
Antigravity 刷新会在需要时续期令牌并查询模型/配额；其他未接入真实驱动的签到和配额仍显示为未接入。

## 7. Antigravity 登录、模型与三个配额池

1. 启动后进入 Antigravity → 添加，在浏览器完成 Google 授权。回调为当前端口的
   `http://localhost:<端口>/oauth-callback`，使用一次性 state 和 PKCE S256，授权窗口有效期 10 分钟。
2. 后端查询账号项目和模型目录；模型来自账号返回的数据，不硬编码上面展示的型号。
   如果授权成功但目录查询失败，保留有效登录并显示说明，稍后点刷新；不捏造模型和配额。
   首次使用、尚无项目的账号需要先在官方客户端完成初始化。
3. accounts.json 只保存 credential_id 引用。用户令牌在 `.gateway-auth/`；Windows 使用当前用户 DPAPI 加密，
   不能直接复制给另一台电脑或 Windows 用户使用。POSIX 回退为私有目录/文件权限，并非加密。
4. 令牌临近到期时按账号加锁续期，支持 refresh token 轮换；授权撤销时停用账号、提示重新登录。
   配额/状态接口不返回令牌，OAuth 回调查询串在访问日志前移除。
5. 如需显式代理，设置 `GATEWAY_UPSTREAM_PROXY=http://127.0.0.1:8080`；默认不读取系统代理环境变量，保持 TLS 校验。
   `ANTIGRAVITY_CLIENT_ID`、`ANTIGRAVITY_CLIENT_SECRET`、`ANTIGRAVITY_USER_AGENT` 可覆盖公开客户端默认配置。

渠道页点击「查看配额 · 三个池」或侧栏「配额看板」进入 `/quota`。按用户确认的规则，每个账号固定分为：

| 配额池 | 包含型号 |
| --- | --- |
| Gemini 4 Argon | Argon 的各档型号 |
| Claude / GPT | Claude 与 GPT 共用 |
| 其他 Gemini / 内部型号 | 其余所有 Gemini 和内部型号共用 |

三个池分别显示百分比/状态条，不跨池相加或平均。池内返回值不一致时使用最低已知值，显示范围；未知值显示「未知」。
型号明细默认折叠；同名、同配额、同重置时间的别名合并展示，仍可展开查看 ID。
渠道模型列表和聊天选项默认折叠第三池，点击「其他 Gemini/内部型号」可显示全部；模型接口仍保留已启用的真实型号。
打开看板读取本机缓存，点「刷新配额」才主动查询上游。

`upstream_client.send_upstream_request` 分派到 `antigravity_driver.send_native`，支持文本、基础工具调用、
内联 base64 图片和 JSON/SSE 转换。多模态、工具签名与各模型参数最终受上游限制；不支持的输入返回 400。
401 在输出前允许一次令牌刷新；其他故障由号池冷却/换号。已输出内容后失败不重播答案。
当前通过模拟 Google HTTP 的协议测试；尚未完成真实 Google 模型生成与长期稳定性测试。

## 8. 测试与多主机部署

当前工作区：

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) ".vendor")
python -m unittest discover -s tests -v
node tests/quota-models.test.mjs
python tests/smoke_http.py
```

虚拟环境中使用对应解释器，不设置 .vendor 的 PYTHONPATH。
第二条测试临时监听 127.0.0.1 随机端口，使用随机测试密钥，测试后自动关闭；
还会调用已安装的 curl。限制本地 socket 的沙箱需要允许本地 socket 才能运行异步事件循环。

单实例使用 **一个 worker**；进程内锁不能对多个 worker 共享号池实施全局并发限制。
每台主机独立运行本项目、分配不同账号，前面挂支持 SSE 的反代即可。
反代应关闭响应缓冲与自动重试 POST，提供足够的上游读取超时，避免重试叠加与答案重复。
默认只绑定 loopback；跨主机时显式配置监听地址，并由反代提供 TLS、请求体大小限制、入口限流。

需要多个实例共享相同账号时，再迁移到 Redis 等共享存储，增加有期限的原子租约、
续租、全局并发计数与持久化。当前没有跨主机共享状态。
真实渠道权限、长期稳定性与实际吞吐尚未验证；当前验证范围为本地模拟上游及协议链路。
