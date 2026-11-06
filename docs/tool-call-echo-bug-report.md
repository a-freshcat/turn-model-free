# 错误报告：Antigravity 工具调用退化成正文字符串

> 影响：`gemini-4-argon-medium` / `gemini-3.8-flash-high` 在多轮工具调用后不再发 `tool_calls`，改用 assistant `content` 吐出一行行 `Historical assistant tool call (already executed): {...}`。
> 报告依据：真实会话日志 18444 行 + 8 组复现 payload + 直连上游探测。

---

## 0. 一句话结论

`antigravity_driver.py` 在历史回放时，拿不到上游 `thoughtSignature` 的 `functionCall` 会被**降级成一行普通文本**：

```python
parts.append({'text': 'Historical assistant tool call (already executed): ' +
    json.dumps({'tool': function['name'], 'arguments': arguments}, ensure_ascii=False)})
```

模型在上下文里看到的「自己上一轮的发言」就是这个格式，于是照着写 —— 不再发 `tool_calls`，改用正文输出这行字符串。

而签名拿不到，是**三处叠加**造成的必然而非偶发：

1. `ToolSignatureCache` 的 key 塞了 `(账号ID, 模型名, call_id, 工具名, args摘要)`，账号轮转 / 模型切换 / 进程重启全部 miss；
2. pi-ai 的 `openai-completions` 适配层**不解析也不回传** `extra_content`，签名连 `replayState` 都进不去；
3. 网关进程内的缓存是唯一通路，一旦重启全丢。

---

## 1. 现场记录

日志：`C:\Users\handsomeboys\.dsh\sessions\--C-Users-handsomeboys-Documents-deepseek-harness-repair_question--\session-67604be4-637c-4c5a-a1ad-de8d672149b0\session.v4.jsonl.zstd`

### 1.1 出问题的五个步骤

| 行号 | turn | step | 模型 | 文本长度 | 有 tool_calls | stopReason | inputTokens |
|---|---|---|---|---|---|---|---|
| 16976 | 51 | 2 | antigravity/gemini-4-argon-medium | 399 | **是** | `toolUse` | 194434 |
| 16981 | 51 | 3 | antigravity/gemini-4-argon-medium | 654 | 否 | `stop` | 194800 |
| 17020 | 52 | 4 | antigravity/gemini-4-argon-medium | 740 | 否 | `stop` | 201854 |
| 17053 | 54 | 2 | antigravity/gemini-4-argon-medium | 730 | 否 | `stop` | 208673 |
| 17070 | 55 | 2 | antigravity/gemini-3.8-flash-high | 171 | 否 | `stop` | 212338 |

`stopReason` 从 `toolUse` 掉到 `stop`，模型主动放弃工具调用。

### 1.2 行 16976 的原文（混合形态）

```json
{
  "role": "assistant",
  "content": [
    {"type": "text",
     "text": "Historical assistant tool call (already executed): {\"tool\": \"read\", \"arguments\": {\"offset\": 80, \"limit\": 60, \"file_path\": \"C:\\\\Users\\\\handsomeboys\\\\.dsh\\\\profiles\\\\desktop\\\\node_modules\\\\dsh-our-free-model\\\\index.js\"}}Historical assistant tool call (already executed): {\"tool\": \"read\", \"arguments\": {\"file_path\": \"C:\\\\Users\\\\handsomeboys\\\\.dsh\\\\ofm-guard\\\\ofm-guard.mjs\", \"offset\": 1, \"limit\": 120}}"},
    {"type": "tool-call", "id": "call_16304708030960510913", "name": "read",
     "arguments": "{\"offset\":1,\"limit\":100,\"file_path\":\"...cordis.patch.yml\"}"}
  ],
  "source": {"kind": "model", "provider": "antigravity-pool",
             "model": "antigravity/gemini-4-argon-medium"}
}
```

一条消息里**同时**有 echo 文本和真 tool-call。下一轮这两段都会被降级，标记数量翻倍。

### 1.3 行 17020 的原文（纯文本版）

```json
{"type": "text",
 "text": "Historical assistant tool call (already executed): {\"tool\": \"read\", \"arguments\": {\"offset\": 910, \"limit\": 45, \"file_path\": \"...index.js\"}}Historical assistant tool call (already executed): {\"tool\": \"read\", \"arguments\": {\"offset\": 1850, \"limit\": 35, \"file_path\": \"...index.js\"}}Historical assistant tool call (already executed): {\"tool\": \"pwsh\", \"arguments\": {\"description\": \"Check listening ports on 8326 and 53760\", \"command\": \"Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(8326, 8199, 53760) } | Select-Object LocalAddress, LocalPort, OwningProcess\"}}"}
```

三条标记串联，`stopReason: stop`，**零 tool_calls**。

### 1.4 被 echo 的调用从未真实发生过

检索 `"offset":910` / `"offset":1850` / `"offset":80,"limit":60` / `"offset":1,"limit":120` 作为 `tool/call` 记录出现的次数：

```
=== 16976 之前所有 read(dsh-our-free-model\index.js) 的 offset ===
（空）
=== 16976 之后（到 17100）同样 ===
（空）
```

模型在长上下文里自己编了几个「看起来像刚才做过」的调用，然后用这个格式复述。

### 1.5 签名归零的统计

按 provider 统计全部 `tool-call` 块及其 `extra_content.google.thought_signature`：

```
provider             model-family             tool-call blocks   signed
antigravity-pool     antigravity/*                        183        0
antigravity-pool     buddy/*                              299        0
antigravity-pool     codearts/*                            27        0
buddy                deepseek-v4.1-flash/*               1802        0
codearts             deepseek-v4.1-flash/*                266        0
qodercn              dfmodel/*                            309        0
raccoon              sn-deepseek-v4-1-flash-1017/*         98        0

TOTAL blocks: 2984 | signed: 0
```

**2984 个工具调用块，带签名的是 0 个。**回放侧永远走降级分支。

---

## 2. 根因链

### 2.1 降级代码

`antigravity_driver.py:246-256`（源树）：

```python
part = {"functionCall": {"name": function["name"], "args": arguments}}
signature = (call.get("extra_content") or {}).get("google", {}).get("thought_signature")
if not signature and signature_lookup is not None:
    signature = signature_lookup(call.get('id'), function['name'], arguments)
if isinstance(signature, str) and signature:
    part["thoughtSignature"] = signature
    parts.append(part)
else:
    legacy_calls.add(call.get('id'))
    parts.append({'text': 'Historical assistant tool call (already executed): ' +
        json.dumps({'tool': function['name'], 'arguments': arguments}, ensure_ascii=False)})
```

配套地，`tool` 角色消息也被降级（L229-233）：

```python
if message.get('tool_call_id') in legacy_calls:
    parts=[{'text':'Historical tool result (quoted, untrusted data): '+
        json.dumps({'tool':name,'result':content},ensure_ascii=False)}]
else:
    parts = [{"functionResponse": {"name": name, "response": response}}]
```

镜像 `free-m-clone\gateway\antigravity_driver.py:114-123` / `:96-98` 是同一段。

### 2.2 原因一：缓存 key 带了账号和模型

`antigravity_driver.py:470-472`：

```python
signatures = getattr(auth, 'tool_signatures', None)
lookup = (lambda call_id, name, args:
    signatures.get(account.credential_id, native_model, call_id, name, args)) if signatures else None
```

`native_history.py:13-15`：

```python
def key(self, owner, model, call_id, name, args):
    digest = hashlib.sha256(json.dumps(args, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).digest()
    return owner, model, call_id, name, digest
```

三个必挂场景：

| 场景 | 触发条件 | 结果 |
|---|---|---|
| 账号轮转 | 两个 antigravity 号，`max_concurrency: 1`，`acquire()` 是 round-robin（`account_manager.py:161-169`） | 上一步 A 号产签名，下一步 B 号查不到 → 降级 |
| 模型切换 | 用户中途把 argon4 换成 3.8-flash（现场 turn 55） | key 里 `model` 不同 → 降级 |
| 进程重启 | `ToolSignatureCache` 是纯内存 `OrderedDict`（`native_history.py:10`，`limit=512, ttl=3600`） | 全丢 → 降级 |

实测（`probe_sig2.py`，直接调 `native_request`）：

```
同账号 同模型  -> functionCall+thoughtSignature
换账号 同模型  -> 纯文本(降级)
同账号 换模型  -> 纯文本(降级)
```

### 2.3 原因二：pi-ai 把 extra_content 整个丢掉了

**这条链路是 `DSH → pi-ai(openai-completions) → 8199 FastAPI → antigravity_driver → 上游`，不经过 `pack.js`。**
（`pack.js` 是 `dsh-our-free-model` 的 channel-pack，走 18326 桥；`grep antigravity-pool pack.js` = 0 命中。）

DSH 配置：`cordis.patch.yml:324-328`

```yaml
antigravity-pool:
  displayName: 本机多渠道配额池
  apiKeyEnv: ANTIGRAVITY_POOL_API_KEY
  api: openai-completions
  baseURL: http://127.0.0.1:8199/v1
```

版本：`@earendil-works/pi-ai` 1.1.0，`@deepseek-ai/dsh-llm-pi-ai` 0.2.1-rc.5。

**响应解析侧**，`pi-ai/dist/api/openai-completions.js:426-452`：

```js
if (choice?.delta?.tool_calls) {
    for (const toolCall of choice.delta.tool_calls) {
        const block = ensureToolCallBlock(toolCall);
        if (!block.id && toolCall.id) { block.id = toolCall.id; ... }
        const name = toolCall.function?.name ?? toolCall.custom?.name;
        if (!block.name && name) { block.name = name; }
        ...
    }
}
```

只读 `id` / `function.name` / `function.arguments`。**`extra_content` 一个字都没读。**

`extra_content` 在 `pi-ai/dist` 全目录出现次数：**0**。

**序列化侧**，`openai-completions.js:1021-1041`：

```js
assistantMsg.tool_calls = toolCalls.map((tc) => {
    ...
    return {
        id: tc.id,
        type: "function",
        function: { name: tc.name, arguments: JSON.stringify(tc.arguments) },
    };
});
```

只写 `id / type / function`。**不回传 `extra_content`。**

**但 DSH 自己是有签名槽位的**——`dsh-llm-pi-ai/lib/index.js:206-213`：

```js
case "tool-call": return {
    type: "toolCall",
    id: block.id,
    name: block.name,
    arguments: parseArguments(block.arguments),
    ...replay.type === "tool-call" && replay.thoughtSignature !== void 0
        ? { thoughtSignature: replay.thoughtSignature } : {},
    ...
};
```

`toPiReplayState`（L85-89）也会写回：

```js
case "toolCall": return {
    type: "tool-call",
    ...block.thoughtSignature === void 0 ? {} : { thoughtSignature: block.thoughtSignature },
    ...
};
```

pi-ai 内部也有 `thoughtSignature` 通道（`assistant-message-frame.js:395-408` 的 `toolcall_end` 分支）。**缺口在 `openai-completions` 这一层：上游响应里的 `extra_content.google.thought_signature` 没有映射到 `block.thoughtSignature`，序列化时也没有从 `block.thoughtSignature` 映射回 `extra_content`。**

会话日志佐证：`antigravity replayState blocks carrying thoughtSignature: 0`。

### 2.4 原因三：网关是唯一通路

签名只要进了 `ToolSignatureCache` 还能活 1 小时。但那是**纯内存**的，网关一重启（`launch_web.py` 每次拉起新进程）就全清。加上 2.3 的断裂，缓存实际上是从未命中过的状态。

### 2.5 放大器

一条 assistant 消息若原本既有 echo 文本**又有**未签名 tool-call（行 16976 的形状），降级后：

- `content[0]`（echo 文本）原样保留 → 1 条标记
- `tool_calls[0]` 被降级 → 再 1 条标记

一条产出 2 条。下一轮 2 条都被保留，格式被进一步强化。这与日志里标记从 2 涨到 3 的观察一致。

---

## 3. 上游签名规则（直连实测）

脚本 `probe_sig_upstream.py`，直连 `https://cloudcode-pa.googleapis.com/v1internal:generateContent`：

```
--- signature=none     HTTP 429  （配额已空，未走到校验层）
--- signature=garbage  HTTP 400  Invalid value at 'request.contents[1].parts[0].thought_signature'
                                 (TYPE_BYTES), Base64 decoding failed for "GARBAGE-NOT-FROM-UPSTREAM"
--- signature=real     HTTP 429
```

**`thoughtSignature` 是 TYPE_BYTES / base64，伪造字符串直接 400。**所以不能改成「随便填一个」——降级分支本身是必要的。

但 `none` 和 `real` 都撞上 `429 RESOURCE_EXHAUSTED`，**没有验证出「完全不带签名的 functionCall 上游是否接受」**。这是修复前必须先测的一项（见 §6）。

顺带确认：两个账号的 `gemini-4-argon-medium` 配额此刻都是空的。

```json
// GET http://127.0.0.1:8199/admin/accounts
{"accounts": [
  {"id": "ag-f7a97668844467a834101611", "status": "active",
   "last_failure_reason": "antigravity_transport", "in_flight": 0},
  {"id": "ag-2480c34faf591b1b4419982f", "status": "active", "in_flight": 1}
]}
```

---

## 4. 复现矩阵：短上下文复现不出来

8 组 payload 打 8199，全部 200：

| 脚本 | 构造 | argon4 结果 |
|---|---|---|
| `mk_legacy.py` | 1 轮无签名历史工具调用 | 正常出自然语言 |
| `mk_echo.py` | 3 条无签名工具调用 | 正常出 `tool_calls: [read]` |
| `mk_echo2.py` | 历史已降级成文本（1 轮） | 正常出 `tool_calls` |
| `mk_echo3.py` | 4 轮降级文本 | 正常出 `tool_calls` |
| `mk_echo4.py` | 三种历史编码 A/B（assistant / user 包 / system 包） | 全正常出 `tool_calls` |
| `mk_mix.py` | echo 文本 + 真 tool_call 混合 | 正常出 `tool_calls` |
| `mk_mixed.py` | 原生/降级交替 48 轮 | 正常出 `tool_calls` |
| `mk_amp.py` | 放大器形状 28 轮 | 正常出 `tool_calls` |
| **`mk_huge.py`** | **160 轮降级历史，109119 tokens** | **正常出 `tool_calls: [grep]`** |
| **同上 payload 换 g38** | **109120 tokens** | **`tool_calls: []`，吐 767 字符散文** |

**唯一能稳定复现的差异是上下文长度。**短上下文怎么造都复现不出来。

**所以降级文本不是触发器，是诱因。**真正触发的是「历史里堆了几十条同格式的假 assistant 发言 + 上下文逼近 200k」。长上下文里模型把这个高频模式当成了输出规范。

这解释了为什么换模型（argon4 → g38）照样复发，也解释了为什么一旦出现第一条就迅速恶化。

---

## 5. 要改的位置

### P0-A —— `native_history.py` 的 key 去掉账号和模型

```python
def key(self, call_id, name, args):
    digest = hashlib.sha256(json.dumps(args, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).digest()
    return call_id, name, digest
```

`put` / `get` 的 `owner, model` 参数同步去掉；`antigravity_driver.py:472` 与 `:489` 两个调用点同步改；镜像同改。

**已用 monkey-patch 验证**（`probe_fix.py`）：

```
现状：同账号同模型 native / 换账号 degraded / 换模型 degraded
放松：同账号同模型 native / 换账号 native   / 换模型 native
```

理由：签名是「这次调用内容」的凭据，上游按内容校验，不按账号校验。

**连带改测试**：`tests/test_tool_compatibility.py:65-82` 的 `test_real_signature_cache_is_scoped_to_account_model_and_arguments` 明确断言 `cache.get('other',...)` 与 `cache.get('owner','other',...)` 返回 `None`。不改会红。

### P0-B —— 降级标记换成不可模仿的形式

`Historical assistant tool call (already executed): {...}` 是完整英文句子，模型极易模仿。三个方案，推荐第 3 个：

1. 结构化标记：`[tool-call:read {"file_path":"..."}]`
2. 中文且不成句：`〈已执行工具调用〉read({"file_path":"..."})`
3. **不进 `contents`**：把降级历史汇总成一段，只放进 `systemInstruction`

第 3 个最彻底——`systemInstruction` 里的内容模型不会当作「自己上一轮说过的话」来模仿。这一条改完，即使签名继续丢，也不会再出现「模型复述格式」。

### P1-A —— 打通 `extra_content` 的两个断点（根治）

两个文件，两处小改：

**断点 1**：`pi-ai/dist/api/openai-completions.js:426-452`（响应解析）

```js
const block = ensureToolCallBlock(toolCall);
if (!block.id && toolCall.id) { ... }
// 补：把上游签名搬进 block
const sig = toolCall.extra_content?.google?.thought_signature;
if (typeof sig === "string" && sig.length > 0) block.thoughtSignature = sig;
```

**断点 2**：同文件 `:1021-1041`（序列化）

```js
return {
    id: tc.id,
    type: "function",
    function: { name: tc.name, arguments: JSON.stringify(tc.arguments) },
    // 补：把签名送回去
    ...tc.thoughtSignature === undefined ? {}
        : { extra_content: { google: { thought_signature: tc.thoughtSignature } } },
};
```

下游通路已经就绪，不用动：

- `assistant-message-frame.js:395-408` 的 `toolcall_end` 已在维护 `block.thoughtSignature`
- `dsh-llm-pi-ai/lib/index.js:206-213` / `:85-89` 的 replayState 双向映射都在
- `antigravity_driver.py:246` 已经在读 `call["extra_content"]["google"]["thought_signature"]`
- `antigravity_driver.py:393-394` 已经在往下游塞 `call["extra_content"]`（SSE 实测能出来，见下）

**已验证下游是通的**：8199 的 SSE 原始帧里签名在。

```bash
curl -s -X POST ... --data-binary @t_stream_sig.json http://127.0.0.1:8199/v1/chat/completions
```
```
data: {"id":"chatcmpl-64a07d...","object":"chat.completion.chunk","choices":[{"index":0,
 "delta":{"role":"assistant","tool_calls":[{"id":"call_11122306606546162875","type":"function",
 "function":{"name":"read","arguments":"{\"path\":\"gateway/main.py\"}"},
 "extra_content":{"google":{"thought_signature":"Ep0BCpoBAWkUfRMTRVhKrvtVdW6N2G...
```

**所以断点确实只在 pi-ai 那一层。**

> 注意：改 `pi-ai` 是改 `D:\ai_ds\resources\app\dsh\node_modules\` 下的第三方包，`pnpm install` / 应用升级会覆盖。要么提 patch-package，要么在 `dsh-llm-pi-ai` 里加一层适配。这是设计决策，需要定。

### P1-B —— 源树与镜像已严重分叉

```
antigravity_driver.py    src= 935L/sha256 29678913…   mir= 373L/sha256 AEA7B010…
native_history.py        src=  32L/677093FD…          mir=  32L/677093FD…   (一致)
account_manager.py       src= 232L/4CE45660…          mir= 232L/B9A0B364…   (不一致)
upstream_client.py       src= 355L/35636E04…          mir= 355L/35636E04…   (一致)
main.py                  src= 369L/D1B01E8B…          mir= 367L/0921FFA3…   (不一致)
```

`native_request` 签名已分叉：

```
源树 L196: def native_request(messages, options, *, signature_lookup=None, native_model: str = "", inject_search_tool: bool = False)
镜像 L64 : def native_request(messages, options, *, signature_lookup=None)
```

镜像**完全没有** `supports_native_search` / `INTERNAL_SEARCH_TOOL` / `is_image_model` / `extract_grounding` / `_SEARCH_CONTEXT_BY_CALL_ID` / `native_http_error` 的字段白名单（源树 935 行 vs 镜像 373 行 = 562 行差异）。

**镜像现在跑的正是这 373 行的旧代码**——也就是说镜像这条链路上，搜索代理、图片输出、grounding 全部缺失，`native_http_error` 还是旧版。修 driver 前得先定：同步源树，还是双轨。

---

## 6. 修复前必须补测的一项

**上游接不接受不带 `thoughtSignature` 的历史 `functionCall`？**

如果接受 → 降级分支可以整个删掉，问题从根上消失，P0-A 都不用做。
如果不接受 → P0-A + P1-A 都得做。

当前两个账号配额都空（`429 RESOURCE_EXHAUSTED`），测不出来。补测脚本已经写好（`probe_sig_matrix.py`，四路：none / b64_garbage / b64_other / real），等配额恢复后跑：

```powershell
cd C:\Users\handsomeboys\Documents\deepseek-harness\repair_question
$env:PROBE_ACCOUNT="0"   # 或 "1"
& "C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity\.venv\Scripts\python.exe" probe_sig_matrix.py
```

判读：
- `none` 返回 200 → 直接删降级分支
- `none` 返回 400 且报 `thought_signature is required` → 保留降级，做 P0-A + P0-B + P1-A
- `b64_other` 返回 200 → 说明上游只校验 base64 合法性，那连缓存都不用，填个固定 base64 占位即可

---

## 7. 已排除项（别在这上面浪费时间）

| 怀疑 | 验证 | 结论 |
|---|---|---|
| 模型坏了 | 短上下文（158 tokens）历史工具调用 | 完全正常，正确回答 |
| args 摘要不稳导致 miss | 同 args 不同 key 顺序 | `sort_keys=True` + `separators` 规范化，稳定 |
| `pack.js` 吞了签名 | `grep antigravity-pool pack.js` | 0 命中，这条链路不经过 pack.js |
| `encode_event` 丢了 `extra_content` | 抓 8199 SSE 原始帧 | 签名在，没丢 |
| echo 的调用是真实历史 | 检索 `tool/call` 记录 | 0 次，全是模型编的 |
| argon4 与 g38 差异是模型特性 | `t_huge_*` 同 payload 对比 | 差异只在 token 边界 |
| `stripCourseLeakFromHistoryContent` 相关 | 代码路径 | 只在 codearts 分支，无关 |

---

## 8. 环境与脚本

### 环境

```
网关       ：python launch_web.py --port 8199
             PID 27168，启动于 13:35:57
             源码根 C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity
venv       ：C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity\.venv\Scripts\python.exe
API key    ：C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity\.gateway-api-key
凭据       ：...\plugin_antigravity\.gateway-auth\ag-*.json（DPAPI 加密）
DSH 配置   ：C:\Users\handsomeboys\.dsh\profiles\desktop\cordis.patch.yml:324-368
DSH 包     ：D:\ai_ds\resources\app\dsh\node_modules\@earendil-works\pi-ai@1.1.0
             D:\ai_ds\resources\app\dsh\node_modules\@deepseek-ai\dsh-llm-pi-ai@0.2.1-rc.5
```

### 脚本

| 脚本 | 用途 |
|---|---|
| `probe_sig2.py` | 证明签名缓存 key 的 account/model 作用域 |
| `probe_fix.py` | monkey-patch 验证「放开 key」能恢复 native 回放 |
| `probe_sig_upstream.py` | 直连上游验证签名校验规则 |
| `probe_sig_matrix.py` | 四路矩阵，**待配额恢复后跑** |
| `mk_huge.py` | 生成 109k token 长上下文 payload（唯一稳定复现） |
| `mk_amp.py` | 放大器形状（echo + 未签名 tool_call 同条消息） |
| `mk_echo{.py,2,3,4}` / `mk_mix.py` / `mk_mixed.py` | 排除项用的短上下文对照 |
| `count_all.py` | 按 provider 统计 signed/unsigned |
| `scan_where.py` | 定位每条标记出现在哪个字段 |
| `report_evidence.py` | 汇总统计（污染消息清单 + token 数） |
| `verify4.py` | 证明被 echo 的调用从未真实发生 |
| `check_piai.py` | 证明 replayState 里没有任何 thoughtSignature |

### 稳定复现命令

```powershell
cd C:\Users\handsomeboys\Documents\deepseek-harness\repair_question
python mk_huge.py
$k = Get-Content C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity\.gateway-api-key
curl.exe -s --max-time 300 -X POST -H "Authorization: Bearer $k" `
  -H "Content-Type: application/json" --data-binary "@t_huge_g38.json" `
  http://127.0.0.1:8199/v1/chat/completions
```
期望：`content` 是 700+ 字符散文，`tool_calls` 为空。

---

## 9. 验收标准

1. `probe_fix.py` 三个场景（同账号同模型 / 换账号 / 换模型）全部输出 `native`
2. 连续 3 轮用不同账号轮流执行工具，历史回放全程 `functionCall` + `thoughtSignature`
3. `count_all.py` 的 `signed` 列不再是 0
4. 长上下文（≥150k tokens）不再出现 `Historical assistant tool call`
5. pi-ai 那两处补丁生效后，`check_piai.py` 输出 `antigravity replayState blocks carrying thoughtSignature: N`（N > 0）
6. `tests/test_tool_compatibility.py` 全绿（含改写后的 cache scope 测试）
7. 镜像 `free-m-clone\gateway` 测试全绿
