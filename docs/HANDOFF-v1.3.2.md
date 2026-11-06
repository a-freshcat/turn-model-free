# 交接：turn-model-free v1.3.2 待办

> 上一对话结束时的状态。本文件是**交接单**，不是长期文档；做完可删。
> 最后更新：2026-11-07，HEAD `dd2910c`。

## 一、仓库与远端现状

| 项 | 值 |
|---|---|
| 仓库 | `C:\Users\handsomeboys\Documents\deepseek-harness\repair_question\turn-model-free` |
| 远端 | https://github.com/a-freshcat/turn-model-free |
| 分支 / HEAD | `main` @ `dd2910c`（已推送，工作树干净） |
| 已发布 | `v1.3.1`（tag `v1.3.1` → `e4af8e4`） |
| 版本号 | `package.json` 仍是 `1.3.1`，下一版应为 **1.3.2** |

`v1.3.1` 的四项资产（desktop/web 各含 zip 与 sha256）已上传，哈希与本地一致。

## 二、必须先处理：v1.3.1 有已知缺陷

### 缺陷 A（严重）：会打断 Qoder 渠道

`v1.3.1` 里 `gateway/scripts/pack_bridge.mjs` 会自动把 `pack.js` 复制到
`gateway/.pack/` 打补丁后加载。但 `pack.js` 按**自身所在目录**读取同级资源：

```
~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/vendor/channel-pack/
  pack.js
  qoder-auth-wasm.wasm   ← 副本目录里没有它
```

于是 Qoder 一用就报：

```
ENOENT: no such file or directory, open '...\plugin_antigravity\.pack\qoder-auth-wasm.wasm'
```

**已在 `dd2910c` 修复**：默认只加载插件目录里的原件，删除 `.pack` 副本，
`DSH_PACK_PATCHED` 仅作显式实验用；备用网关也不再注入副本路径。
本地运行目录已同步并验证（Qoder 对话 200、`/v1/models` 64 个）。

**但 GitHub 上 `v1.3.1` 仍是坏的** → 需要发 `v1.3.2` 带上这个修复。
发版流程见第五节。

### 缺陷 B（体验）：控制台轮询太吃接口

`gateway/web/console.mjs` 第 1170 行：

```js
setInterval(() => { if (connected) void load() }, 15_000)
```

`load()`（第 891 行起）每轮会发：

- `/ui/quota`、`/ui/state`、`/ui/protection`、`provider.status`（本机，便宜）
- 每个有账号的渠道 `account.list`（本机）
- **每个有账号的渠道 `credits.balances`（真打上游接口）**

6 个渠道有账号时约 14 次调用 / 15 秒 ≈ **56 次/分钟**，其中积分查询是真实外部请求。
这既耗配额，也是 Raccoon 频繁撞 429 的放大器。

建议改法（尚未实施）：

1. 冷数据（账号、积分、模型）启动拉一次，之后 5 分钟或手动刷新；
2. 热数据（在线状态、并发、倒计时）保持短轮询，不走上游；
3. `document.hidden` 时暂停轮询；
4. 积分单独加"仅手动/到期刷新"开关。

## 三、用户明确提出的需求（未做）

### 1. 免 CMD 的 EXE 启动器（"像 app 端那样用快捷方式启动，其他隐藏在后端"）

这是本对话最后一项需求，**尚未实现**。已确认可行方案：

- 用系统自带 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`
  编译 `/target:winexe` 的 C# 启动器（无需安装 .NET SDK，实测编译器可用）
- 它用 `pythonw.exe` 隐藏拉起 `gateway\launch_desktop.py --port 8199`，
  无控制台窗口；关闭窗口时进程一并退出
- 生成桌面 `.lnk` 指向该 exe（`WScript.Shell` COM）
- 图标建议自己做（不要拿 Antigravity 的第三方图标当本项目图标）
- **`.cmd` 要保留**作排障入口；Key 在界面「接入设置」里可复制

注意：不要再用"把 stdout 打到控制台"的思路，用户要的是完全无黑框。

### 2. Raccoon 冷却退避补丁（已写好但**默认关闭**）

- 脚本：`gateway/scripts/patch-raccoon-cooldown.mjs`
- 行为测试：`gateway/scripts/test-raccoon-cooldown.mjs`（15 项全过）
- 语义：`pack.js` 里 Raccoon 是唯一用指数退避的渠道（30s→…→10min 封顶），
  而档位只在收到 200 时归零；账号被冷却后拿不到 200，档位单向上涨。
  补丁改为"冷却**到期后**重新选中并收到 429 时降一级"，同期内并发 429 仍递增。
- **当前状态：不加载。** 若要启用，必须解决缺陷 A 的同级资源问题——
  正确做法是**整目录复制**（含 wasm）到隔离路径再打补丁，而不是只复制 `pack.js`。
  这条要单独验证，不要和发版混在一起。

## 四、本对话已完成的部分（不要重做）

- 控制台 UI 全量重写：`gateway/web/console.{html,css,mjs}` + `connect.html`，
  淡粉纸感、原创设计，`/` 与 `/quota` 均指向它
- 号池合并本机 Antigravity 与插件账号（10 个账号全显示）
- `reset_at` epoch 归一化（修复本地化日期 `6/11/2026` 被按美式解析导致的假"已到时间"）
- 凭据状态未知时不再误删账号（`web_api.py`）
- 渠道下线 `trae`（字节）与 `loomy`（讯飞）：`CHANNELS` 已移除，`RETIRED_CHANNELS` 保留兼容
- 备用网关 `gateway.ensure`（端口 18326，绝不碰 DSH 的 8326），修复对话 503 ConnectError
- `main.py` 已恢复 HEAD 的调用保护链（SSE 观察器、`observe_error_value` 等）
- 启动横幅打印地址与 Key；诊断日志提示不再是 `WARNING:`
- 发布脚本：`scripts/prepare_release.py`

## 五、发版流程（v1.3.2）

```powershell
cd C:\Users\handsomeboys\Documents\deepseek-harness\repair_question\turn-model-free

# 1) 改版本号（package.json / package-lock.json / README / CHANGELOG）
# 2) 跑测试
$env:PYTHONPATH='C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity\.vendor'
cd gateway; C:\Python314\python.exe -m pytest tests -q; cd ..
node scripts/test-all.mjs          # 期望 79/79

# 3) 打包（.vendor 从已装目录取，或沿用旧包）
C:\Python314\python.exe scripts/prepare_release.py --vendor-dir gateway\.vendor
#   若本地无 gateway\.vendor：
#   ... --vendor-archive .release\turn-model-free-desktop-v1.3.0.zip

# 4) 提交推送
git add -A; git commit -m "release: v1.3.2 ..."; git push origin main
git tag v1.3.2; git push origin v1.3.2

# 5) 发布
gh release create v1.3.2 `
  ".release/turn-model-free-desktop-v1.3.2.zip" `
  ".release/turn-model-free-desktop-v1.3.2.sha256.txt" `
  ".release/turn-model-free-web-v1.3.2.zip" `
  ".release/turn-model-free-web-v1.3.2.sha256.txt" `
  -R a-freshcat/turn-model-free --title "v1.3.2 — ..." --notes-file .release/release-notes-v1.3.2.md --latest
```

发版后校验：`gh release view v1.3.2 -R a-freshcat/turn-model-free --json assets --jq '.assets[]|{name,size,digest}'`
（`isLatest` 不是合法字段，会报 `Unknown JSON field`）。

## 六、环境要点（踩过的坑）

- 运行目录与仓库是**两棵树**：
  `C:\Users\handsomeboys\Documents\ChatGPT\plugin_antigravity`（跑着的实例）
  改完网关代码要同步过去并重启，否则看不到效果
- 网关端口 8199，Key 在 `plugin_antigravity\.gateway-api-key`
- 会话登录：`POST /ui/session {"key": ...}`，必须带 `Origin: http://127.0.0.1:8199`
  （跨源 POST 现在明确回 403）
- 插件包路径：`~/.dsh/profiles/desktop/node_modules/dsh-our-free-model/vendor/channel-pack/pack.js`
- pytest 要在 `gateway/` 目录下跑，并设 `PYTHONPATH` 指向 `.vendor`
- Node 侧旧 UI 的 79 项测试里，`provenance` 有一项在插件升级后会**跳过**
  （归档演示页来源校验，不是当前控制台的构建输入）
- 不要给 `launch_desktop.py` 用 `logging` 输出到控制台：uvicorn 启动会重配
  logging，消息会被吞掉；控制台提示一律用 `print`
- `pythonw.exe` 无 stdout/stderr；这就是它藏起输出、也藏起 Key 的原因

## 七、工作区卫生

本对话已清理：根目录 28 个临时脚本/夹具、根 `.pytest_cache`、
`gateway/.pack`（两处）。排查报告已归档到 `docs/tool-call-echo-bug-report.md`。

**以下不属于本对话，请勿删除**：`_probe/`、`free-m-clone` 相关、
各类 `*-werewolf-*.mjs`、`main_repai/`、其他 `conversation-*` 目录。
