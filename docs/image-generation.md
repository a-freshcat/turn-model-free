# 原生图像生成（v1.2.12）

这是独立的 OpenAI Images 兼容接口，不限定 DSH。登录已有 Antigravity 账号即可调用其已配置且启用的图像型号，无需额外的 OpenAI 或 DeepSeek Key。仍然需要本网关的 API Key，并受账号实际权限、配额及上游可用性限制。

## 调用

`POST /v1/images/generations`，使用与聊天相同的 `Authorization: Bearer ...`。

```json
{
  "model": "antigravity/gemini-3.1-flash-image",
  "prompt": "蓝色背景上的一根黄色香蕉，扁平插画，无文字",
  "n": 1,
  "size": "auto",
  "response_format": "url"
}
```

将此 JSON 保存为 image-request.json 后，在 PowerShell 中运行（环境变量中填写已有网关 Key）：

```powershell
curl.exe http://127.0.0.1:8199/v1/images/generations `
  -H "Authorization: Bearer $env:GATEWAY_API_KEY" `
  -H "Content-Type: application/json" `
  --data-binary '@image-request.json'
```

接口省略 model 时，从账号中已配置的图像型号选择一个；不会猜测或偷偷使用未配置型号。每次仅支持 `n=1`。`size` 支持 auto、1024x1024、1536x1024、1024x1536，分别作为默认或 1:1、3:2、2:3 比例请求传给上游；实际像素尺寸由上游决定，不保证与 size 字面尺寸相等。

- `response_format=url`：标准 `created`、`data[0].url` 返回结构；链接可直接打开，有效期一小时。链接签名只授予这张图片的下载权限，不含 API Key。转发链接也会授予持有者临时查看权限。
- `response_format=b64_json`：返回 `data[0].b64_json`；调用端自行 Base64 解码保存。此模式不在网关存图。

URL 模式图片存放在网关私有 `.gateway-images/`，后续存图时清理过期文件，最多保留 128 个文件。图片最大 20 MiB，仅接受 PNG、JPEG、WebP，校验编码、MIME 与文件签名。接口不接受任意 output_path，也不返回服务器磁盘路径。多台网关反代使用 URL 模式时需配置会话保持或共享图片存储；使用 b64_json 可避免下载请求跨节点问题。

## 在 DSH 中使用

让技能或 HTTP 工具向上述接口发请求，再保存 Base64 图片或打开返回 URL。仅在普通聊天中说“调用 Banana”，不会自动创建一个生图工具；旧的 GPT/OpenRouter 生图技能也不会自动切换端点。客户端接入时需明确选择本网关 Images API，不能向外部服务发送本网关 Key。

目前提供文字生成图片；不包含图片编辑、遮罩、批量生成或生图 SSE。聊天接口与工具历史处理维持原行为。模型目录出现 Image 名称，不意味着通过 Chat Completions 调用它就能返回图片；请使用 Images API。

## 调度与错误

与聊天共用账号租约、并发上限和保护机制，图像请求也计入全局并发及速率限制；不会绕过禁用、冷却或模型开关。生成使用 stream_timeout_seconds（默认 300 秒）。调用端断开或取消时释放请求与租约。

参数或无图片结果返回清楚的 400，不冷却账号；超时返回 504 并短退避；真实鉴权、限流和风控继续触发原保护，不将所有失败强行当作“与账号无关”。未配置图像型号返回 404。

## 验证

使用现有账号实际调用 gemini-3.1-flash-image，URL 模式生成并下载 JPEG（1408×768），图片通过解码与目视检查；本机 8199 部署的 b64_json 模式也生成了可解码 JPEG（1408×768）。这两次均未使用 OpenAI/DeepSeek 图像接口。

协议字段参考 [Google GenerateContent](https://ai.google.dev/api/generate-content?hl=en) 中 responseModalities、imageConfig 和 inlineData；Antigravity 端点是否支持以真实请求验证为准。
