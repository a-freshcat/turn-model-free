# free-m-clone · 白嫖模型接入（抄写版 + 腾讯 11128 防护）

`dsh-our-free-model` 插件里「白嫖模型接入」页面的独立抄写版，**并把该插件的腾讯渠道防护层
一并移植进来、接成可运行的请求管道**。

- 页面来源：`~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/client.js` v2.5.4，第 4165–4861 行（逐字节）
- 防护来源：`pack.js` v2.5.4（install sha256 `9b6d430a…`），从 `main_repai/.../tencent-gate-guard.js` 移植
- 测试：**66 / 66 通过**（防护层 15 + 防漂移 9 + 渲染树对比 7 + 行为 20 + 交付物 9 + 防护演示 5 + 其他）
- 演示：双击 `dist/index.html`，不需要服务器、不需要联网
- 打包：`free-m-clone.zip` 解压到任何位置都能直接跑（见 [REPORT.md](REPORT.md) 第五节）

完整报告见 [REPORT.md](REPORT.md)。

## 30 秒上手

```bash
# 1) 看页面 + 防护实验台（不用装任何东西）
start dist/index.html

# 2) 两处来源有没有漂移
node scripts/extract.mjs --check        # 页面 vs 已安装插件
node scripts/extract-theme.mjs --check  # 设计令牌 vs app.asar
node scripts/merge-guard.mjs --check    # 防护层 vs pack.js 里的抽取件

# 3) 跑全部测试
node scripts/test-all.mjs
```

## 两部分内容

### 1. 抄来的页面（前端）

| 路径 | 作用 |
|---|---|
| `src/extracted/channels-page.js` | 页面全部代码，**原文**（13 渠道 / 图标 / 登录轮询 / 余额 / 模型开关） |
| `src/extracted/channels.css` | 162 条规则 + 10 组动画，按原文格式 |
| `src/extracted/channels-dict.js` | 69 条 `chan.*` 中文 + 69 条英文 |
| `src/extracted/theme-tokens.css` | 从桌面端 `app.asar` 取出的 627 个设计令牌 |

### 2. 移植的防护层（后端）

| 路径 | 作用 |
|---|---|
| `src/guard/tencent-gate.js` | 防护层本体：10 条规则、三档语义、路径遮罩、WeakSet 登记、取证埋点。**函数体逐字节来自 pack.js** |
| `src/guard/pipeline.mjs` | 接进真实请求路径：构建消息 → 发送 → 400/11128 → 升档 → 从原始输入重建 → 重发（预算 2 次） |
| `src/guard/provenance.json` | 源的 sha256、规则数、以及"只动了哪一处"的记录 |
| `src/demo/guard-lab.mjs` | 演示页里的防护实验台：可改提示词、切上游严格程度、看每次实际发出的字节 |

### 支撑

| 路径 | 作用 |
|---|---|
| `src/mock/pack.mjs` | 假渠道包：13 家渠道的 RPC 语义与数据形状 |
| `src/runtime/mini-dom.mjs` | 无浏览器时可用的迷你 DOM |
| `src/test/` | 六套测试 + 迷你运行器 |
| `dist/` | 构建产物：`index.html` + `demo.js` + `demo.css` |
| `scripts/` | `extract` / `extract-theme` / `merge-guard` / `build-demo` / `test-all` |

## 命令

| 命令 | 说明 |
|---|---|
| `npm test` | 构建 + 跑全部 66 条测试 |
| `npm run check:drift` | 三处来源是否都没漂移（不一致退出码 1） |
| `npm run verify` | 构建 + 防漂移 + 全部测试 |
| `npm run extract` | 插件升级后重新抄页面 + 主题令牌 |
| `npm run extract:guard` | 插件升级后重新移植防护层 |
| `npm run demo` | 重新构建演示页 |
| `npm run test:guard` | 只跑防护层测试 |
| `npm run test:parity` | 只跑渲染树对比 |
| `npm run test:behaviour` | 只跑行为测试 |

## 演示页用法

双击 `dist/index.html`。页面分两半：

**上半 · 防护实验台**（移植过来的后端行为）

- 改 system 提示词（默认样例含四个实测指纹 + 一个必须保留的路径）
- 切假上游严格程度：只查字面量 / 还查零宽（要 L2）/ 还查全角（要 L3）/ 永远拒绝
- 「发一次请求」跑完整流程：升档几次、每次实际发出的字节、路径有没有被破坏、埋点长什么样
- 「只看三档清洗效果」并排比较 L1/L2/L3

**下半 · 渠道卡**（抄来的插件页面）

- 7 个场景 / 中英 / 深浅色 / 每次 RPC 明细
- 深链：`dist/index.html#scenario=fresh&lang=en&theme=dark&open=both`

## 硬约束（改防护层前必读）

这几条是原插件用真实故障换来的，移植版同样遵守，并且有测试盯着：

1. **不写不可见字符去绕过** —— 零宽塞进 Windows 路径会让工具调用直接失败
2. **不改用户内容** —— 只处理 `system` / `assistant`；`user` 与 `tool` 逐字节保留
3. **干净文本逐字节不变** —— 否则丢 `prompt_cache_key` 前缀缓存
4. **路径逐字节保留** —— `C:\...\DeepSeek Harness\app.js` 里的品牌词不是指纹
5. **升档有界** —— 默认 2 次，整请求共享一份预算
6. **已经吐过内容就不再重发**

## 它不是什么

不是能登录真实渠道的插件。登录、领取、余额来自 `src/mock/pack.mjs`；防护层只做本地内容处理，
不联网、不读凭据、不代你调上游。真机接入仍只有原插件能做（`vendor/channel-pack/pack.js`）。

## 依赖

`react` / `react-dom` / `scheduler` 19.2.3，已 vendored 进 `node_modules/`。
**打包时请带上它**：这台机器没有包 registry，删掉无法还原。只想要演示页的话，单拷 `dist/` 即可。
