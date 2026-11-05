# 版本变更

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
