# 检查清单（给审查者 / Codex）

> 这份清单的对象是 **free-m-clone** 仓库。它包含两块来源不同的代码：
> ① 逐字节抄自 `dsh-our-free-model` 的页面；② 移植自该插件 `pack.js` 的腾讯 11128 防护层。
>
> 目标是**能证伪**：每条都给了命令、期望值和"失败了说明什么"。
> 全部通过不代表代码无缺陷（见 §6 已知局限），但能证明"声明与事实一致"。

---

## 0. 准备：拿到一个干净副本

```bash
git clone https://github.com/a-freshcat/free-m-clone.git
cd free-m-clone
```

**期望**：克隆后**没有** `node_modules`（这是刻意的，见 §4.1）。
如果你在已有环境里检查（目录下已有 `node_modules`），§4.1 需要先手工删掉再测。

记录一下起点，后面每一步都与它对比：

```bash
git rev-parse HEAD
git status --porcelain      # 期望：无输出（工作区干净）
```

---

## 1. 一键总览（先跑这个）

```bash
node scripts/test-all.mjs --with-drift
```

**期望**：末尾 `PASS 77/77`，退出码 0。

| 现象 | 说明什么 |
|---|---|
| `PASS 77/77` | 一切正常，继续看 §2–§5 的细项 |
| `node_modules 不存在，按 package-lock.json 安装依赖…` 后失败 | 网络不通或 lock 损坏；见 §4 |
| 某套失败 | 按套件名定位到 §3 对应小节 |
| `--check: ... differs` | 有生成物与来源不一致，见 §2.2 |

---

## 2. 声明 vs 事实（最该被质疑的一块）

### 2.1 页面是"逐字节抄的"，不是重写的

`src/extracted/provenance.json` 记录了两件事：源文件在**已安装插件**里的绝对路径 + sha256，
以及每个复制片段的**字符区间与哈希**。核验方式：

```bash
node scripts/extract.mjs --check
```

**期望**：退出码 0。

- ✅ 通过 = 仓库里的抄写产物与「本机已安装的插件」逐字节一致
- ⚠️ 若报 `源 not found`：本机没装 `dsh-our-free-model`，这条**无法验证**（不是失败，见 §6.1）
- ❌ 报 `differs`：抄写产物被改过，或插件已升级

关键常量（应当与 `src/extracted/provenance.json` 完全一致）：

| 字段 | 值 |
|---|---|
| `sourceSha256` | `3e567a15989527214741360e2b35395b51aa99ecda80e2146df1a31824d78104` |
| `version` | `2.5.4` |
| `ranges.region.chars` | `91186`（字符数，不是字节；UTF-8 字节数另见 provenance） |
| `ranges.region.lines` | `[4173, 4883]` |
| `css.keptRules` | `162` |
| `dict.zhKeys` / `enKeys` | `70` / `70` |
| `dict.missingEn` / `missingZh` | `[]` / `[]` |

> ⚠️ **`sourceSha256` 会随安装目录变动**。它记的是"我抄写那一刻，磁盘上那个
> `client.js`"的哈希。若本机插件在别的会话里被打了补丁（本仓库就遇到过：
> 2026-10-25 有人修了「失效凭据不提示重新登录」的缺陷），这个值会变，
> `extract --check` 会报 `differs` —— 那是**真的漂移**，不是误报。
> 处理方式：重跑 `node scripts/extract.mjs` 重新抄写，而不是改这个期望值。
> 复核时请以"重跑后 `--check` 退出码 0"为准，而不是这里的字面数字。

**可以主动证伪的问题**（把答案与代码对照，不要只看测试）：

1. 打开 `src/extracted/channels-page.js`，第 1 行起是工厂签名，之后是否就是插件原文？
   找 `function sameChannelTransport` 到文件末尾，这段是否没有任何"为了跑通而做的修改"？
2. 三个渠道表（`CHANNEL_ICONS` / `CHANNEL_PROVIDERS` / `CHANNEL_CREDITS`）来自各自的区间，
   而页面区域从第一个组件开始——**为什么**要这样切？（提示：否则同名的三张表会被声明两次）
3. `src/extracted/channels.css` 里有一条 `.ofm_root{--ease-instrument:…;--gap:…}`。
   页面自己从不渲染 `.ofm_root`，为什么它必须被保留？（提示：变量闭包，否则动画与间距失效）

### 2.2 防护层是"移植"而不是"重写"

```bash
node scripts/merge-guard.mjs --check
```

**期望**：退出码 0。

关键常量：

| 字段 | 值 |
|---|---|
| 基线 `vendor/upstream/tencent-gate-guard.js` sha256 | `dfb86be77bcf451b6fa4629d5591bcf80903b3cda82018736b4d66e6d625baed` |
| 合并产物 `src/guard/tencent-gate.js` sha256 | `4d4ea38503b6fe7fbcc2eae126f3ac2219a94e62501512822f581e122d3ed4c6` |
| 上游安装文件 `pack.js` sha256（记录用） | `9b6d430a51bd047534bba4608024a5e571e277fa8b9534cb143f0db119b544cc` |
| 规则数 | `10` |
| 升档预算默认 | `2` |

**重点审查项：`src/guard/tencent-gate.js` 与基线相比，函数体真的没被改吗？**

`src/guard/provenance.json` 声称只有一处差异：把 `readFileSync` / `homedir` / `join`
三个宿主名字改成模块级可变绑定。请**自己用 diff 验证**，不要相信这句话：

```bash
node -e "
const fs=require('fs');
const base=fs.readFileSync('vendor/upstream/tencent-gate-guard.js','utf8');
const merged=fs.readFileSync('src/guard/tencent-gate.js','utf8');
// 两边从 var TG_ZW 起是函数区；合并产物的函数区之后才是导出块
const cut=s=>s.slice(s.indexOf('var TG_ZW'));
const a=cut(base).trimEnd();
const bAll=cut(merged), b=bAll.slice(0, bAll.indexOf('/* ── 导出')).trimEnd();
console.log('基线函数区:', a.length, '字符 /', Buffer.byteLength(a,'utf8'), 'UTF-8 字节');
console.log('合并函数区:', b.length, '字符 /', Buffer.byteLength(b,'utf8'), 'UTF-8 字节');
console.log('逐字节完全一致:', a === b);
if (a !== b) { let i=0; while(i<a.length&&i<b.length&&a[i]===b[i])i++;
  console.log('首个差异 @'+i); console.log('  基线:', JSON.stringify(a.slice(i-40,i+80)));
  console.log('  合并:', JSON.stringify(b.slice(i-40,i+80))); }
"
```

**期望**：`逐字节完全一致: true`，两边都是 **19497 个字符 / 25289 UTF-8 字节**。
这条已被 `src/test/provenance.test.mjs` 的「函数体逐字节」用例固化（断言长度与内容全等）。

函数区之外的差异**只应有两处**，请核对：

| 位置 | 内容 |
|---|---|
| 文件头（`var TG_ZW` 之前） | `let readFileSync / homedir / join` 三个可绑定声明 + `bindTencentGateEnv()` |
| 函数区之后 | `/* ── 导出 */` 导出块 |

> **这里有个可验证的历史**：上一版合并产物比基线少 68 字节——因为我在拼装时截掉了
> 基线末尾一个未被引用的常量 `TOOL_RESULT_IMAGE_TEXT`。虽然无害，但让"逐字节保留"
> 这句话不成立。现在已经改成保留原文。
> 如果你在这一步看到任何长度差，说明同样的问题又出现了，请直接指出差在哪。
>
> 另一处历史教训：第一版移植曾"就地改写" `tencentGateUserConfig()`，引入未定义的
> `join`，异常被原文 `catch` 吞掉，导致用户扩展规则**静默失效**且没有任何报错。
> 请确认函数区里没有 `TG_ENV.` 这类改写痕迹（`assert.notIncludes(merged, 'TG_ENV.readFileSync')` 也是测试里的一条断言）。

### 2.3 设计令牌来自安装包，不是手抄的颜色

```bash
node scripts/extract-theme.mjs --check
```

**期望**：退出码 0，或 `已是最新`。
本机若无 `app.asar`，这条无法验证（不是失败）。

---

## 3. 行为是否正确

### 3.1 页面渲染与已安装插件**逐行一致**

```bash
node src/test/run.mjs src/test/parity.test.mjs
```

**期望**：`PASS 7/7`。

这 7 条的做法值得审查：它把「已安装插件的 `ChannelsPage`」和「抄写版的 `ChannelsPage`」
放进**同一个进程、同一个 DOM、同一份 React**，各接一份假后端，然后**逐行比较渲染树**。

**可以质疑的点**：

- 用自己写的迷你 DOM 比对，是否等于自证？→ 关键在两边**共用**同一个 DOM 与 React，
  差异只可能来自页面代码本身。请检查 `src/test/harness.mjs` 是否真的这样做的。
- 默认状态 595 行树完全相同，是否因为测试太宽松？→ 请检查 `diffTrees` 的实现，
  以及断言是否真的在失败时返回差异行。

### 3.2 防护层：三档语义符合实测

```bash
node src/test/run.mjs src/test/guard.test.mjs
```

**期望**：`PASS 17/17`。

**请重点质疑以下断言，它们是基于实测行为写的，可能与直觉不符：**

| 断言 | 为什么这样 |
|---|---|
| `DeepSeek Harness` → L1 就变成 `DSH` | 字面量品牌词，L1 直接替换 |
| `DeepSeek\u200BHarness`（零宽）→ **L1 不动，L2 才动** | 零宽变体归 L2 |
| `ＤｅｅｐＳｅｅｋ`（全角）→ **L1/L2 都不动，只有 L3 才动** | 只有 NFKC 能等价 |
| `C:\...\DeepSeek Harness\app.js` → **任何档位都逐字节保留** | 路径里的品牌词不是指纹 |
| 干净文本 → `applyTencentGateGuard` 返回 falsy 且内容不变 | 保住 `prompt_cache_key` 前缀缓存 |
| 用户消息（`role: user`）→ **永不被改** | 硬约束 |

**可以主动证伪的两处**：

1. `test/guard.test.mjs` 的"移植完整性"用例断言了 `typeof r.find === "string" && typeof r.replace === "string"`
   这行原文存在。请确认：**如果这行被改成更严格的过滤条件**（例如要求 `level` 必须是 1–3 整数），
   测试会不会红？→ 历史上这正是第一版改写犯的错。
2. `端到端：上游仍报 11128 → 升档 → 重发成功` 这条，假上游是测试自己写的。
   请检查 `src/guard/pipeline.mjs` 里的升档循环：**每次重发是否都从原始输入重建**，
   而不是在已清洗的文本上再洗一遍？（注释声称如此，请对照代码）

### 3.3 端到端升档链路

```bash
node src/test/run.mjs src/test/guard-demo.test.mjs
```

**期望**：`PASS 5/5`。这一套驱动的是**构建产物** `dist/demo.js` 里的按钮。

### 3.4 页面交互

```bash
node src/test/run.mjs src/test/behaviour.test.mjs
```

**期望**：`PASS 21/21`。覆盖：添加账号（含恶意链接拒绝）、测试连接、移除（确认框取消/确认）、
停用启用、一键领取、余额失败、强制刷新、模型开关、下架恢复、自动签到开关、
无 connection、渠道包挂载失败、probe 重试、ZCode、RPC 失败 toast、状态机、英文界面。

---

## 4. 工程可靠性

### 4.1 依赖可复现（这是上一轮修过的地方）

```bash
# 确认 node_modules 不在版本库里
git ls-files | grep node_modules | wc -l      # 期望：0

# 确认 lock 锁住了版本与校验和
node -e "
const l=require('./package-lock.json');
for (const n of ['react','react-dom','scheduler']) {
  const p=l.packages['node_modules/'+n];
  console.log(n, p.version, p.integrity.slice(0,20)+'…');
}"
```

**期望**：3 个包都有 `sha512-` 开头的 `integrity`。

**证伪方法：改坏 integrity，`npm ci` 必须拒绝**

```bash
node -e "
const fs=require('fs');const l=JSON.parse(fs.readFileSync('package-lock.json','utf8'));
l.packages['node_modules/react'].integrity='sha512-'+'A'.repeat(86)+'==';
fs.writeFileSync('package-lock.json',JSON.stringify(l,null,2));"
rm -rf node_modules
npm ci            # 期望：EINTEGRITY，非 0 退出
git checkout package-lock.json
```

> ⚠️ 这一步会破坏工作区，测完务必 `git checkout package-lock.json`。

### 4.2 干净克隆开箱能跑

```bash
cd .. && git clone <repo> clone2 && cd clone2
node scripts/test-all.mjs      # 不手动装任何依赖
```

**期望**：先打印 `node_modules 不存在，按 package-lock.json 安装依赖…`，
然后 `added 3 packages`，最后 `PASS 77/77`，**且没有 `DEP0190` 之类的告警**。

> Windows 上这里曾经踩两个坑：Node 20+ 不允许直接 spawn `.cmd`（EINVAL），
> 必须走 shell；而 `shell:true` + args 数组又会触发 DEP0190。
> 现在的写法是"整条命令作为字符串交给 shell"。请确认 `scripts/test-all.mjs`
> 里 `ensureDependencies()` 是否确实如此。

### 4.3 可搬迁（换目录不改行为）

```bash
# 生成物里不应出现相对本目录的路径
node -e "
const fs=require('fs');
for (const f of ['src/extracted/channels-page.js','src/extracted/provenance.json','src/guard/provenance.json']) {
  const t=fs.readFileSync(f,'utf8');
  const bad=[...t.matchAll(/\"source\":\s*\"([^\"]+)\"/g)].map(m=>m[1]).filter(s=>s.includes('../'));
  console.log(f, bad.length? '❌ '+bad : '✅');
}"
```

**期望**：全部 ✅。
理由：相对路径会随目录层级变化，导致同一个插件版本换个位置就"误报漂移"。

### 4.4 演示页自包含

```bash
# 只把 dist/ 复制到别处，双击 index.html，或：
ls dist/     # 期望：demo.css demo.js index.html（截图在 docs/ 下）
```

**期望**：在浏览器里打开，13 张渠道卡 + 防护实验台都正常，**不需要** `node_modules`、
不需要服务器、不联网。

---

## 5. 安全与卫生

### 5.1 无凭据、无真实账号

```bash
git grep -nE "eyJ[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{32,}" || echo "clean"
```

**期望**：`clean`。

### 5.2 不读写真实凭据

**请人工确认**（这条没法用命令证明）：

- `src/mock/pack.mjs` 是**纯内存**假后端：不读 `~/.dsh/channel-pack/state.json`、
  不读任何 token、不发网络请求。请搜索 `fetch(` / `readFile` / `http` 确认。
- 防护层只做本地字符串处理；`bindTencentGateEnv()` 允许注入 fs 读用户规则文件
  `~/.dsh/our-free-model/tencent-gate.json`，**这是唯一的外部读取**，且只在 Node 环境显式注入时才发生。

### 5.3 防护层不会"为了绕过而写不可见字符"

```bash
node -e "
const s=require('fs').readFileSync('src/guard/tencent-gate.js','utf8');
const codes=[...s].map(c=>c.codePointAt(0)).filter(c=>c===0x200B||c===0x200C||c===0x200D||c===0xFEFF||c===0x2060);
console.log('不可见字符出现次数:', codes.length);
"
```

**期望**：出现次数应很少，且**每一个都在注释里**（原文里有一个在 `/** … */` 的示例中）。
请定位它们，确认都不参与运行。

---

## 6. 已知局限（**不要**当成缺陷报）

诚实列出，避免把"已声明的限制"误判为 bug：

1. **`extract --check` / `extract-theme --check` 依赖本机安装**
   它们要读 `~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/client.js`
   和桌面端 `app.asar`。在没装插件的机器上会跳过或报"源 not found"——这是设计，
   不是失败。防护层的 `merge-guard --check` 不依赖外部路径（基线已入库）。

2. **假后端不联网**
   `src/mock/pack.mjs` 里所有登录/领取/余额都是编造的。所以"渠道能用"在本仓库里
   **无法被证明**，只能证明"页面与防护层的行为正确"。真实接入依赖原插件的
   `vendor/channel-pack/pack.js`（2.9 MB 编译产物），**没有抄，也抄不了**。

3. **真实 11128 未做端到端验证**
   上游拦截是被动事件，主动造它等于刷封号。仓库里只有离线逻辑验证 + 假上游。
   真实事件的取证靠原插件的埋点（前缀 `[ofm-tencent-gate]`）。
   **请不要为了"验证"去批量探测上游。**

4. **迷你 DOM 不是浏览器**
   `src/runtime/mini-dom.mjs` 只实现页面用到的那部分 API，没有布局、没有 CSS 级联。
   所以"渲染树一致"证明的是 DOM 结构一致，**不证明像素级外观一致**。
   外观请用 `docs/` 里的三张截图或自己开 `dist/index.html` 对照。

5. **`scripts/verify-pack.mjs` 只能在本机跑**
   它校验的是**已安装**的 `pack.js`（可通过 `OFM_PACK_INSTALLED` 指定）。没装插件时无意义。

### 6.1 已经修掉的缺陷（由外部审查发现，留作参照）

这些不是"局限"，是**真实缺陷**，已修复并有回归测试。列出来是为了让下一个审查者
知道它们存在过，以及现在的防线在哪：

| 缺陷 | 后果 | 现在由什么守住 |
|---|---|---|
| 管道对同一个响应调了两次 `response.text()` | 原生 `Response` 的 body 只能读一次，第二次失败被 `.catch(() => '')` 吞成空串 → **持续 11128 不抛错**、普通 400 的调用方读不到错误详情 | `readBodyOnce()` 只读一次并缓存，并给响应装回可重复调用的 `text()`；`src/test/native-response.test.mjs` 用**原生 Response** 断言 |
| 重建消息时只保留 `role`/`content` | `tool_calls` / `tool_call_id` / `name` 丢失，工具调用与结果断开关联 | `scrubForLevel()` 改为整条消息浅拷贝；同名测试断言 5 个字段都在 |
| 非 Windows 首次安装依赖传命令字符串且 `shell:false` | 参数不拆分，报 `ENOENT` | `test-all.mjs` 分平台：Windows 走 `shell:true` + 命令串，其它平台走 `npm` + 参数数组 |

> **这三条的教训是同一条**：假对象替真实依赖补了它不提供的属性，就掩盖了真实缺陷。
> 第一、二条原本被 `{ text: async () => … }` 这种可重复读取的假响应掩盖；
> 第三条被"只在 Windows 上测过"掩盖。
> 同类检查请优先怀疑 **mock 是否比真实对象更宽容**。
>
> 还有一条自查方式：把 `src/guard/pipeline.mjs` 换回修复前的版本再跑
> `native-response.test.mjs`，应当是 **4 条失败**。如果全绿，说明测试是空断言。

### 6.2 跟随上游修复重新抄写（2026-10-26）

安装目录里的 `client.js` 不是一成不变的。本仓库抄写之后，它在 **2026-10-25**
被别的会话打过一个补丁：

```
needsRelogin(entry) → entry?.refreshable === false
```

原来判据是 `!refreshable && !expiresAt`，而 CodeArts 的 `expiresAt` 永远有值
（access_token 约两小时就写一个），于是**账号明明已经用不了，面板还显示「已启用」**。
这段代码正落在本仓库抄写的区间内，所以重新抽取跟进：

| 项 | 变化 |
|---|---|
| 页面切片 | `90672` → `91186` 字符 |
| 词条 | `69` → `70`（新增 `chan.expires.relogin`） |
| `sourceSha256` | `d5b22d78…` → `3e567a15…` |

**这条留给审查者的意义**：`sourceSha256` 与 `region.chars` 是**会变**的。
比对时以"重跑 `extract.mjs` 后 `--check` 退出码 0"为准；如果只因哈希不同就
判定"副本被篡改"，会误判——先看安装目录里的 `client.js` 是不是也变了。

同时补了两条防线：

| 补的防线 | 原因 |
|---|---|
| `provenance` 测试不再写死词条数，改读 `provenance.dict.zhKeys` 并校验 zh/en 相等 | 写死 `69` 时，插件加一句文案就让整条测试假红 |
| 账号折叠区的渲染树对比新增 `需重新登录` 与 `ofm_acct pending` 断言 | `trae-2` 正是 `refreshable:false` 的行，此前这个分支渲染出来却没人断言 |
| 新增两条防护层回归：非文本块原样保留、升档重发工具字段不丢 | 对应「序列化消息时压平 content 数组」这一类真实缺陷（CodeArts 图片输入即此形态） |
| 测试不再依赖 `process.cwd()` | 从任意目录调用 `test-all.mjs` 时，原会去 `C:\Windows\src\...` 找文件并满屏假红 |

---

## 7. 快速核对表（勾选式）

| # | 检查 | 命令 | 期望 |
|---|---|---|---|
| 1 | 工作区干净 | `git status --porcelain` | 无输出 |
| 2 | 全套测试 | `node scripts/test-all.mjs --with-drift` | `PASS 77/77`，exit 0 |
| 3 | 页面未漂移 | `node scripts/extract.mjs --check` | exit 0 |
| 4 | 防护层未漂移 | `node scripts/merge-guard.mjs --check` | exit 0 |
| 5 | 令牌未漂移 | `node scripts/extract-theme.mjs --check` | exit 0 |
| 6 | 渲染树一致 | `node src/test/run.mjs src/test/parity.test.mjs` | `7/7` |
| 7 | 防护语义 | `node src/test/run.mjs src/test/guard.test.mjs` | `17/17` |
| 8 | 端到端升档 | `node src/test/run.mjs src/test/guard-demo.test.mjs` | `5/5` |
| 9 | 页面交互 | `node src/test/run.mjs src/test/behaviour.test.mjs` | `21/21` |
| 10 | 交付物冒烟 | `node src/test/run.mjs src/test/demo.test.mjs` | `9/9` |
| 11 | 防漂移自查 | `node src/test/run.mjs src/test/provenance.test.mjs` | `12/12` |
| 12 | **原生 Response 回归** | `node src/test/run.mjs src/test/native-response.test.mjs` | `6/6` |
| 13 | 依赖不入库 | `git ls-files \| grep -c node_modules` | `0` |
| 14 | 无凭据 | `git grep -E "ghp_\|sk-[A-Za-z0-9]{32}"` | 无匹配 |
| 15 | 干净克隆可跑 | 新 clone 后直接 `node scripts/test-all.mjs` | 自动装依赖后 `77/77` |
| 16 | **与工作目录无关** | 在 `C:\Windows` 等任意目录执行 `node <repo>/scripts/test-all.mjs --no-build` | 同样 `77/77`（测试不得依赖 `process.cwd()`） |

**如果你发现任何一条与上表不符，那就是真问题，请直接指出命令与输出。**
**如果你发现上表"期望值"本身写错了（例如某条测试其实是空断言），那更有价值——请说明理由。**
