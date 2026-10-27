# 环境变量说明

`.env` 只放在运行机器上，不要提交到 Git。抖音/小红书的 `ACCOUNT_ID` 是本地审计命名空间，不是登录账号、密码、Cookie，也不会切换浏览器登录态；登录态保存在各自的 `browser-data/<platform>` 目录，由操作者在可见浏览器中扫码完成。

## 平台用户白名单

`DOUYIN_MENTION_ALLOWLIST` 和 `XIAOHONGSHU_MENTION_ALLOWLIST` 只填写平台唯一用户 ID，多个 ID 用逗号分隔：

```ini
DOUYIN_ACCOUNT_ID=douyin-main
XIAOHONGSHU_ACCOUNT_ID=xhs-main
DOUYIN_MENTION_ALLOWLIST=7123456789012345678,9876543210987654321
XIAOHONGSHU_MENTION_ALLOWLIST=5f6a7b8c9d00112233445566
```

昵称不参与授权，因为昵称可以重复或变化。桌面端计划动作需要同时提交：

- 文本里的 `@昵称`，用于让操作者确认要提及谁；
- `mention_ids`，即从当前平台页面或运营台账选出的唯一用户 ID。

服务层只核对 `mention_ids` 是否在对应平台白名单里。没有 ID 的 `@昵称` 会被拒绝；平台原生候选选择器尚未在真实抖音和小红书页面完成验收，因而当前真实写入路径会安全阻断这类动作。

## 微信引用和最近上下文

微信消息满足以下任一条件才会进入回复路径：显式 `@` 机器人名称、消息开头的机器人名称，或命中 `WECHAT_KEYWORDS` 任意关键词。设置了 `WECHAT_SELF_NAME` 时，登录账号昵称会自动作为一个机器人名称。机器人名称不会因为出现在普通句子中而触发，因此回答里的“我是 AI”不会再次触发。若消息是微信的“引用/回复”消息，UI 适配器会尽量读取引用块中的发送者和正文，并以 `<wechat_reply_to>` 来源块传给模型；最近 `WECHAT_CONTEXT_MESSAGES` 条同群消息也会以 `<wechat_context>` 传入。引用内容和历史内容都按不可信资料处理，不能改变模型规则。不同引用内容会参与去重哈希，因此引用不同前文的问题不会被误判成同一个问题。

微信 4.1.x 冷启动时可能只暴露 `Qt51514QWindowIcon` 外壳。`wechat-agent` 会通过 `wechatauto-replica==1.2.7.0` 先热激活 Qt accessibility gate，再检查 `mmui::MainWindow` 和 `MainView`；依赖缺失或热激活失败时才提示开启 Windows 讲述人/辅助功能模式。

```ini
WECHAT_CONTEXT_MESSAGES=12
WECHAT_CONTEXT_MAX_CHARS=6000
WECHAT_AI_ALIASES=AI,ai,机器人
WECHAT_KEYWORDS=问AI,小助手
```

微信不同版本可能只在 UIAutomation 树中暴露“引用”按钮而不暴露原文。当前实现是 best-effort 解析；如果版本没有暴露引用正文，需要在运行机器上根据控件树再补选择器。

监听目标由 `WECHAT_GROUP_NAME` 解析出的群 wxid 固定下来，正常路径使用 `WeChatDB.Listener`，所以你把微信窗口切到别的群也不会改变监听对象。只有微信本地消息库不可读时才使用 UIAutomation 回退；回退每轮会检查当前会话名称，无法确认是配置群就跳过该轮。数据库消息里的 `sender_username` 是成员唯一微信 ID，`get_nickname()` 只用于显示备注/昵称；`WECHAT_ALLOWED_MEMBERS` 应填写这些 ID，不要填写昵称。

```ini
WECHAT_ALLOWED_MEMBERS=成员微信唯一ID1,成员微信唯一ID2
```

## 联网问答

默认关闭。开启后，只有明确的实时/搜索类问题才调用搜索服务，普通聊天不会因为 `@AI` 而自动联网：

```ini
WEB_SEARCH_ENABLED=true
WEB_SEARCH_PROVIDER=deepseek-search-api
WEB_SEARCH_API_KEY=lan
WEB_SEARCH_BASE_URL=http://192.168.20.38:8787/v1/tools
WEB_SEARCH_MAX_RESULTS=5
WEB_FETCH_ENABLED=true
WEB_FETCH_ALLOWED_DOMAINS=
WEB_FETCH_MAX_CHARS=6000
WEB_TIMEOUT_SECONDS=15
```

支持的 provider：

- `brave`：使用 Brave Web Search API，需要 API key；
- `tavily`：使用 Tavily Search API，需要 API key；
- `searxng`：使用自建 SearXNG JSON 接口，`WEB_SEARCH_BASE_URL` 填实例地址，通常不需要 API key；
- `deepseek-search-api`：使用 DSH 同款局域网 DeepSeek 搜索中转。`WEB_SEARCH_BASE_URL` 填
  `http://192.168.20.38:8787/v1/tools`，客户端 key 填占位值 `lan`。实际请求是
  `POST /v1/tools/search`，只用于搜索；网页正文仍由本机 HTTP 抓取；
- `none`：不进行网页搜索。

网页正文抓取只需要打开 `WEB_FETCH_ENABLED`。`WEB_FETCH_ALLOWED_DOMAINS` 留空时允许抓取所有公开 HTTP(S) 网站；填入域名后才会收窄范围。群友也可以在 @AI 消息中直接给出 `https://...` URL。程序仍会阻断 localhost、内网、回环、链路本地、保留地址和 `.local` 域名，并限制正文长度。搜索结果和正文只进入当前回答上下文，不会被当作群友事实或人物画像写入 `memory_facts`。

## 多模型和生图

文字问答继续使用 `LLM_*`。同一个 agent 收到“生成图片/画一张/生图”等明确 @ 请求时，才会路由到可选的第二个 OpenAI-compatible 生图服务：

```ini
IMAGE_MODEL_ENABLED=true
IMAGE_MODEL_BASE_URL=https://你的生图网关/v1
IMAGE_MODEL_API_KEY=在运行机器本地填写
IMAGE_MODEL=你的生图模型名
IMAGE_MODEL_SIZE=1024x1024
IMAGE_MODEL_RESPONSE_FORMAT=url
IMAGE_MODEL_TIMEOUT_SECONDS=120
```

接口约定为 `POST /v1/images/generations`。如果开启上传，程序会把生成的远程 URL 或 `b64_json` 文件下载/读取后，直接上传到阿里云 OSS，再把公开或签名 URL 作为微信文本回复：

```ini
IMAGE_UPLOAD_ENABLED=true
OSS_REGION=cn-shenzhen
OSS_ENDPOINT=https://oss-cn-shenzhen.aliyuncs.com
OSS_BUCKET=你的-bucket
OSS_ACCESS_KEY_ID=运行账号的 AccessKey ID
OSS_ACCESS_KEY_SECRET=运行账号的 AccessKey Secret
OSS_PREFIX=wechat-agent-assets
# 私有 bucket 留空；程序会生成有效期为 7200 秒的签名 URL。
OSS_SIGNED_URL_TTL_SECONDS=7200
# 公共 bucket 或 CDN 域名；填写后返回长期可访问的公开 URL。
OSS_PUBLIC_BASE_URL=https://media.example.com
IMAGE_UPLOAD_TIMEOUT_SECONDS=120
IMAGE_UPLOAD_MAX_BYTES=20000000
```

`OSS_PUBLIC_BASE_URL` 为空时使用私有 bucket 的签名 GET URL；要长期公开访问，应使用公开 bucket/CDN 域名。`OSS_ACCESS_KEY_ID` 和 `OSS_ACCESS_KEY_SECRET` 只放在运行机器的 `.env`，不要提交到仓库。未开启上传时，远程 URL 仍直接回复，`b64_json` 会保存到 `state/generated/`。

## 可配置人设

人设只包裹回答语气，不改变群/成员隔离、工具结果、事实准确性和安全规则。默认已内置你提供的 [dafeiyu-persona](https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona) 适配版：

```ini
PERSONA_ENABLED=true
PERSONA_NAME=大肥鱼
PERSONA_FILE=personas/dafeiyu/SKILL.md
PERSONA_SOURCE_URL=https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona
PERSONA_MAX_CHARS=12000
```

把 `PERSONA_FILE` 改成自己的 Markdown 文件即可添加其他人设；同目录下的 `references/lines.md` 和 `references/lore.md` 会作为风格参考一起读取。修改配置后在桌面 UI 点击“保存并重载”，或重启监听进程。人设文件不会在运行时从 URL 下载，`PERSONA_SOURCE_URL` 只是来源记录。

## 记忆库和向量模型

当前默认是按 `account_id + group_id + member_id` 隔离的 SQL 事实表，使用关键词重合、置信度和时间衰减召回，不需要向量模型即可运行。`embedding_json` 和 pgvector 迁移是预留接口，尚未宣称已启用语义向量检索。Mem0、Graphiti、Letta/LangMem 可以参考事实抽取、更新和语义召回，但接入前仍需保留本项目的群/成员隔离、来源审计和删除策略，不能直接把外部记忆库当作跨群共享存储。

## 运行边界

`PLATFORM_BROWSER_ENABLED=true` 只在有桌面会话的 Windows 机器上启用抖音/小红书可见浏览器。服务器可以长期运行 LLM、搜索、数据库和任务编排；微信 UIAutomation 以及需要可见浏览器登录态的平台采集仍需要 Windows 连接器在线。
