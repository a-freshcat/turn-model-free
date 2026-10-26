# 白嫖模型接入 · 页面抄写版 + 对比测试报告

> 目标：把 `dsh-our-free-model` 插件里「白嫖模型接入」那一页**连功能一起**抄下来，当备份和参考，
> 然后用测试证明抄出来的东西和原插件长得一样、点得动、不漂移。
>
> 结论先说：**能用了**。页面代码是逐字节抄的（不是重写），和已安装插件在同一 DOM / 同一 React /
> 同一份假后端下渲染出的树**逐行相同**；当前版本 77 条测试全绿；历史截图保留在 docs/ 下。
> 打成 zip 搬到别处解压后仍然通过（含防漂移检查），验证记录见第六节。

| 项 | 值 |
|---|---|
| 抄写来源 | `~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/client.js`（插件 v2.5.4） |
| 源码指纹 | sha256 `d5b22d78d2a76fd94bd828a77c3224d4318ca8e2abb5a7dd6aab3f3bf20cdb85` |
| 抄哪一段 | 第 4165–4861 行（`// ── 白嫖模型接入页` 到 `// ── 数据看板` 之前），90 672 字符 |
| 测试 | `npm test` → **77 / 77 通过**（最近一次完整验证） |
| 演示页 | 双击 `dist/index.html` 即可，无需服务器、无需联网 |

---

## 一、抄了什么、怎么抄的

「抄」有两种：重新照着写一遍，或者把原文搬过来。这里用第二种，因为你的目的是**读源码学怎么做**——
重写一遍就没有源码可读了，而且一旦手抄出错，后面所有「能不能用」的对比都失去意义。

| 产物 | 内容 | 来源 |
|---|---|---|
| `src/extracted/channels-page.js` | 整个页面：`ChannelsPage`、`ChannelCard`、13 张渠道卡的定义、渠道图标（base64 PNG）、RPC 传输层、登录轮询、余额/领取/模型开关全部逻辑 | client.js **4165–4861 行逐字节** |
| `src/extracted/channels.css` | 162 条样式规则（页面用到的全 55 个类名）+ 10 个动画 `@keyframes` + 它们所在的 `@media` | client.js 的 `CSS` 模板串，**按原文格式**摘出 |
| `src/extracted/channels-dict.js` | 70 条 `chan.*` 中文 + 70 条英文词条 | client.js 的 `DICT.zh` / `DICT.en` |
| `src/extracted/theme-tokens.css` | 428 个浅色 + 199 个深色设计令牌（`--dsw-alias-*`） | 桌面端 `app.asar` 里 `dsh-client-ui-theme` |
| `src/extracted/provenance.json` | 每一段的字符区间、行号、sha256 | 抽取时算出 |

四样东西互相印证：**字符区间 sha256** 说明「搬的是哪一块」；**生成物里能找到原文**说明「没被改过」；
**测试拿安装好的 client.js 重新切一遍再对哈希** 说明「搬运这件事本身没错」。

### 为什么抄出来能直接跑

原始代码作为插件的一部分，闭包引用了模块作用域里的东西（`h`、`useState`、`showToast`、`kilo` …
和 `window` 上的计时器）。抄写版没有为了跑起来去改原文，而是在外面套了一层工厂，把这些依赖**当参数传进去**：

```js
export function createChannelsPage(env) {
  const { h, Fragment, useState, useEffect, useMemo, useRef, useCallback,
          Pill, Switch, showToast, kilo, ago, post,
          setTimeout, clearTimeout, setInterval, clearInterval } = env
  /* ↓↓↓ 以下 90 672 字符是 client.js 原文，没有任何改动 ↓↓↓ */
```

所以文件里搜 `// ── 白嫖模型接入页` 就是插件里那一页的原样，可以直接当源码读。

### 抽取是脚本化的，不是一次性手工活

```bash
node scripts/extract.mjs          # 重新从已安装插件切一遍
node scripts/extract.mjs --check  # 只校验：抄写是否与插件一致（漂移就退出码 1）
node scripts/extract-theme.mjs    # 从 app.asar 里取设计令牌
```

插件升级后重跑一次就能跟上；`--check` 让「抄写悄悄过期」变成一条会红的测试。

---

## 二、能不能用：三层验证

### 第 1 层 · 长得一样（渲染树对比）

关键做法：把**已安装插件的 `ChannelsPage`** 和**抄写版的 `ChannelsPage`** 放进**同一个进程、同一个
迷你 DOM、同一份 React**，各自接一份独立但同样的假后端，然后逐行比渲染树。

用两个不同 DOM 库比是没意义的（差异可能来自库），所以两边共用同一个环境。

| 场景 | 结果 |
|---|---|
| 默认状态（13 张卡 + 自动签到条 + 顶部统计） | 595 行树 **逐行相同** |
| 展开「账号」折叠区（余额、到期、今日已领） | 逐行相同 |
| 展开「模型」折叠区（免费 / 已隐藏 / 已下架 / 已开启 n/m） | 逐行相同 |
| 首屏 15 次 RPC 的**方法名、参数、顺序** | 完全相同 |
| 英文界面、整页关闭（closed）等状态 | 相同 |

### 第 2 层 · 点得动（行为测试）

不是「渲染出来就算」，而是像用户那样**点真实按钮**，再断言页面怎么变、后端收到了什么：

| 操作 | 断言 |
|---|---|
| 添加账号 | 弹窗先开（`about:blank`）→ 拿到登录 URL 再跳 → 每 3 s 轮询 → 账号落池 → 页面出现「已接入：新账号 1」 |
| 恶意登录链接 `javascript:` | 不跳转、不渲染（只放行 http/https） |
| 测试连接 | 成功显示毫秒；失败显示上游原因 |
| 移除账号 | 取消框里点「取消」→ 一个 RPC 都不发；点「确定」→ `account.delete` 真发 |
| 停用/启用 | `account.update` 带 `{enabled:false}`，池子与卡片同步翻转 |
| 一键领取 | `credits.claimAll` → 「已领取 1 个账号，共 1000 积分」→ 余额真的涨了 → 强制重查余额 |
| 部分失败 | 成功数与失败原因一起显示 |
| 余额失败 | 卡片上「n 个账号未能查询余额」+ 每行显示上游原因 |
| 刷新余额 | 只发一次，且 `force: true` |
| 模型开关 | 单条 `model.setDisabled`；全部 `model.setAllDisabled` |
| 已下架模型 | 「恢复显示」走 `model.clearDead` |
| 自动签到开关 | 写回 pack，`aria-checked` 跟着变 |
| 宿主无 `connection` | 显示说明，13 张卡仍在但按钮全禁用，零 RPC |
| 渠道包挂载失败 | 显示原因 + 提示 + 骨架屏 |
| probe 失败 | 每次 4 s 重试 4 次，耗尽后把 RPC 错误显示出来 |
| ZCode | 渠道选择写进 `localStorage`，并作为 `zcodeProvider` 传给后端 |
| RPC 抛错 | 弹 toast（带渠道名与原因），页面不崩 |

### 第 3 层 · 交付物本身（`dist/` 冒烟）

`npm test` 会先构建，再**只加载构建产物 `dist/demo.js`** 跑一遍：13 张卡、7 个场景切换、
中英切换、浅深主题、四个控件按钮全点一遍。所以「源码测试过了但打包坏了」这种坑也堵住了。

### 浏览器里真的跑起来了

用 Edge 无头模式渲染 `dist/index.html`（`file://` 直接打开）：

![默认状态](screenshot-light.png)

![展开折叠区](screenshot-expanded.png)

![深色 + 余额失败场景](screenshot-dark.png)

13 张渠道卡、原版渠道 logo、余额数字、按钮排布、毛玻璃质感、滚动条都正常；
深色是另一套令牌，余额失败时每张卡下方出现红色提示——这都是页面自己的逻辑。

---

## 三、顺带发现的坑（抄的时候才看得见）

| 坑 | 现象 | 怎么处理的 |
|---|---|---|
| **`.ofm_root` 的变量块差点被漏掉** | 按「页面上出现的类名」筛样式，`.ofm_root` 不在其中（它由插件的外壳渲染）。但它声明了 `--ease-settle` / `--gap`——一漏，所有过渡动画和间距全失效 | 加了**变量闭包**：只要保留的规则 `var()` 了某变量，就把它所在的声明块一起保留 |
| 设计令牌不在插件里 | 页面风格全靠 `--dsw-alias-*`，这些在 DSH 外壳里。独立跑 = 没有颜色、没有毛玻璃 | `extract-theme.mjs` 从 `app.asar` 里把 627 个令牌抠出来 |
| `--dsw-alias-state-warning-primary` 不存在 | 令牌族实际叫 `state-warn-*`；插件 CSS 用的是 `warning` 这个名字 | 显式别名映射（不猜前缀），并写进测试覆盖 |
| `--chan-accent` 无声明 | 它是每张卡 `style={{'--chan-accent': accent}}` 内联注入的 | 测试里标为 runtime-only，并要求页面代码里确实有它 |
| 原 bundle 里三段文本重名 | 页面区域开始处又声明了一遍那三张表 | 区域从第一个组件起抄，三张表各自按自己的区间抄，避免重复声明 |
| 沙箱里没有浏览器 DOM | 没有 jsdom / linkedom，也没有外网 | 自己写了 `src/runtime/mini-dom.mjs`（含事件冒泡、选择器、样式代理），并满足 React DOM 的 `instanceof` 探测 |

---

## 四、目录与命令

```
free-m-clone/
├─ src/extracted/          # 抄写产物（AUTO-GENERATED，勿手改）
│  ├─ channels-page.js     # ← 91 186 字符，这一页的全部代码
│  ├─ channels.css         # 162 条规则 + 10 组动画
│  ├─ channels-dict.js     # 70×2 词条
│  ├─ theme-tokens.css     # 627 个设计令牌
│  └─ provenance.json      # 区间 / 行号 / sha256
├─ src/app/page.mjs        # 依赖注入装配（t 绑定、post、计时器）
├─ src/mock/pack.mjs       # 假渠道包：13 家渠道的 RPC 语义
├─ src/runtime/mini-dom.mjs# 够用的迷你 DOM
├─ src/test/               # 七套测试 + 迷你运行器
├─ src/demo/               # 演示页外壳（内容仍是抄来的页面）
├─ dist/                   # 双击 index.html 即可（无需服务器/联网）
├─ scripts/                # extract / extract-theme / build-demo / test-all
└─ docs/                   # 本报告 + 三张截图
```

```bash
npm test                 # 构建 + 77 条测试
npm run check:drift      # 抄写是否与已安装插件一致
npm run verify           # 上面两件事一起做
npm run extract           # 插件升级后重新抄一遍
```

打开演示页：直接双击 `dist/index.html`。右侧可切 7 个场景、中英、深浅色，并实时看到每次 RPC。
也支持深链，例如 `dist/index.html#scenario=fresh&open=both&only=CodeArts`。

---

## 五、打包 / 搬迁可用性（历史记录，旧版本数据）

问过「打成压缩包还能不能用」，所以是真的打包、解压到新目录、再跑一遍：

| 检查 | 结果 |
|---|---|
| `free-m-clone.zip` | 2.79 MB / 123 个条目，解压后 9.5 MB |
| 压缩包结构 | 顶层就是 `free-m-clone/`，解压后不会多套一层 |
| 解压到新路径后 `extract --check` | **exit 0**（不再误报漂移） |
| 解压后 `npm test`（含 `--with-drift`） | 旧版本验证 **46 / 46 通过** |
| 解压后 Edge 无头渲染 `dist/index.html` | 13 张卡与全部数据正常 |
| 只把 `dist/` 单独拷到空目录再打开 | 正常（`dist/` 自包含） |

打包过程中发现并修掉了一个真实缺陷：生成物里原本写的是**相对本目录**的源码路径
（`../../.dsh/profiles/...`），这个字符串会随文件夹层级变化——同一个插件版本，换个位置
`--check` 就会报「四个文件全部漂移」，而备份本身其实是好的。现在改成绝对路径 + `~` 缩写
（`~/.dsh/profiles/desktop/...`），并加了两条测试守着：生成物里不允许出现相对本目录的路径，
`dist/index.html` 里不允许出现绝对或远程资源。

> 提醒：zip 里**带着 `node_modules`**（react / react-dom / scheduler，约 7.5 MB 解压后）。
> 这台机器没有包 registry，删掉就没法还原，所以有意打进去。只想要演示页的话，拷 `dist/` 一个目录就够。

---

## 六、这份备份能干什么、不能干什么

**能**：当源码参考（就是原文）；当对照物（改插件前后比渲染树）；当离线演示（不碰真实凭据）；
当回归基线（插件升级后 `--check` + 77 条测试）；换机器换目录照常跑。

**不能**：它**不是**一个能登录真实渠道的插件。登录、领取、余额都来自 `src/mock/pack.mjs`，
不联网、不读 `~/.dsh/channel-pack/state.json`、不接触凭据。真机接入仍然只有原插件能做
（`vendor/channel-pack/pack.js`，2.9 MB，那一层没抄）。
