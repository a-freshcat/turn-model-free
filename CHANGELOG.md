# 版本变更

## v1.4.0 — 2026-11-08

### 1. Gemini 官方 API Key 独立池（`gemini_api` 渠道 / `driver="gemini"`）

- 新增本机渠道 `gemini_api`：凭据是 Google Generative Language 的官方 API Key（`AIza…`），由本进程直接出站，不经过插件的 channel-pack，也不依赖浏览器登录态。它与插件里的 `gemini`（网页 Cookie 反向代理）是两条完全独立的链路，凭据互不通用。
- 新增 `gateway/gemini_driver.py`：请求体构造、`functionDeclarations`、`thoughtSignature` 校验、候选帧解析全部复用 `antigravity_driver.py` 的协议内核（Gemini 线格式是同一套），只替换三处 —— 端点 `/v1beta/models/<id>:generateContent`、鉴权头 `x-goog-api-key`、响应无 `{"response": …}` 信封。没有复制一份新的协议实现，也没有把已经稳定的 Antigravity 传输改成双模式。
- `thoughtSignature` 缓存与 Antigravity 共用同一个实例：签名属于 `functionCall` 本身（名字 + 规范化参数），不绑定凭据。多轮工具调用第二跳不再因为「客户端不回 `extra_content`」而 400。

### 2. 失败分类：只罚请求、不罚账号

- `400` 一律抛 `InvalidRequest`，不进入冷却。详情里只回已知的字段名（`generation_config.max_output_tokens` 这类），上游原文、提示词、密钥都不越过 API 边界。
- `429` 走 20 秒起步的短退避（`gemini_http_429` 进 `TRANSIENT_FAILURES`），不再用整轮 600 秒冷却锁住一把好钥匙 —— 官方免费档是按分钟令牌桶限流，不是账号坏了。Antigravity 的 `antigravity_http_429` 刻意保持整轮冷却，因为那代表按池计的配额已经耗尽。
- `401/403` 与 `API_KEY_INVALID` → `reauth=True` → 账号停用调度，页面显示「需重登」，且响应体里不出现密钥。
- 安全策略整段拦下（`promptFeedback.blockReason`）如实回 `content_filter`，不报「流不完整」让号池换号重打同一段提示词。

### 3. 渠道面与页面

- `web_api.LOCAL_CHANNELS` 集中声明「凭据在本进程」的渠道（`antigravity` + `gemini_api`），替换原先散落的 `!= "antigravity"` 判断 —— 漏改一处就会让本机账号被当成插件账号去调 bridge。
- `channel_capabilities` 新增 `plugin` / `login` 两个字段，前端据此分流，不再靠渠道 id 硬编码。
- 号池页为 API Key 账号单独渲染：模型数、按上游限流（无可读额度）、凭据长期有效不续期，不再是三个「未知」配额条；驱动标签写「API Key」。
- 渠道页新增「Gemini API Key」卡片：点「＋ 添加账号」打开本机导入窗口，只需填 API Key（账号 ID 自动生成，可选接口基址）；保存前验证密钥并自动生成模型映射，不走厂商登录。

### 4. 模型清单按「真能用」过滤，不按接口签名猜

- 官方 `/v1beta/models` 里有 47 个声明支持 `generateContent` 的模型，但实测同一把免费 Key 只有 7～9 个真能对话：旧版本是 `404 no longer available to new users`，pro / image / omni 是免费额度 429，tts / lyria / transcribe 只要非文本输出（400）。按方法签名过滤等于把 38 个必然失败的模型列给用户选。
- 保存与「刷新」都改走 `usable_models()`：目录 → 按可用概率排序（flash-lite → 新版本 flash → latest → gemma → pro 最后）→ 逐个发一次 8 token 的最小请求实测。非对话族（tts / image / lyria / embedding / transcribe / robotics / computer-use / deep-research）直接不进候选。
- 探测有 60 秒预算，到点带着已确认可用的模型返回，不让页面空转。
- `2xx` 之外的结果分开处理：`404` 是「这个模型对你不存在」（抛 `InvalidRequest`，不罚账号 —— 以前它被判成账号故障，直接进 600 秒冷却）；`401/403` 是钥匙作废（停用账号）；`429` 再细分。
- `429` 拆成两种：**每分钟**超限是暂时限流，模型照常可用；**每天**超限（免费档每模型每天 20 次，`QuotaFailure.quotaId` 含 `PerDay`）则该模型当天报废、十几小时后恢复。后者写进 `disabled_models` 并在页面上标为「今日额度用完」，而不是从列表里悄悄消失 —— 用户需要知道「明天还能用」而不是「这个模型被删了」。
- 单号探活的模型不再写死 `gemini-2.5-flash`（该 id 已对新用户下线，第一次探活就吃 404），改为按「便宜通用族」的正则匹配账号实际拥有的模型。

## v1.3.3 — 2026-11-07

### 1. 彻底消除后台控制台黑框与启动器自检

- 修复 `bridge_client.py` 启动 Node.js 子进程（`pack_bridge.mjs`）未设 `CREATE_NO_WINDOW` 导致托盘/后台残留 `conhost.exe` 黑窗口的缺陷；`launch_web.py` 内部 `netstat`/`tasklist`/`taskkill`/`icacls` 等调用统一添加隐藏窗口标记。
- 启动器 `TurnModelFreeLauncher.cs` 启动前先模拟 `sys.path`（含 `.vendor/fastapi`）自检 `uvicorn` / `webview` 依赖，依赖缺失时直接弹窗提供具体诊断与安装命令，不再让子进程无报错静默退出；仅贴出本次启动的新日志并捕获 stderr。
- `scripts/install-shortcut.ps1` 支持 `-Exe` 指向已安装启动器，固定 UTF-8 BOM 避免 PowerShell 执行乱码。

### 2. 控制台签到恢复与冷热轮询分流

- 补回每日签到状态与「领取今日积分」一键入口：支持签到的渠道展示「今日已签」/「今日未签」和每号领取按钮，顶部常驻一键签到按钮。
- 控制台冷热轮询分流（运行态 15s、账号/模型/积分 5min、后台自动休眠），解决原频繁全量轮询放大上游 429 与消耗配额的问题。

### 3. 单号探活穿透与失效令牌自愈

- 修复账号探活失败因只读 `error` 导致关键结论退化为「探活失败：未知原因」的缺陷：完整透传插件 `message`、`raw` 及测试模型与账号，并明确标注单号探活不代表同渠道其它账号失效。
- 华为 CodeArts 等渠道一次性轮换 `refresh_token` 过期/作废（`STS5.1806`）时，状态栏准确显示红色「• 需重登」（不再误标「正常」）；手动点击刷新遇到 `STS5.1806` 时自动调用 `account.update { enabled: false }` 将死账号停用，防止死账号排在队列首位阻塞同渠道其他健康账号的正常调度与按需续期。
- 修复刷新失败报错中重复的「刷新失败：刷新失败：」前缀。


### 1. 免控制台的 EXE 启动器

- 新增 `launcher/TurnModelFreeLauncher.cs`：用 `csc /target:winexe` 编译的薄壳，
  静默拉起 `pythonw.exe gateway\launch_desktop.py`，**没有任何黑窗口**；
  子进程退出码原样返回，失败时弹一个 MessageBox 并指路 `launch-desktop.cmd`。
- `scripts/build-launcher.ps1` 负责现编（系统自带 .NET Framework 编译器，不需要 SDK）；
  `scripts/make-icon.py` 画出原创多尺寸图标（纸感底 + 靛红圆角 + 白色 `t`），
  不再借用第三方品牌图标；`scripts/install-shortcut.ps1` 建桌面/开始菜单快捷方式。
- 发布包根目录直接带 `TurnModelFree.exe`（`prepare_release.py` 打包时现编，编不出来
  则退回只带 `.cmd`，不影响出包）。`.cmd` 两个都保留作排障入口。
- 控制台补上 `#bootstrap=<一次性令牌>` 静默换会话：以前双击 exe 开出来仍要手贴 Key，
  现在开窗即用；令牌换完立刻从地址栏抹掉，且只能用一次。

### 2. 控制台轮询降频（缺陷修复）

- 旧实现每 15 秒把**所有**渠道的账号与积分查一遍：6 个渠道有账号时约 14 次调用 / 15 秒
  ≈ 56 次/分，其中 `credits.balances` 对每个账号都是一次真实上游请求。这既烧配额，
  也是上游 429 的放大器（Raccoon 尤其明显）。
- 改为冷热分离：热数据（在线、并发、冷却、保护状态、倒计时）15 秒一轮、全走本机接口；
  冷数据（账号清单、渠道统计、模型目录）5 分钟或手动刷新；积分 5 分钟一次，并可切成
  「仅手动」。稳定状态每轮上游调用从 14 次降到 0 次（实测日志：每轮只剩
  `/ui/quota`、`/ui/state`、`/ui/protection`）。
- `document.hidden` 时停表，回到前台补一轮；页面底部写明各项读数时刻与下一轮间隔。

### 3. Raccoon 冷却补丁的隔离加载路径

- 新增 `patch-raccoon-cooldown.mjs --isolated-dir DIR`：**整目录复制**（含同级
  `qoder-auth-wasm.wasm`）后再打补丁，解决 v1.3.1 里「只复制 pack.js 导致 Qoder
  `ENOENT`」的根因；同时把行为验证脚本改成可以对着副本路径跑。
- 该路径**默认不参与运行**：桥仍旧只加载插件目录里的原件，补丁副本必须通过
  `DSH_PACK_PATCHED` 显式指定，供隔离实验使用。

### 4. 顺带修掉的显示问题

- 号池标题下那行说明不再永远停在「读取中…」，改为写清账号数、在线数与读数时刻。
- 积分行多一行「读数 HH:MM:SS」，让「这是 5 分钟前的数」变得可见。
- 控制台新增「桌面启动」页：三种启动方式的区别、端口冲突怎么处理、日志与密钥在哪。

## v1.3.1 — 2026-11-07

- 使用淡粉纸感原创控制台替换旧渠道卡界面；号池同时展示本机 Antigravity 和插件账号，状态、额度与模型数取真实接口，三池显示重置倒计时。
- 修复本地化重置时间误解析、插件账号遗漏、凭据状态未知时误删账号，并从渠道入口移除已下线的 TRAE 与 Loomy。
- DSH 桌面端 8326 网关未运行时自动在 18326 拉起独立备用网关，修复插件渠道对话的 503 ConnectError；恢复对话过程中的调用保护链。
- Raccoon 冷却到期后重新选中且收到 429 时，退避档位降一级；仍处于冷却期间的并发 429 保留原有递增机制。补丁由桥在启动时根据已安装的插件包生成，不修改插件原件。
- 桌面端启动脚本显示网关 Key 与地址；诊断日志就绪提示改为普通信息，不再出现误导性的 WARNING 前缀；修复根目录启动脚本路径。

## v1.3.0 — 2026-11-06

### 1. 独立桌面客户端支持（pywebview / WebView2）

- 新增 `launch_desktop.py` 与 `launch-desktop.cmd`（支持 `pythonw.exe` 静默免黑窗口启动）；
- 启动时自动接管并安全释放滞留的旧网关实例，杜绝 Windows 端口冲突报错；
- 窗口关闭与底层服务联动清理，彻底消除后台孤儿子进程。

### 2. 配额看板与渠道管理 SPA 单页秒开切换 & Antigravity 探活修复

- 顶部 Header 新增「渠道与账号管理」与「配额看板 · 三个池」统一导航切换卡；
- 彻底解决配额看板弹外部浏览器导致 401 报错及数据加载失败（共 0 个账号）的问题；
- 配额看板采用两阶段渐进式渲染：第一阶段毫秒级秒开本机 Antigravity 账号及三个配额池，第二阶段后台异步补齐外部插件渠道与积分；
- 修复 Antigravity 账号点击「测试」按钮时默认选用内部补全端点（`chat_20706`）导致 500 报错的问题，改为优先匹配标准轻量对话模型（`gemini-2.5-flash` 等）；
- 锁定右侧侧边栏双列栅格布局（防止屏幕缩放或宽度不足时折叠掉落至屏幕底部），并移除多余的「接口测试」表单模块。

### 3. 仓库更名与双端独立分包发布

- 仓库与包体统一更名为 `turn-model-free`；
- `turn-model-free-web-v1.3.0.zip`：纯净轻量 Web 网关版；
- `turn-model-free-desktop-v1.3.0.zip`：包含开箱即用桌面客户端及 WebView2 运行依赖环境。

## v1.2.15 — 2026-11-06

### 1. 附件服务：修复 18326 桥拉起网关时图片输入 400

- **问题现象**：调用 OpenAI 接口发送图片数据时，网关返回 400「当前 profile 未装载附件服务（`@deepseek-ai/dsh-attachment-local`）」。
- **原因分析**：`pack.js` 解析 `image_url` 时需通过 `ctx.get("attachments")` 获取附件存储服务；桌面端主网关默认装载了该服务，而子进程桥独立上下文未挂载该 isolate store 服务。
- **修复措施**：在 `gateway/scripts/pack_bridge.mjs` 中通过 `ctx.plugin()` 将 `@deepseek-ai/dsh-attachment-local` 注册进主 context，并在桥初始化握手返回中增加 `attachments` 状态上报；配套增加 `test_bridge_mounts_the_attachment_store` 单元测试。

### 2. Antigravity：彻底修复工具调用回显污染与签名中断

- **废除正文降级**：移除了将无签名调用降级为 `Historical assistant tool call (already executed): ...` 文本标记的逻辑，已结束的历史轮次省略签名直接原生回放；
- **签名缓存解耦与持久化**：`ToolSignatureCache` 键去除账号和模型绑定，允许跨账号轮转与跨模型复用，且支持持久化到磁盘 `.gateway-auth/tool-signatures.json`，避免进程重启后丢失签名；
- **历史污染清除**：使用 `json.JSONDecoder().raw_decode` 及平衡括号状态机彻底剥离旧会话中遗留的多层嵌套、含花括号脚本等历史标记，解决长会话复述回显格式的问题；
- **精确借用保护**：严格限制 `signature_borrow` 仅在未封闭当前轮次的首个调用上生效，防止污染已结束历史轮及并行调用的原生结构。

## v1.2.14 — 2026-11-06

### 模型过滤：`/v1/models` 按渠道白名单过滤未支持的上游渠道

- **问题现象**：上游 `dsh-our-free-model` 网关探活返回了未在白名单中的匿名渠道模型（如 `opencode/deepseek-v4.1-flash-free`），导致客户端能在 `/v1/models` 里看到该模型，但发起对话时因 `CHANNELS` 白名单校验失败被 404 拦截。
- **修复措施**：在 `gateway/main.py` 的 `/v1/models` 接口中增加白名单校验，过滤掉不在 `CHANNELS` 范围内的模型，确保导出的模型清单与实际支持的对话路由严格对齐。

## v1.2.13 — 2026-11-06

- 新增独立 gateway-web-search 技能与标准库调用脚本，支持私有本机配置、自建或授权共享网关，无需额外 DeepSeek Key。
- 新增 /v1/search 扩展接口，真实 Google grounding 来源、摘要和检索词返回；无来源时明确报错，不伪装联网成功。
- 搜索与聊天/生图共用账号并发租约、保护和退避，支持取消；参数错误与缺失来源不冷却健康账号。
- 生图技能增加私有 config.json 支持，两个技能独立下载。README 补充 DSH 安装、旧模式切换及共享管理权限边界。
- 真机验收：独立技能客户端使用现有账号，返回摘要、实际检索词和 3 条真实来源。

## v1.2.12 — 2026-11-06

- 新增可单独下载的 gateway-imagegen 技能（纯标准库），支持接入自建或经授权共享的网关；补充安装与共享管理边界。

- 接入 Antigravity 原生图像生成，新增 OpenAI Images 兼容 POST /v1/images/generations。
- 支持 URL、b64_json 返回和比例配置；URL 从请求地址生成，使用一小时签名下载链接。
- 生图与聊天共用账号轮询、并发租约、保护和限流；支持取消清理、生成超时及失败分类。
- 私有、有界图片存储；不允许任意路径写入，不返回服务器磁盘路径，安装包不包含账号和生成图片。
- 真机验收：现有 gemini-3.1-flash-image 账号在 URL 与 Base64 两种模式均生成可解码的 1408×768 JPEG，无需额外 OpenAI/DeepSeek Key。
- 新增生图说明和 curl 示例。客户端需要使用 Images API；本版未实现图片编辑、批量生成或生图 SSE。

## v1.2.11 — 2026-11-06

- 流式首帧默认等待 120 秒；非流式完整回答使用 stream_timeout_seconds（默认 300 秒）。
- 生成超时保留真实原因，返回 504 / upstream_timeout；不再被下一次取号覆盖成号池 503，保留短退避与 Retry-After。
- 原生请求读取由路由时限管理，保留连接、写入及连接池时限。
- 未适配的图像/代码执行输出不冷却账号；SSE 已开始时发送错误事件结束，不伪造成功 DONE。
- 本版尚未接入原生生图和 Images API。图片输入、Image/Banana 模型名称及模型自述不代表生图可用。

## v1.2.10 — 2026-11-06

- 修复 DSH 工具 Schema 元字段直接透传导致 Gemini INVALID_ARGUMENT 的问题；仅转换 Schema，不修改工具实参/结果。
- 修复工具执行后客户端丢失签名造成后续请求失败：按账号/模型/调用/实参缓存真实签名，未能恢复的旧历史保留为明确标注的历史文本，不伪造签名。
- 参数校验 HTTP 400 直接返回输入错误，不再让健康账号冷却后变成 503；真正限制代码继续保留保护。
- 真实本机验证 gemini-3.8-flash-high：带合成 imagegen 加载历史的 JSON/SSE 均 200，SSE 正常 DONE，失败计数 0。没有生成图片或执行实际工具。

## v1.2.9 — 2026-11-06

- 修复普通网络抖动、超时、临时 500/502/503/504 也被统一冷却 600 秒的问题：默认退避 20、40、80、最多 120 秒，完整成功后重置；五分钟无新网络错误也重新计数。
- 普通网络/临时服务故障不再累计成账号风险暂停，页面单独显示网络异常读数；明确拒绝、限流和渠道校验保持保护策略。
- OAuth 令牌刷新网络失败不再冒充授权失效；临时上游错误即使返回非 JSON 也保留正确分类与 Retry-After。
- 增加 network_cooldown_seconds / network_cooldown_max_seconds 配置；更长的上游 Retry-After 继续遵守。
- 保留安全错误的重新授权标记，成功完整响应后清零连续网络退避。

验证：154 项离线 Python 测试通过，9 项真实登录/插件依赖用例未运行；两组 JavaScript 及 HTTP/SSE/curl 冒烟测试通过。

## v1.2.8 — 2026-11-06

- 统一识别 Antigravity 原生驱动的 antigravity_http_401/403/429 分类，确保立即暂停策略与兼容驱动一致。
- 账号监控新增最近失败分类和时间，便于区分冷却后的 503 与真正的首次失败，不保存上游原文或凭据。
- v1.2.7 的 README、误删修复、配额冷却显示与调用保护均包含在本版本。

验证：原生状态码映射与保护回归通过；本次没有调用真实模型或修改本机正在运行的服务。

## v1.2.7 — 2026-11-06

- 修复凭据查询失败或漏返回状态时，真实账号可能被当作空壳自动删除的问题；未知状态保留账号。
- 持久凭据库不可用时停止插件桥接初始化，避免将内存回退结果误当成真实凭据状态。
- 修复插件冷却秒数与限流模型在看板合并时丢失。
- 重写首页 README，以当前功能、安装与使用为主，底部保留简短借鉴及 NOTICE 链接。
- 新增本机调用保护：默认 2 个并发、每分钟 20 次模型调度；明确拒绝/限流/渠道校验立即暂停，一分钟累计 3 次其他上游故障也暂停。
- 暂停后不再切换账号；尽可能取消本服务其他在途请求；暂停跨重启保留，检查后手动恢复。
- 渠道页和配额页增加提醒与「复制诊断给 AI」，仅复制固定分类/时间/状态，不包含账号、消息或令牌，不自动发送给第三方。
- 保护不涵盖插件内部重试或绕过本服务的请求，不能保证在上游识别前预知或中止。

验证：145 项离线 Python 测试通过，9 项真实登录/插件依赖用例未执行；两组 JavaScript 测试和本机 HTTP/SSE/curl 冒烟测试通过。

## v1.2.6 — 2026-11-06

两个报障，一个在文案、一个在看板，都不影响转发本身。

### 测试失败：那句「全部账号均被…已逐个换号重试」是假的

报障原文：

```
测试失败：无法确认：buddy: 全部账号均被服务端安全策略拦截（HTTP 403，已逐个换号重试）。
该拦截按账号生效（同一请求在其他账号可正常返回），请在 Channel Pack 停用或更换被拦账号：
账号存在异常行为，已被限制使用 request illegal
```

这句话里至少有三处在骗人，而用户拿到的可用信息是零。

**一、「已逐个换号重试」「在其他账号可正常返回」是不存在的。** 这个 buddy 渠道
**只有 1 个账号**，一次换号都没发生。pack.js 在 401/403 的收尾分支里把 `rotated`
写死成 `true`（`contentRejectionError(productId, status, body, true)`），于是
单账号渠道也照念「已逐个换号重试」。它还会顺手建议「停用或更换被拦账号」——
没有第二个号可换，这句等于让人去点一个不存在的按钮。

**二、11140 和 11128 被糊成同一句话。** 上游两种完全不同的裁定共用
`msg: "request illegal"`：

| code | HTTP | 含义 | 能重试吗 |
|---|---|---|---|
| 11140 | 403 | `账号存在异常行为，已被限制使用`（账号级封禁） | 不能，`retry_policy.retry=false` |
| 11128 | 400 | `请求被拦截，请重新发送`（渠道校验） | 能 |

只看 `request illegal` 分不出来，必须认 `displayMsg.zh`。原来的文案把封禁说成
「安全策略拦截」，把渠道校验也说成「安全策略拦截」，用户没法判断该等、该重试、
还是该换号。

**三、没说是哪个账号。** 页面在这一步只拿到一句整渠道的话。

现在网关转发插件结果之前先翻译（`web_api.explain_plugin_probe_failure`）：

```
账号 153****9148（buddy-7a118532）被服务端限制使用：账号存在异常行为，已被限制使用。
（HTTP 403 · 11140 · 上游 retry_policy.retry=false）
本渠道只有这 1 个账号，没有可换的号：重试、续期、换模型都不会放行，
要么等上游解封，要么再登录一个同渠道账号，要么用这个号到客户端提交反馈申诉。
```

- 认得出账号级封禁 / 渠道校验 / 凭据失效三类，各自给出对应动作；
- 说清是哪个账号（昵称 + id）；
- 「有没有可换的号」按渠道**实际账号数**算，不再照抄插件那句假设；
- 原始文案留在 `raw` 字段里，便于对照日志。

顺带核实了「该拦截按账号生效」这句：同一台机器上 5 个 buddy 凭据逐个探活，
2 个 HTTP 200、3 个 HTTP 403 `11140`。是账号级，不是 IP 级，也不是渠道级。

### 看板：WorkBuddy 国内版 / 国际版 装反了

用户报障：「在配额界面中 codebuddy 会被显示为国际版，workbuddy 反倒是变成了
国内版」。看板原来的表就是反的：

```js
workbuddy: 'WorkBuddy 国内版',   // ← 实际是国际版
buddy:     'CodeBuddy 国际版',   // ← 实际是国内版
```

以插件自己的注册表为准（`web/channel-runtime.js` 的 CHANNELS 表）：
`buddy` 走 `copilot.tencent.com`（CodeBuddy 同一端点，国内版账号），
`workbuddy` 走 `workbuddy.ai`（国际版，独立账号与积分体系）。现在两边一致。
中文名只差一个字，所以域名放进 `title`：悬停能看到端点与渠道 id。

### 看板：被限制的账号不再显示「正常」

pack.js 在真实对话被 403 + 11140 拒时会写一条 30 分钟的 `modelRateLimits`，
期间这个账号不参与选号。看板原先完全不读这张表——测试失败、对话也发不出去，
账号那一栏却还是「正常」。

现在按「标记还没过期」翻成 `cooldown`：状态列显示「冷却 30 分钟」（超过两分钟
就换成分钟），悬停能看到是哪些模型被标记。过期的标记会被忽略，不会把账号永远
挂在冷却里。

### 顺带对齐：发布包里漏了源码树已有的两块

改这两处时核对「发布包 vs 源码树」，发现两边已经不同步（不是这次改出来的，
是以前某次只改了其中一边）：

- **积分套餐明细的折叠面板**：源码树的 `quota.mjs` 早已有「剩余 X% ＋ 套餐明细
  折叠／箭头／逐条 `剩余 / 总量`」这套渲染，发布包里只有平铺的几行；配套的
  `.pkg-*` 样式也只存在于源码树的 `quota.css`。现在两边一起发了。
- **渠道清单**：源码树的 `scripts/build_ui.py` 会把 Loomy / ZCode 两张卡片从
  渠道页里删掉（那里没有免费额度，登录方式也不同），发布包保留它们。两边各自
  自洽（`build_ui.py` 会断言卡片存在才删），本次不动这个取舍，只在这里记一笔。

### 端口

默认仍是 **8199**（8000 是本机另一个 Web 插件面板的）。插件自己的 OpenAI 兼容
网关仍是 **8326**。

## v1.2.5 — 2026-11-05

v1.2.4 改端口时在 `.cmd` 里写了中文注释，反而制造了一个新问题。

### `.cmd` 脚本被自己的注释搞坏

报障原文：

```
'屾挒涓婂幓鍙細寰楀埌涓€鍙?rem' 不是内部或外部命令，也不是可运行的程序
或批处理文件。
```

原因是 `cmd.exe` **按系统 OEM 代码页读取批处理文件**，中文 Windows 上是
936/GBK。v1.2.4 里那些 UTF-8 编码的中文 `rem` 注释，被当成 GBK 解码后变成
乱码，还会把行拆断、与下一行粘起来当作命令执行。

更麻烦的是脚本**仍然往下跑**，最终照常打印密钥、然后才是真正的失败原因，
于是屏幕上「乱码命令报错」和「端口错误」混在一起，看起来像脚本本身坏了。

现在两处 `.cmd` 都是**纯 ASCII（0 个非 ASCII 字节）**，中文说明移到本文与
`gateway/README.md`。测试用「读字节、断言全部 ≤ 127」锁住，而不是断言「文件里
没有中文」——后者会漏掉全角空格、中文标点之类。

### 端口冲突：说清是哪条端口、谁占着

原来的报错是 `Cannot start the loopback listener (OS error 10048); try another --port.`。
10048 是 `WSAEADDRINUSE`，但用户从这句话里看不出是什么在占端口。

而且顺序错了：`serve()` 里 bind 失败，意味着**先打印了密钥、再报错**——

```
Mode: configured upstream accounts
Gateway API key (...):
beuIS-c9CPgn2Hiwtp1N9r9yH4nqIHjFaeVZxegfvbg
Keep this window open. Press Ctrl+C to stop the gateway.
Cannot start the loopback listener (OS error 10048); try another --port.
```

前四行看起来像启动成功了。现在改成：

1. **启动前先探端口**（`_port_available`），被占就直接退出，一行「看起来成功」
   的输出都不会有；
2. 报错点名端口与占用者：

   ```
   端口 8199 已被占用，无法启动。占用者：PID 1234（python.exe）。
   常见原因：上一次启动的网关窗口还在运行，关掉那个窗口再试。
   或者换一个端口：launch-web.cmd --port 8200
   ```

3. `PortInUse` 单独一类异常，退出码 1；其余 `OSError`（权限、地址不可用）仍走
   通用分支，因为「换个端口」对它们不适用。

### 顺带说明

v1.2.4 说是「端口冲突」，其实那次 8199 上占着的是我自己测试时遗留的网关进程。
端口号本身是对的，问题在于冲突时的提示质量——用户没法从一句 `10048` 判断
该关掉什么。

### 测试

`LauncherPortTests` 增加 2 项：
- `test_cmd_files_are_pure_ascii`：两个 `.cmd` 的每个字节都 ≤ 127；
- `test_port_in_use_names_the_port_and_the_owner`：真的 bind 一个端口，再断言文案
  里有端口号、有 `PID`、有换端口的命令，且探端口发生在打印 `Mode:` 之前。

源目录 `130 passed, 1 skipped`，`gateway/` 镜像 `131 passed, 0 skipped`。

## v1.2.4 — 2026-11-05

换掉本机监听地址。默认端口从 **8000** 改为 **8199**。

### 为什么换

8000 是本机另一个 Web 插件面板的地址，两边撞在一起时很难分辨到底是谁起失败：
`launch_web.py` 的 `serve()` 先自己 bind socket，占不到就打印一句
`Cannot start the loopback listener (OS error …); try another --port.` 然后退出。
而那个 8000 上跑着的其实是别的服务——页面打得开，只是不是这个面板。

### 改了什么

- `launch_web.py`：新增 `DEFAULT_PORT = 8199`，`--port` 的默认值改为引用它。
  帮助文本也跟着显示 `(default: 8199)`。
- `launch-web.cmd`：把地址显式写成 `set "GATEWAY_PORT=8199"` 并传给
  `launch_web.py --port %GATEWAY_PORT%`。**地址写在命令里，看一眼就知道面板在哪**，
  不再埋在工作目录某处的默认值里。换端口仍然是一条 `--port 8300`。
- `run.ps1`：`$Port` 默认值同步为 8199。
- 根目录 `launch-web.cmd`（双击入口）补注释说明地址在哪，仍然只做转发，
  不自己再写一份端口——两份端口各自漂移是这类脚本最容易出的问题。
- README 里所有 `127.0.0.1:8000` 示例同步改为 `8199`，`--port 8001` 改为 `--port 8200`。

### 不受影响的部分

- Antigravity 的 OAuth 回调地址由运行时端口算出
  （`http://localhost:<port>/oauth-callback`），没有写死，改端口后仍能回跳。
- 插件自己的 OpenAI 兼容网关仍是 8326，与本次改动无关。
- 网关密钥、账号表、`.gateway-auth/` 的位置都没变，换端口不需要重新登录。

### 测试

新增 `LauncherPortTests`（4 项），锁住默认端口不是 8000、启动脚本显式传端口、
`run.ps1` 与启动器一致、根目录入口只转发不含第二份启动逻辑。

源目录 `127 passed, 2 skipped`（其中一项是「根目录没有 launch-web.cmd」的形态跳过），
`gateway/` 镜像 `128 passed, 1 skipped`。

## v1.2.3 — 2026-11-05

修掉三个让「账号在、按钮在，却什么都不对」的根因。三者都跟界面无关，都是桥接
层把插件的运行时环境装错了。

### 凭据：桥接进程读的是真凭据库，不是内存 Map

`scripts/pack_bridge.mjs` 里 `ctx.credentials` 原本是个启动即为空的内存 `Map`。
账号凭据（`WORKBUDDY_ACCOUNT_*` 之类）由 DSH 写在 `~/.dsh/.credentials.yaml`
的 `refs` 下，这个 Map 从来没读过那个文件。后果是连锁的：

- `credits.balances` 对每个账号都报「凭据未配置」——积分一条都查不到；
- 对话发不出去（`upstream stream ended before finish`），而账号状态是 active；
- 登录完成后 `ctx.credentials.set` 也写进内存，DSH 那边看不到新账号。

现在挂载宿主自己的 `@deepseek-ai/dsh-credentials-local`，读走文件、写回文件。
它是个 cordis Service，`ctx.plugin()` 会把 `credentials` 注册进 fiber store，
而 pack.js 是 `apply(ctx)` 裸调用的模块，直接读会抛
`cannot get property "credentials" without inject`；所以在**独立的 Context**
里完成初始化，再把成形的服务对象作为普通属性交给主 ctx。装载失败才退回内存
实现，并在 `ready` 行里写明 `memory-fallback`——静默退化正是这个缺陷原来的样子。

实测：workbuddy 130 / 100 credits，raccoon 4,029 / 5,368 积分，都读到了。

### 端口：桥不再抢 DSH 的 openai-gateway

`pack.js` 的 `apply()` 会顺带启动它自带的 OpenAI 兼容网关，默认监听
`127.0.0.1:8326` —— 那正是 DSH 里这个网关的端口。桥是只做登录/查询转发的
子进程，两个进程抢同一端口的结果是**谁先起来谁占着**，另一个 bind 失败后
静默降级。实测现象：8326 被桥占据，`/v1/models` 回空数组、对话回 502，
看起来像插件坏了，实际是一个辅助进程冒名顶替了服务端。

桥不需要那个网关（它只用 `channel-pack` 这组 RPC），现在显式
`DSH_OPENAI_GATEWAY_ENABLED=0` 关掉。需要时用同名环境变量覆盖即可。

### 账号操作：update / refresh / test 不再报「账号不存在」

`account.delete` 早先已经补了「先问插件」，但另外三个动作仍然只看本机
`accounts.json`，而插件账号不在那张表里。于是账号的调度开关、刷新、测试
一按就是「账号不存在」。现在三个动作共用 `_plugin_owner()` 归属判断：
插件账号的停用走 `account.update`，刷新与探活转发给插件自己的实现——
按下「刷新」必须有真实动作，不能是一句成功的空操作。

### 占位账号：判据从「昵称等于 id」改成「凭据是否存在」

`account.create` 会先写一条「昵称等于 id」的占位记录，登录完成后才补凭据。
登录窗口中途丢了（浏览器被关、进程重启）时，那条记录会永久留着，形状与
「正在登录」完全一致，于是页面上多出一个点不动的「等待授权」账号——用户报的
就是「我还没添加账号，怎么残留了以前的数据」。

旧判据是「本次会话的 `bridged` 表里有没有它」，进程一重启就失效（新的表是空的，
旧占位被当成新登录）。现在问桥 `credential.presence`：有凭据的是真账号
（哪怕昵称等于 id），没凭据且没人等的才是空壳，清掉。

### 积分与能力申报

- `credits.balances` / `credits.status` / `credits.claimAll` 转发给插件。
  此前 12 家一律返回「本机网关没有余额接口」，页面上每个插件账号都显示
  「未返回余额数据」——而积分其实查得到。
- `provider.status` 的能力申报改为以 pack.js 的分支清单为准
  （`_PLUGIN_CLAIM` / `_PLUGIN_CREDIT_STATUS`）。声明了但那边没实现的，
  按钮点下去只会得到一句「操作失败」。
- 配额看板的条件判断修正：原来用「`/ui/state` 的账号比看板多」判断是否需要
  查询插件账号，而 `/ui/state` 只含本机 `accounts.json`；只有 1 个 antigravity
  账号时两边都是 1，条件恒为假，看板永远只剩一个账号。改为无条件问
  `provider.status`。
- 配额看板的外部渠道行改为显示**积分包明细**（包名 / 剩余 / 有效期 / 进度条）。
  此前那一列只有一个并发负载条，用户看不到任何积分。
- `quota.mjs` 的资源版本号 +1，避免浏览器缓存住旧的 ES module。

### 测试

`124 passed, 1 skipped`（源目录与 `gateway/` 镜像各自独立跑同一结果）。
新增的回归用例锁住上面每一条：桥不许抢端口、桥必须读真凭据库、
有凭据的占位不能删、重启后旧占位必须清掉、余额必须真的转发给插件。

## v1.2.2 — 2026-11-05

收口 v1.2.1 的两个缺口：**插件里登录过的账号网关上不显示**，以及**界面还是旧版**。

### 账号与模型以插件为准

- 12 家渠道的账号改为**以插件为准**：`account.list` / `provider.status` 合并
  `channel-pack` 的账号表。此前只读本机 `accounts.json`，而 12 家的账号在
  `~/.dsh/channel-pack/state.json` 里，于是用户在插件里登录完成后，网关仍然显示
  「账号 0/0 · 还没有账号」。插件账号排在前面，并标记 `source: "plugin"`。
- 12 家渠道的模型改由**插件自带的 OpenAI 兼容网关**提供
  （`~/.dsh/openai-gateway/api-key` + `127.0.0.1:8326`）。模型 id 与插件一致
  （`codearts/GLM-5.2`），这样页面上看到的模型就是能直接对话的模型。
  插件网关只暴露**当前启用**的模型；某渠道全部模型被关掉时退回插件的
  `model.list`，让用户至少能看到有哪些模型、可以去插件里开启。
- 对话转发：12 家的 `/v1/chat/completions` 直接代理到插件网关，支持 JSON 与
  SSE 流式；`/v1/models` 也合并插件模型。插件没在运行时返回 503 并说明原因，
  不假装成功。
- 插件的账号可以在网关上**删除与停用**：`account.delete` / `account.update`
  先问插件再动本机表。此前一律走本机 `accounts.json`，删一个插件账号会报
  「账号不存在」——账号明明就在页面上。
- 登录未完成的**占位账号**标为「等待授权」并置为不可用；登录窗口过期时
  连同占位记录一起删除。历史遗留的空壳不显示并顺手清掉（实测清掉 407 条）。

### 界面

- 采用新版渠道页样式：侧栏动作按钮改为 `side-action-btn`，卡片两列布局。
- 配额看板重做：账号池分组筛选、账号下拉筛选、汇总胶囊、三个独立配额池的
  用量条与重置时间、容量、状态徽章、调度开关、可展开的型号明细。
- 配额链接补 `rel="noopener"`。

### 其它

- 新增 `gateway/plugin_gateway.py`（插件网关发现与代理）。
- 新增 `gateway/tests/test_plugin_accounts.py`（18 项）。
- 依赖外部状态的用例（要求 DSH 正在运行）改为**跳过**而非失败：插件网关停不停
  取决于 DSH 是否运行，与本仓库无关，算成回归会让每次 DSH 重启都变成一次假红灯。

验证：120 项 Python 测试 + 12 项子测试通过，1 项按条件跳过。真机实测：
buddy / raccoon 直连插件与经网关转发返回一致（`200 你好`），流式 55 帧以
`[DONE]` 收尾；配额看板正常显示三池用量（Argon 1.96%、Claude/GPT 28.27%、
其他 Gemini 0.26%）；账号表从 414 条（其中 407 条空壳）清理为 7 条真实账号。

## v1.2.1 — 2026-11-05

- 12 家渠道的「添加账号」改为**真实厂商登录**：网关把登录委托给已安装的
  `dsh-our-free-model` 插件（Node 子进程桥），不再只返回本地导入页。
  覆盖 codearts / buddy / workbuddy / lobsterai / qoder / qodercn / trae /
  cline / loomy / raccoon / minimax / zcode，登录页落在各厂商域名。
- 新增 `gateway/bridge_client.py` 与 `gateway/scripts/pack_bridge.mjs`：
  行分隔 JSON-RPC 常驻桥。`@deepseek-ai/*` 通过 ESM 解析钩子指向宿主实现，
  **不改动已安装插件的任何文件**。
- 保留导入入口：`account.importWindow` 显式打开原有 `/connect` 导入页，
  已有兼容上游配置不受影响。
- 登录弹窗文案按来源分三档：Google OAuth / 厂商登录 / 导入配置，
  不再对厂商登录显示「填写已有账号与模型配置」。
- 账号级出站代理对**所有驱动**生效（原先只对 Antigravity 生效，其余渠道
  流量可被识别）。新增 `Account.proxy`、`Account.jitter_ms`。
- 修复同一凭据从两个出口出网：令牌刷新、项目探测、模型目录查询此前只认全局
  `GATEWAY_UPSTREAM_PROXY`，与对话请求的账号级代理不一致。
- `GATEWAY_API_KEY` 跨重启保持（落盘 `.gateway-api-key`，ACL 收紧），
  外部客户端不再因重启而 401。
- 渠道能力由后端申报（`provider.status.capabilities`），页面据此决定渲染哪些
  按钮；未接入的积分领取/签到/余额查询给出具体原因，不再返回空成功。
- 测试：103 项 Python 测试 + 12 项子测试通过，含 14 项真实厂商登录测试。

已知限制：MiniMax 的 `oauth2/device/code` 在这条网络上约 1/3 请求会 SSL EOF
或读超时（实测 2.8s–22s 不等），桥内置 5 次重试；仍失败时会在弹窗里报出原因。

## v1.2.0 — 2026-11-05

- 新增 gateway/ 本机 FastAPI 网关，兼容 OpenAI Chat Completions JSON/SSE 与模型列表。
- 新增轮询号池、单号并发限制、冷却和输出前故障切换。
- 接入 Antigravity Google OAuth/PKCE、模型发现、令牌续期和 Windows DPAPI 凭据存储。
- 保留原渠道界面风格，新增账号管理与独立配额页。
- 配额固定分为 Argon、Claude/GPT、其余 Gemini/内部型号三个池，独立百分比和进度条，型号默认折叠。
- 根目录新增 launch-web.cmd；原演示页与腾讯防护层代码继续保留。
- 发布包排除真实账号、凭据、依赖、缓存和含个人信息的截图。

验证：52 项 Python 测试、配额分组测试、本机 HTTP/SSE/curl 冒烟测试通过。
Google 上游使用模拟 HTTP；真实模型生成与长期稳定性尚未完成验证。

原项目回归为 78/79：本机已安装插件的 client.js 已更新，来源哈希与保留的 2026-10-28 快照不一致，导致来源一致性检查失败。其余防护、渲染、行为和演示测试通过；本版本不覆盖该来源基线。

### 5. 补回签到 / 积分领取入口（重写控制台时丢的）

后端 `credits.status` 与 `credits.claimAll` 一直是好的，但控制台重写时整块入口没跟过来：
号池里看不到「今天签没签」，也没有任何领取按钮。现在：

- 每个支持签到的账号行多一行「签到」，写清**今日已签 +N / 今日未签 / 状态未知**三种情况；
- 行末多一个领取按钮。已签的号按钮仍然可点（领取是幂等的，重复点只会回「今天已领取」），
  在途时显示转圈并禁用，避免连点发出两次写请求；
- 顶栏加「领取今日积分」，逐渠道发、单独报结果 —— 一个渠道挂了不拦其它渠道；
- 能不能领以**网关回的能力表**为准，不看前端静态表：Raccoon / WorkBuddy / Cline 后端
  没有签到接口，就不渲染按钮，不制造「点了必然失败」的按钮；
- 回执把插件 summary 的四个数分开说（新领 / 已领过 / 没有活动 / 失败+原因），
  不再合并成一句「操作失败」。
