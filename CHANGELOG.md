# 版本变更

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
