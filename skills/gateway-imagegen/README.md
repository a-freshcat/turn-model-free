# gateway-imagegen

独立技能，需要 Python 3.10+，无 pip 依赖。将 gateway-imagegen 文件夹导入客户端的技能目录，配置 GATEWAY_BASE_URL、GATEWAY_API_KEY，可选 GATEWAY_IMAGE_MODEL，再调用此技能。

可接入自建或经授权共享的 Images 兼容网关；不附送账号/配额，不需要 Google 登录态，也不要求下载服务端项目。

安装与共享说明：https://github.com/a-freshcat/turn-model-free/blob/v1.2.13/docs/standalone-skills-and-sharing.md

也可使用技能目录内的私有 config.json 配置 base_url、api_key 和 model；该文件不应上传或分发。
