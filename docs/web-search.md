# 网关搜索 API 与 DSH 独立技能（v1.2.13）

`POST /v1/search` 是本项目的搜索扩展接口，不是 OpenAI 或 DeepSeek Messages 的标准端点。支持此扩展的自建或授权共享网关均可提供服务；本机部署可继续使用 8199 和已有网关 Key，无需额外 DeepSeek Key。

## 独立使用

在 Release 下载 `gateway-web-search-v1.2.13.zip`，解压后将整个 gateway-web-search 文件夹导入 DSH 技能目录（通常为 `~/.dsh/skills/`）。在技能选择器中选择 **gateway-web-search**，或让模型明确加载此技能再搜索。目录内只有标准库 Python 脚本，需要 Python 3.10+，不必部署服务端源码。

配置环境变量 GATEWAY_SEARCH_BASE_URL、GATEWAY_SEARCH_API_KEY，可选 GATEWAY_SEARCH_MODEL；也可以将 config.example.json 复制成同目录的私有 config.json，在本机填入提供方的地址和调用 Key。config.json 不随安装包或源码发布，不能上传仓库或粘贴到聊天。技能安装本身不附送账号、配额或公共服务。

```text
GATEWAY_SEARCH_BASE_URL=http://127.0.0.1:8199/v1
GATEWAY_SEARCH_API_KEY=本机或授权提供方给出的调用Key
```

远程服务使用 HTTPS。使用共享服务前，提供方仍需隔离管理 API、分发独立调用权限，详见 [共享网关](standalone-skills-and-sharing.md)。

验证安装或独立运行：

```text
python <技能目录>/scripts/search.py --query "Python 官网最新版本" --max-results 5
```

脚本输出包含 content、sources、queries、truncated 的 JSON。技能会要求模型阅读结果并附上真实来源链接。来源 URL 有时是 Google 的 grounding 跳转链接，这是上游实际返回的来源链接，不是凭空生成的引用。

## 接口

使用与聊天一致的网关 Bearer 鉴权：

```json
{"query":"Python 官网最新版本","max_results":5}
```

可选 model 是已配置的本地 Gemini 模型别名；兼容旧桥接插件的 maxResults 字段。默认优先已配置的 gemini-2.5-flash，再选择可用的 Gemini 文字型号，排除 Image 和 Argon。不会发送未配置模型名或混合自定义函数工具。响应只有上游真实 groundingChunks 才算来源；仅有模型摘要但没有来源时返回 502 / search_ungrounded，不冒充成功搜索。

请求与聊天共用号池轮询、账号并发租约、保护、速率及取消清理。参数错误和缺失来源不冷却账号；网络或超时短退避，真正的鉴权、限流、风控仍遵守保护。故障不写入用户对话历史，但真实账号故障可能暂时影响使用同一账号的聊天，不承诺“所有搜索失败均与账号无关”。

## 旧模式

安装此技能不会自动覆盖 DSH 系统内置 web_search 提供方，不会改写 DeepSeek 的 Messages API 地址，也不会更换聊天模型。选用 gateway-web-search 时调用独立搜索接口，避免继续触发旧提供方的 DeepSeek Key 401。旧会话可以继续使用；若当前请求已执行，新增技能只能供之后的请求调用。

本机的旧桥接插件仍可通过相同 /v1/search 地址使用新后端；独立技能则不要求安装该桥接插件。生图继续选择 gateway-imagegen，两个技能分别下载与配置。

## 已验证

实际独立技能客户端通过现有 Antigravity 账号搜索 Python 官网信息，返回成功摘要、实际检索词和 3 条来源。服务端、私有配置客户端、取消、超时及错误分类均有离线回归。

协议依据：[Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)。Antigravity 的实际支持以本次真实请求为准。
