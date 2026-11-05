# 来源说明

腾讯提示词规则与 11128 识别思路参考 a-freshcat/free-m-clone：

- https://github.com/a-freshcat/free-m-clone
- 检查的 Git 树：03729233d039fff44ca34cf1c7cddb355c392099
- 源文件：src/guard/tencent-gate.js、src/guard/pipeline.mjs

源仓库 package.json 声明 MIT，README 标注防护层源于 dsh-our-free-model v2.5.4。
本项目保留来源信息；Python 实现与 FastAPI 网关为本次新增代码。

Python 清洗器重新实现内置规则，没有移植宿主环境绑定、用户自定义规则配置、
WeakSet 异常登记与 JavaScript 遥测。路径占位符冲突时保留原文；引号路径扫描限制在当前行。
本项目测试不代表已与原 JavaScript 全部测试逐条等价。
原项目的同号升档重发改为任务要求的立即冷却和换号。

## 页面来源

按用户指定，本次页面以本地源码
`原 free-m-clone 源码快照` 为准。
页面工厂、渠道组件、词典、CSS、设计令牌及含 React 的原运行包均逐字节复制到
`web/vendor/free-m-clone`，来源路径与哈希记录在 provenance.json。
该本地版本的这 8 个文件与前述 GitHub 快照一致；原始目录未作修改。

`scripts/build_ui.py` 在生成包中替换 Gemini 渠道的 ID、名称、说明与配额映射，
并移除该卡对旧 Gemini 图标的引用；组件使用其原有两字母图标回退。
原卡片布局、组件及 CSS 未改写。`web/app.mjs` 是本机服务适配层，
`web/app.css` 仅补充宿主和表单样式。

后续生成补丁还增加 Antigravity OAuth 提示、三个池的标签、第三池型号折叠，以及独立配额页入口；
原件仍不修改。`web/quota.*` 是新增页面，复用原 CSS 与设计令牌。

## Antigravity 协议参考

Google 原生应用 OAuth/PKCE 参考：
https://developers.google.com/identity/protocols/oauth2/native-app

公开客户端标识和协议字段参考 router-for-me/CLIProxyAPI：

- https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/auth/antigravity/constants.go
- https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/auth/antigravity/auth.go
- https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/auth/antigravity_models.go
- https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/runtime/executor/antigravity_executor_request.go

Python OAuth 状态管理、凭据存储、号池适配、转换驱动与配额 UI 为本项目实现。
默认公开 OAuth 应用标识并非用户凭据；不代表 Google 或 CLIProxyAPI 对本项目的认可。
上游许可原文保留如下（来源 https://github.com/router-for-me/CLIProxyAPI/blob/main/LICENSE）：

```text
MIT License

Copyright (c) 2025-2005.9 Luis Pater
Copyright (c) 2025.9-present Router-For.ME

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
