# WeChat Agent（Weixin 4.1.x 原型）

这是一个 Windows 本机原型，直接接入桌面版 Weixin 的测试群。它使用 `uiautomation` 读取可见消息并发送文本，记忆层使用 SQLAlchemy，支持 SQLite 本地试跑和 PostgreSQL 连接串。PostgreSQL 部署时可在迁移阶段加入 pgvector；当前原型先把群/成员隔离、事实版本和时间召回跑通。

当前适配目标是 Weixin `4.1.x`。本机已核验到 `4.1.15.56`。原始 `wxauto` GitHub 项目声明支持 3.9.X，因此这里没有把它作为 4.1.x 运行时依赖；`LTEnjoy/easyChat` 的 README 明确列出 4.1.15 和群聊/@，但仓库没有可确认的开源许可证，本项目只参考其公开的版本选择和 UI 结构，不复制其代码。

## 安全开关

默认配置不会发送消息：

- `WECHAT_ALLOW_SEND=false`
- `WECHAT_GROUP_NAME` 为空时拒绝打开会话
- 只处理 `WECHAT_GROUP_NAME` 指定的群
- `state/STOP` 存在时立即停止监听
- 所有发送动作写入 `action_audit`
- 发送没有每日硬额度，但有每分钟异常速率熔断；白名单、审计和停机开关始终生效
- 普通问句不会触发模型；只有 `@AI` 或配置的 AI 别名进入回复流程
- 已成功回答的问题写入持久化 `reply_records`，重启后也不会重复回答

专用测试号准备好后，先复制 `.env.example` 为 `.env`，只填写测试群名称和模型密钥：

```powershell
Copy-Item .env.example .env
# 编辑 .env：WECHAT_GROUP_NAME=你的测试群
python -m pip install -r requirements.txt
python -m wechat_agent init-db
python -m wechat_agent ready
```

`ready` 只检查 Weixin 窗口，不打开群、不发送消息。确认窗口和群名称正确后，才显式开启发送：

```powershell
Remove-Item state/STOP -ErrorAction SilentlyContinue
python -m wechat_agent run --allow-send
```

本机桌面控制台：

```powershell
python -m wechat_agent desktop --open-browser
```

抖音和小红书浏览器连接默认关闭。需要启用时，在 `.env` 中设置
`PLATFORM_BROWSER_ENABLED=true`，桌面控制台会分别打开两个持久化浏览器目录；评论和回复仍默认只入队，只有在
`PLATFORM_WRITE_ENABLED=true` 并在控制台二次确认后才会尝试发送。

平台评论或回复中的 `@` 目标必须在对应白名单中。白名单只保存平台唯一用户 ID，昵称不能作为授权主键：

```ini
DOUYIN_ACCOUNT_ID=douyin-main
XIAOHONGSHU_ACCOUNT_ID=xhs-main
DOUYIN_MENTION_ALLOWLIST=抖音用户ID1,抖音用户ID2
XIAOHONGSHU_MENTION_ALLOWLIST=小红书用户ID1
```

`ACCOUNT_ID` 只是本地审计命名，不是登录账号、密码或 Cookie；登录态由各自可见浏览器扫码保存。桌面端创建 @ 动作时，文本里的 `@昵称` 必须同时带 `mention_ids`，服务层只核对唯一 ID 是否在对应白名单；没有 ID 的 @ 文本会拒绝入队。平台原生提及候选选择器尚未完成真实页面验收，当前真实写入会安全阻断这类动作。

## 联网问答和平台内容

可在 `.env` 中显式开启联网研究。`WEB_SEARCH_PROVIDER=deepseek-search-api` 时直接复用 DSH 的局域网搜索中转：

```ini
WEB_SEARCH_ENABLED=true
WEB_SEARCH_PROVIDER=deepseek-search-api
WEB_SEARCH_BASE_URL=http://192.168.20.38:8787/v1/tools
WEB_SEARCH_API_KEY=lan
WEB_SEARCH_MAX_RESULTS=5
WEB_FETCH_ENABLED=true
WEB_FETCH_ALLOWED_DOMAINS=example.com,docs.example.com
```

请求会发到 `POST /v1/tools/search`；网页正文继续使用本地 HTTP 和域名白名单，不使用 Exa 或官方 DeepSeek Messages 搜索。也可以选择 `brave`、`tavily` 或自建 `searxng`。搜索结果、网页正文和抖音/小红书公开评论只进入当前问题上下文，不写入群友长期画像。

同一个 agent 还支持可选的第二个生图模型。把 `IMAGE_MODEL_ENABLED=true`、`IMAGE_MODEL_BASE_URL`、`IMAGE_MODEL_API_KEY` 和 `IMAGE_MODEL` 配好后，明确 @AI 的“生成图片/画一张/生图”消息会调用 `POST /v1/images/generations`，文字问题仍走 `LLM_*`。当前微信连接器会把生成 URL 作为文字发出，图片消息发送需要后续补 UI 适配。

当 `PLATFORM_BROWSER_ENABLED=true` 且平台浏览器已人工登录时，群友明确询问“抖音/小红书”内容会读取当前公开条目和公开评论。遇到验证码、安全验证、IP 风险或登录失效会停止，不绕过平台限制。完整变量说明见 [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)，候选 GitHub 项目核验见 [`docs/OPEN_SOURCE_RESEARCH.md`](docs/OPEN_SOURCE_RESEARCH.md)。

默认只监听 `127.0.0.1:8765`。需要从其他机器访问时，设置 `DESKTOP_TOKEN`，再通过反向代理暴露；控制台的启动、停止、回复台账和动作审计都使用同一套发送策略。

停止方式：

```powershell
python -m wechat_agent --stop
```

当前账号尚未进行真实发送测试。等专用测试号准备好后，先运行 `ready`，再做一条由你指定的测试群 @AI 消息回放；每一步都会先查看审计记录和运行日志。

## 数据边界

`messages` 保存原始消息 30 天；`memory_facts` 长期保存事实和画像，记录 `observed_at`、`valid_from`、`valid_to`、置信度、确认状态和来源事件；检索按 `account_id + group_id + member_id` 强制过滤。聊天、兴趣、稳定事实分别使用 14、90、365 天半衰期。默认记忆库使用关键词和时间衰减，不需要向量模型；`embedding_json` 与 pgvector 迁移是后续可选扩展。Mem0、Graphiti、Letta/LangMem 可借鉴其事实抽取和更新策略，但不能绕过本项目的群/成员隔离与审计边界。

平台账号绑定接口预留在 `identity_bindings`，绑定必须由群友明确确认；同一平台身份可以映射到多个群，但记忆和画像不会跨群直接混用。

## 目录

- `src/wechat_agent/weixin_ui.py`：Weixin 4.1.x UIAutomation 适配器
- `src/wechat_agent/agent.py`：群消息、画像抽取、RAG 和回复编排
- `src/wechat_agent/storage.py`：消息、事实、绑定和动作审计
- `src/wechat_agent/policy.py`：群白名单、@AI、主动回复和停机策略
- `src/wechat_agent/desktop.py`：本地 Web 桌面控制台
- `src/wechat_agent/platforms/`：抖音/小红书统一适配层；默认 dry-run，可显式开启 Playwright 可见浏览器
- `docs/PLATFORM_ADAPTERS.md`：抖音/小红书浏览器登录、滚动读取、评论队列和安全边界
- `docs/DEPLOYMENT.md`：Windows 微信连接器与服务器核心的 24 小时拓扑
- `tests/`：不连接微信的隔离、时间召回和策略测试

## 已知限制

- Weixin UI 结构随客户端版本变化；4.1.x 版本需在专用号上实测消息读取和发送。
- UIAutomation 只能读取当前可见聊天内容，历史加载和媒体消息需要后续适配。
- 个人微信账号存在平台协议和封禁风险；本项目不承诺普通个人微信群长期稳定运行。

服务器 24 小时运行建议采用“服务器核心服务 + Windows 微信连接器”拓扑：微信 UIAutomation 仍需在带桌面会话的 Windows 主机保持登录，服务器负责模型、台账、平台任务和控制台。当前代码没有执行远端部署，也不会把微信登录态迁移到服务器。
