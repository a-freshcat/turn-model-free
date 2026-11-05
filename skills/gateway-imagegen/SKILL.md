---
name: gateway-imagegen
description: Generate images through a user-configured OpenAI Images compatible gateway and save the actual returned image. Use for text-to-image requests when this gateway skill is selected; image editing is not supported.
---

# 网关生图

使用本技能自己的 `scripts/generate.py`，从兼容网关生成并保存图片。技能可独立安装，服务可以由用户自建，也可以由获准共享服务的提供方托管；不要求本机安装网关项目。

连接信息来自 `GATEWAY_BASE_URL` 和 `GATEWAY_API_KEY` 环境变量。缺少时，向用户确认其选择的服务地址与配置方式；凭据应在本机环境或客户端凭据管理中填写，避免粘贴到聊天。使用提供方给出的图像模型名，可通过 `GATEWAY_IMAGE_MODEL` 配置。不能把模型自称的 Banana 能力当作已连通接口。

将用户的画面需求写入当前任务目录的 UTF-8 提示词文件，再执行：

```text
python <本技能目录>/scripts/generate.py --prompt-file <提示词文件> --output <当前任务目录>/generated-image
```

输出文件扩展名由实际图片格式决定。脚本返回保存路径后，确认文件存在且可打开，再把图片或文件链接交给用户。完成标准是收到且保存了真实图片数据，而非模型文字描述或一个生图计划。

请求使用 `/v1/images/generations` 的 `b64_json` 模式，无须在客户端处理临时下载链接。可用 `--model` 指定提供方的图像模型、`--size` 选择受支持的比例请求。完整参数见脚本 `--help`。

遇到 401 检查本网关调用凭据；429/503 遵守提供方的退避时间，避免连续尝试。没有返回图片就报告失败，不改用其他付费服务。服务地址与 Key 仅用于用户选定的网关；本技能不需要读取 accounts.json、Google Cookie 或 OAuth 登录态，也不需要 OpenAI/DeepSeek 的独立 Key。
