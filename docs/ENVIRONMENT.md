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

## 联网问答

默认关闭。开启后，只有明确的实时/搜索类问题才调用搜索服务，普通聊天不会因为 `@AI` 而自动联网：

```ini
WEB_SEARCH_ENABLED=true
WEB_SEARCH_PROVIDER=brave
WEB_SEARCH_API_KEY=在运行机器本地填写
WEB_SEARCH_BASE_URL=
WEB_SEARCH_MAX_RESULTS=5
WEB_FETCH_ENABLED=false
WEB_FETCH_ALLOWED_DOMAINS=example.com,docs.example.com
WEB_FETCH_MAX_CHARS=6000
WEB_TIMEOUT_SECONDS=15
```

支持的 provider：

- `brave`：使用 Brave Web Search API，需要 API key；
- `tavily`：使用 Tavily Search API，需要 API key；
- `searxng`：使用自建 SearXNG JSON 接口，`WEB_SEARCH_BASE_URL` 填实例地址，通常不需要 API key；
- `none`：不进行网页搜索。

网页正文抓取需要同时打开 `WEB_FETCH_ENABLED` 并设置域名白名单。搜索结果和正文只进入当前回答上下文，不会被当作群友事实或人物画像写入 `memory_facts`。

## 运行边界

`PLATFORM_BROWSER_ENABLED=true` 只在有桌面会话的 Windows 机器上启用抖音/小红书可见浏览器。服务器可以长期运行 LLM、搜索、数据库和任务编排；微信 UIAutomation 以及需要可见浏览器登录态的平台采集仍需要 Windows 连接器在线。
