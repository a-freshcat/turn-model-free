# free-m-clone

本机模型账号管理与 OpenAI 兼容 API 网关。通过网页统一管理账号、查看额度、选择模型和测试流式对话。

**当前版本：v1.2.9** · [下载最新版本](https://github.com/a-freshcat/free-m-clone/releases/latest) · [更新记录](CHANGELOG.md)

## 功能

- **统一 API**：支持 `/v1/models` 和 `/v1/chat/completions`，兼容非流式 JSON 与 SSE 流式输出。
- **账号管理**：添加、启停、刷新、测试账号，查看可用模型与渠道能力。
- **Antigravity 原生接入**：Google OAuth 登录、令牌续期、模型目录和配额查询。
- **插件渠道接入**：通过本机 DSH 插件桥接登录、账号、积分和模型，对话由插件网关转发。
- **配额看板**：按渠道和账号筛选，展示状态、积分套餐、用量条和重置时间。
- **本机账号池**：轮询、单号并发限制、冷却和输出前故障切换；插件渠道的调度由插件负责。

## 环境要求

- Python **3.10+**。
- 使用 DSH 插件渠道时，需要 **Node.js 22+**、已安装的 `dsh-our-free-model` 插件，以及正在运行的插件 OpenAI 网关。
- 仅使用 Antigravity 或导入 OpenAI 兼容上游时，无需启用插件渠道。

## 安装与启动

下载并解压 [Release](https://github.com/a-freshcat/free-m-clone/releases/latest)，或克隆仓库：

```powershell
git clone https://github.com/a-freshcat/free-m-clone.git
cd free-m-clone
python -m venv gateway/.venv
gateway/.venv/Scripts/python.exe -m pip install -r gateway/requirements.txt
.\launch-web.cmd
```

首次安装依赖后，后续直接双击根目录 **`launch-web.cmd`**。启动器会打开管理页：

**http://127.0.0.1:8199/**

保持启动窗口开启，按 `Ctrl+C` 停止。更换端口或仅启动服务：

```cmd
launch-web.cmd --port 8200
launch-web.cmd --no-browser
```

默认加载 `gateway/accounts.json`；尚未配置真实账号时使用演示号池。演示号池不会调用真实模型。

## 添加账号与模型

**Antigravity**：在渠道卡片点击添加，打开 Google 授权页并完成登录。账号模型以实际返回的目录为准；如果目录暂时查询失败，可在登录成功后刷新。首次尚无项目的账号需先在官方客户端完成初始化。

**DSH 插件渠道**：先运行 DSH 并启用插件网关。页面通过插件管理账号与积分，对话默认转发到 `127.0.0.1:8326`；该端口与本项目管理页的 `8199` 独立。

**已有兼容上游**：使用导入入口，按 [accounts.example.json](gateway/accounts.example.json) 配置接口地址、凭据和模型映射。

模型能力、签到和积分领取以渠道实际支持情况为准。连接本机管理页与完成厂商登录是独立状态。

## 三个配额池

Antigravity 的额度按账号分为三个池：

| 配额池 | 包含型号 |
| --- | --- |
| Gemini 4 Argon | Argon 各档型号 |
| Claude / GPT | Claude 与 GPT 共用 |
| 其他 Gemini / 内部型号 | 其余 Gemini 和内部型号共用 |

三个池分别显示用量条和百分比，型号明细可折叠。不跨池相加或平均；池内返回值不一致时，以最低已知剩余量汇总并显示范围。

其他渠道按各自的积分套餐展示，支持查看剩余量、总量和有效期。

## 接入客户端

| 配置项 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:8199/v1` |
| API Key | 启动窗口显示的网关密钥 |
| Model | 从模型列表或 `GET /v1/models` 读取 |

密钥默认保存在 `gateway/.gateway-api-key`，重启后继续使用；也可通过 `GATEWAY_API_KEY` 指定。API 调试页：**http://127.0.0.1:8199/docs**。

在仓库根目录用 PowerShell 测试演示号池：

```powershell
$env:GATEWAY_API_KEY = "<启动窗口显示的密钥>"
curl.exe --noproxy "*" --fail-with-body --max-time 30 -N http://127.0.0.1:8199/v1/chat/completions -H "Authorization: Bearer $env:GATEWAY_API_KEY" -H "Content-Type: application/json" --data-binary "@gateway/examples/stream.json"
```

真实账号请将请求中的 `model` 替换为实际模型 ID。已开始输出的流式请求发生错误后不会重播答案。

## 数据与配置

### 异常保护与 AI 排查提醒

默认最多 **2 个对话并发、每分钟 20 次本服务的模型调度**，每次本机换号尝试也计数。
上游返回 401/403/429、11128 或明确账号限制时暂停后续调用；一分钟内累计 3 次协议解析等非网络故障也会暂停。
该暂停会阻止继续换号重试，并尽可能取消本服务正在转发的其他请求。

页面会显示异常原因、发生时间及「复制诊断给 AI」。诊断仅含固定错误分类、HTTP 状态、时间和调用上限，
不会自动发给 AI，也不会复制账号、对话、Cookie 或令牌。暂停状态跨重启保留，检查原因后点「检查后恢复」。
恢复不会解除上游限制，也不会自动启用已停用账号。

普通网络抖动、超时和临时 500/502/503/504 不作为账号风险信号：默认退避 20 秒，连续失败按 40、80、最多 120 秒递增，完整成功后重置。
更长的上游 `Retry-After` 仍会遵守。可在 accounts.json 设置 `network_cooldown_seconds` 和 `network_cooldown_max_seconds`。

可通过 `GATEWAY_MAX_ACTIVE_CHATS` 和 `GATEWAY_MAX_DISPATCH_PER_MINUTE` 调低上限。
保护仅覆盖经过本服务的模型调用；插件内部的重试和绕过本服务的直接请求无法由这里中断。
上游识别过程不可见，本服务无法保证在平台识别前预知或避免账号限制。

账号配置和登录凭据保存在本机，不包含在源码或发布包中。Antigravity 凭据在 Windows 上使用当前用户 DPAPI 加密，插件渠道继续使用插件自己的凭据库。

默认只监听 `127.0.0.1`。当前账号池状态保存在单个进程内，使用一个 worker；多主机部署应分配独立账号池。

详细配置见 [网关说明](gateway/README.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。需要离线查看界面和防护实验台，可直接打开 `dist/index.html`。

## 借鉴

界面与腾讯 11128 防护层借鉴 `dsh-our-free-model`；Antigravity 协议实现参考 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)。感谢相关项目，来源与许可说明见 [NOTICE](gateway/NOTICE.md)。
