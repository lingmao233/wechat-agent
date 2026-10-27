# 微信 AI Agent 当前状态

更新时间：2026-10-27
结论：**离线核心、本地桌面控制台、受控联网研究和可选的平台浏览器连接器已完成，真实微信端到端联调尚未通过。** 抖音/小红书连接器支持人工登录、有限滚动读取和人工确认动作队列；当前没有替用户发送微信消息或平台评论。

## 已完成的代码

| 文件 | 已完成内容 |
|---|---|
| [`src/wechat_agent/weixin_ui.py`](src/wechat_agent/weixin_ui.py) | Weixin 4.1.x 适配器；优先用 WeChatDB `Listener` 按群 wxid 读取并解析成员唯一 ID/昵称，UIAutomation 只作为校验当前群后的回退，另负责窗口、引用读取和文本发送 |
| [`src/wechat_agent/agent.py`](src/wechat_agent/agent.py) | 消息入库、聊天事实、简单兴趣抽取、时间召回、模型回复和动作编排 |
| [`src/wechat_agent/storage.py`](src/wechat_agent/storage.py) | SQLite/PostgreSQL SQLAlchemy 表、原文留存、事实、身份绑定、动作审计、过期清理、持久化 `reply_records` 事件幂等与问题去重 |
| [`src/wechat_agent/policy.py`](src/wechat_agent/policy.py) | 群白名单、成员白名单、显式 @/开头名称/关键词触发、普通消息跳过、发送开关、速率熔断、STOP 文件 |
| [`src/wechat_agent/llm.py`](src/wechat_agent/llm.py) | DeepSeek/OpenAI 兼容文字调用；没有 Key 时使用离线 stub |
| [`src/wechat_agent/image.py`](src/wechat_agent/image.py) | 可选 OpenAI-compatible 生图调用；可直接上传阿里云 OSS 并返回公开或签名 URL |
| [`src/wechat_agent/config.py`](src/wechat_agent/config.py) | `.env` 配置、30 天原文留存和 14/90/365 天半衰期参数 |
| [`src/wechat_agent/desktop.py`](src/wechat_agent/desktop.py) | 本地 Web 桌面 UI：启动/停止、配置分组编辑并校验重载、实时事件/回复/审计、抖音/小红书操作 |
| [`src/wechat_agent/platforms/`](src/wechat_agent/platforms/) | 抖音/小红书统一浏览、评论、回复接口；fixture dry-run、Playwright 可见浏览器、平台动作幂等台账、@ 白名单校验和挑战即停 |
| [`src/wechat_agent/web_research.py`](src/wechat_agent/web_research.py) | DSH 同款 DeepSeek relay、Brave/Tavily/SearXNG 搜索、公开网页正文抓取（可选域名收窄），以及当前已登录平台公开条目/评论的临时上下文 |
| [`migrations/001_pgvector.sql`](migrations/001_pgvector.sql) | PostgreSQL/pgvector 迁移草案 |
| [`tests/`](tests/) | 记忆隔离、时间召回、去重、策略和无 Key stub 测试 |

## 已验证结果

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q`：**23 passed**。
- `python -m compileall -q src`：通过。
- `python -m wechat_agent init-db`：输出 `database schema initialized`。
- 本机已安装并可导入 `uiautomation==2.0.18`、`pyperclip`、`pytest`、`python-dotenv`。
- 微信进程路径为 `C:\Program Files\Tencent\Weixin\Weixin.exe`，此前核验版本为 `4.1.15.56`。

直接运行 `pytest -q` 时，当前 Python 全局环境会自动加载一个与本项目无关的旧 `web3` pytest 插件，并在 Python 3.11 中因 `inspect.getargspec` 导入失败。禁用外部 pytest 插件后，项目测试通过；这不是项目测试代码本身的失败。

## 已做的真实微信检查

### 做过的只读动作

- 枚举过微信主窗口类名、进程号和 UIAutomation 子树。
- 检查过 Windows 讲述人进程和微信进程。
- 启动过 `ready`，只做窗口就绪检查。
- 重启过微信客户端以让辅助功能树重新加载。

### 没有做过的动作

- 没有打开或切换测试群。
- 没有读取群聊天内容。
- 没有发送、@、评论或私信。
- 没有复制或保存微信密码、Cookie、二维码登录凭据。

### 当前阻塞

1. 讲述人曾被启动，但最近一次进程检查没有发现 `Narrator.exe`。
2. 微信登录页曾被识别为 `mmui::LoginWindow`，说明重启后没有处于可用聊天态。
3. 进入主窗口后，修改前的 UIAutomation 查询只看到 `Qt51514QWindowIcon` 和渲染窗格，未暴露适配器需要的 `MainView`；现已接入 `wechatauto-replica` 的 accessibility gate 热激活步骤。
4. 依赖安装后需要重新运行 `python -m wechat_agent ready`；在重新核对前，仍不能宣称已经进入读取消息和发送验证阶段。

## 当前可以做什么

### 已可离线运行

- 用构造的消息事件测试群/成员隔离。
- 生成聊天事实和简单兴趣事实。
- 按时间半衰期和关键词召回。
- 在无模型密钥时生成 stub 回复。
- 通过 `reply_records` 持久化事件幂等、已回答问题和失败重试状态。
- 通过 `python -m wechat_agent desktop` 使用本地控制台查看状态、台账和审计。
- 通过 `python -m wechat_agent desktop --open-browser` 或 `start_desktop_ui.cmd` 打开可操作桌面 UI；配置中心可以编辑 `.env`，保存后自动重载监听线程，事件面板每 2 秒刷新。
- 微信监听已固定到 `WECHAT_GROUP_NAME` 对应的群 wxid；成员日志使用 `sender_username` 和联系人备注/昵称，不再把所有成员归为 `unknown`。`WECHAT_ALLOWED_MEMBERS` 按唯一 ID 校验。
- 通过平台 dry-run 适配器验证浏览、评论/回复计划和幂等键；尚未连接真实平台浏览器会话。
- 设置 `PLATFORM_BROWSER_ENABLED=true` 后，可在桌面控制台打开独立的抖音/小红书持久化浏览器，滚动读取公开内容和带稳定 DOM ID 的评论；评论/回复需 `PLATFORM_WRITE_ENABLED=true` 且二次确认。
- `DOUYIN_MENTION_ALLOWLIST` 和 `XIAOHONGSHU_MENTION_ALLOWLIST` 只按平台唯一用户 ID 配置；动作请求必须同时提交 `mention_ids`，服务层只按 ID 核对并把已核对的 ID 写入动作元数据。
- 设置 `WEB_SEARCH_ENABLED=true` 后，明确的实时/搜索类 @AI 问题可以调用 DSH 同款 `deepseek-search-api`（`POST /v1/tools/search`）、Brave、Tavily 或自建 SearXNG；网页正文继续使用本地 HTTP。`WEB_FETCH_ALLOWED_DOMAINS` 留空时允许公开 HTTP(S) 网站，填写后才收窄范围；本机和内网地址仍会阻断。搜索结果和平台公开内容只进入当前回答上下文，不写入 `memory_facts`。
- 设置 `IMAGE_MODEL_ENABLED=true` 并填写独立的 `IMAGE_MODEL_*` 后，同一个 agent 的生图意图会调用 `/v1/images/generations`；再开启 `IMAGE_UPLOAD_ENABLED=true` 并配置 `OSS_*` 后会直接上传 OSS，再回复公开或签名 URL。
- 微信引用/回复消息会尽量解析为 `<wechat_reply_to>`，最近同群消息会进入 `<wechat_context>`；引用正文参与问题去重。
- 审计允许和拒绝的发送动作。
- 用 STOP 文件停止监听。

### 当前还不能宣称可用

- 不能宣称已完成真实微信群自动回复。
- 不能确认 4.1.15.56 上消息行选择器、输入框和发送按钮长期稳定。
- 不能确认真实客户端中自动 @ 选择和发送后的成功回执。
- 不能宣称已经接入 PostgreSQL 向量检索。
- 不能宣称已经建立完整结构化人物画像；当前是按账号/群/成员隔离的事实、兴趣和聊天记录，带来源、置信度、状态和时间衰减。
- 不能宣称已经完成抖音或小红书真实账号登录后的页面选择器验收。
- 不能宣称抖音或小红书真实评论/回复已经发送；当前需要专用账号逐平台扫码和人工确认。当前网络对小红书返回了“IP 存在风险”，连接器会停止，不会绕过。
- 当前平台连接器只完成唯一 ID 的白名单校验和记录；抖音/小红书原生 @ 候选选择器仍需逐平台真实页面验收，带 `mention_ids` 的真实写入会安全阻断，不能把普通文本 `@昵称` 当作已确认的平台提及。
- 联网搜索和公开平台读取仍是可选的当前请求上下文能力；没有搜索配置或浏览器登录态时会回退到普通 AI 回复，网页抓取可通过空域名列表允许公开站点或填写域名进一步收窄。DeepSeek relay 的健康检查已通过，但 wechat-agent 尚未做真实群端到端发送验收。微信引用解析和 OSS bucket 权限/URL 可访问性仍需用户本地实测。
- 本次生图上传改为直连 OSS 后只完成静态依赖、配置和差异检查，未运行测试、模型调用或真实 OSS 上传；需要在本地填入 `.env` 后试验。

## 下一步最小联调顺序

1. 使用专用测试号完成微信登录，并确认微信账号确实是准备好的测试号。
2. 在讲述人运行时重启微信，重新执行 `ready`。
3. `ready` 通过后，只打开指定测试群并读取一条可见消息。
4. 先保持 `WECHAT_ALLOW_SEND=false`，检查入库、画像和候选回复。
5. 由操作者在群内发送一条 `@AI` 测试消息，再单独开启发送开关。
6. 以微信客户端中实际出现的消息为发送成功标准，并核对 `action_audit`。
7. 发现异常时创建 `state/STOP`，再处理日志和选择器。

## 安全默认值

- `WECHAT_ALLOW_SEND=false`
- 普通消息不触发回复；显式 @、消息开头名称或 `WECHAT_KEYWORDS` 命中才触发
- 必须设置 `WECHAT_GROUP_NAME`
- `state/STOP` 存在时停止监听
- 发送动作写入 `action_audit`
- 只处理配置群和配置成员

详细需求与验收标准见 [`PRD.md`](PRD.md)。
