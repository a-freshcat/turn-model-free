# DSH 与 Gemini 工具历史兼容

## 修复的两类问题

OpenAI 客户端的工具 parameters 是 JSON Schema，而 Antigravity 的函数声明并不接受所有元字段。旧驱动直接透传 `$schema`、`default`、`additionalProperties` 等，可能得到 HTTP 400 / INVALID_ARGUMENT。

Gemini 的工具历史还可能带有上游返回的 thoughtSignature。客户端若只保留 OpenAI 标准工具字段，会丢失这部分元数据；从其他模型切换过来的旧对话通常也没有这些签名。把这种历史原样作为原生 functionCall 回放，会在后续工具结果提交时被拒绝。

因此，「第一轮生成了工具调用，工具已经执行，下一轮却出现 503」不一定是账号问题。
旧服务把参数 400 记作账号失败并冷却，进一步隐藏成了 account_pool_unavailable；新版本将参数校验失败直接返回 400，不因此冷却账号。

## 现在的处理

- 仅转换工具声明中的 Schema，保留属性名称；不会对实际工具参数、结果或用户文本做 Schema 清洗。
- 支持常见对象/数组、局部引用、allOf 和可空类型。元数据去除，不支持的约束作为说明提示；复杂条件与循环/外部引用返回明确输入错误。
- 模型侧说明提示不等同于严格 Schema 执行。工具实际参数仍应由 DSH 或工具宿主按原始 Schema 验证。
- 对零参数工具省略参数声明，不制造假的必填字段。
- 保存本服务真正收到的上游签名，按账号、模型、调用 ID、工具名、参数内容绑定，在客户端丢失元数据时恢复。缓存仅在内存中，最多 512 条、有效期一小时，不写入诊断或配置文件。
- 没有可恢复签名的旧工具调用/结果会转成明确标注的历史文本。保留内容、继续对话，不伪造签名。
- 能恢复真实签名的历史继续使用原生 functionCall/functionResponse。

历史文本回退可以继续旧对话，但不能恢复已经丢失的模型内部思考状态。若上游拒绝客户端显式提供的旧签名，仍会返回参数错误；不会自动认定账号异常。

## 来源与验证

工具 Schema 与请求结构参考 [CLIProxyAPI 请求实现](https://github.com/router-for-me/CLIProxyAPI/blob/c2f402b45643f0740d70b9010fd7d8d0aab7ef65/internal/runtime/executor/antigravity_executor_request.go) 和 [Schema 适配实现](https://github.com/router-for-me/CLIProxyAPI/blob/c2f402b45643f0740d70b9010fd7d8d0aab7ef65/internal/util/gemini_schema.go)。Python 转换器与签名缓存为本项目实现。

使用合成工具声明和合成的 imagegen 加载历史，在 gemini-3.8-flash-high 上复现了 Schema 400 和缺签名 400。修复后，经本机真实 API 的 JSON 与 SSE 均返回 200，SSE 正常 [DONE]，账号失败计数保持 0。没有执行真实工具或生成图片。
