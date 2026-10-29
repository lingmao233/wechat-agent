# 微信 AI Agent 当前状态

更新时间：2026-10-28
结论：**离线核心、本地桌面控制台、受控联网研究和可选的平台浏览器连接器已完成；记忆清洗、滑动窗口画像、LLM 语义群画像、鲸鱼娘人设约束、本地训练数据导出与适度主动发言已落地，真实微信端到端联调尚未通过。** 抖音/小红书连接器支持人工登录、有限滚动读取和人工确认动作队列；当前没有替用户发送微信消息或平台评论。

## 已完成的代码

| 文件 | 已完成内容 |
|---|---|
| [`src/wechat_agent/weixin_ui.py`](src/wechat_agent/weixin_ui.py) | Weixin 4.1.x 适配器；优先用 WeChatDB `Listener` 同时注册多个群 wxid，解析成员唯一 ID/昵称，并从本地附件解密图片；UIAutomation 只作为校验当前群后的回退 |
| [`src/wechat_agent/agent.py`](src/wechat_agent/agent.py) | 消息入库、作用域记忆/画像抽取、线程/任务相关上下文过滤、群聊风格拟合、混合召回、模型回复和动作编排；常驻轮询时按配置每晚导出当天语料 |
| [`src/wechat_agent/storage.py`](src/wechat_agent/storage.py) | SQLite/PostgreSQL SQLAlchemy 表、原文和线程元数据留存、图片附件路径、事实版本/Embedding、用户/群级 AI/任务画像、OpenViking 桥接、身份绑定、动作审计、过期清理、持久化 `reply_records` 事件幂等与问题去重 |
| [`src/wechat_agent/embedding.py`](src/wechat_agent/embedding.py) | Ark 多模态和标准 OpenAI Embedding 合约、维度校验与安全错误边界 |
| [`src/wechat_agent/openviking_memory.py`](src/wechat_agent/openviking_memory.py) | 按账号/群/成员/任务 URI 写入、提交、语义检索和二次作用域过滤 |
| [`src/wechat_agent/profiles.py`](src/wechat_agent/profiles.py) | 用户候选记忆、群级 AI 画像和任务字段的确定性提取 |
| [`src/wechat_agent/textnorm.py`](src/wechat_agent/textnorm.py) | 统一清洗：wxid/已知发送者前缀、XML 占位、@AI 归一、系统行识别；原文与派生文本分离 |
| [`src/wechat_agent/prompt_profiles.py`](src/wechat_agent/prompt_profiles.py) | 画像投影为 ≤4,000 字自然语言摘要，丢弃 history/evidence/原始计数器 |
| [`src/wechat_agent/profile_synthesis.py`](src/wechat_agent/profile_synthesis.py) | LLM 从最近群聊合成群文化、梗、称呼、回复示例、规则与成员印象；新增整活/互损/安慰/接梗/发消息习惯字段，并过滤示例里的泡泡口癖 |
| [`src/wechat_agent/persona_style.py`](src/wechat_agent/persona_style.py) | 人设输出检查：泡泡/冒泡口癖、自称 AI、过激脏话替换，表情数量上限；保留“傻傻的/笨蛋”等轻微互损 |
| [`src/wechat_agent/training_data.py`](src/wechat_agent/training_data.py) | 本地 `style_corpus.jsonl` / `behavior_pairs.jsonl` / `manifest.json` 导出，支持匿名化、上下文窗口和行为标签 |
| [`src/wechat_agent/group_style.py`](src/wechat_agent/group_style.py) | 本地聚合群消息长度、标点、表情和口头表达，按阈值生成动态群聊风格摘要 |
| [`src/wechat_agent/policy.py`](src/wechat_agent/policy.py) | 群白名单、成员白名单、显式 @/开头名称/关键词触发、普通消息跳过、发送开关、回复冷却/小时上限、主动发言社交信号/独立冷却/概率门、速率熔断、STOP 文件 |
| [`src/wechat_agent/llm.py`](src/wechat_agent/llm.py) | DeepSeek/OpenAI 兼容文字调用、可调采样参数和本地图片视觉输入；按鲸鱼娘人设自然交流，主动插话时带“可只回 `[不回复]`”提示，没有 Key 时使用离线 stub |
| [`src/wechat_agent/image.py`](src/wechat_agent/image.py) | 可选 OpenAI-compatible 生图调用；URL/base64 结果落本地并交给微信原生图片附件发送，OSS 作为可选增强 |
| [`src/wechat_agent/config.py`](src/wechat_agent/config.py) | `.env` 配置、30 天原文留存、14/90/365 天半衰期、主动发言/人设检查/每晚导出参数 |
| [`src/wechat_agent/desktop.py`](src/wechat_agent/desktop.py) | 简洁风本地 Web 控制台：启动/停止、配置分组编辑并校验重载、回复频率、主动发言/人设检查/每晚导出、实时事件/回复/审计、抖音/小红书操作 |
| [`src/wechat_agent/platforms/`](src/wechat_agent/platforms/) | 抖音/小红书统一浏览、评论、回复接口；fixture dry-run、Playwright 可见浏览器、平台动作幂等台账、@ 白名单校验和挑战即停 |
| [`src/wechat_agent/web_research.py`](src/wechat_agent/web_research.py) | DSH 同款 DeepSeek relay、Brave/Tavily/SearXNG 搜索、公开网页正文抓取（可选域名收窄），以及当前已登录平台公开条目/评论的临时上下文 |
| [`migrations/001_pgvector.sql`](migrations/001_pgvector.sql) | PostgreSQL/pgvector 迁移草案 |
| [`migrations/004_image_paths.sql`](migrations/004_image_paths.sql) | 为已有 PostgreSQL 消息表增加图片附件路径元数据列 |

## 已验证结果

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q`（测试进程关闭 Embedding 网络）：**96 passed**。
- `python -m compileall -q src`：通过。
- `python -m wechat_agent init-db`：输出 `database schema initialized`。
- `python -m wechat_agent rebuild-memory`：183 条历史消息完成清洗，63 条旧 `chat` 噪声事实归档，2 个群/7 个成员画像重建；真实数据复测见 [`docs/MEMORY_AUDIT.md`](docs/MEMORY_AUDIT.md) 第八节。本次重新合成后两个群的画像均写入 `bit_routines`、`tease_patterns`、`meme_triggers`、`message_shape` 等新字段，提示词摘要分别约 737/582 字，低于 4000 字上限。
- `python -m wechat_agent export-training-data --out data/training --context 6`：从本地 SQL 导出 **133 条**可用群消息（183 条原始消息中排除自身回显、系统消息、疑似机器人文本和无法还原的占位），行为计数为 `joke=4, ordinary=122, question=7`；文件名和匿名化状态见 `data/training/manifest.json`，该目录已被 `.gitignore` 忽略。
- 真实 Embedding smoke test：读取 `.env` 中的 `WORKHUB_EMBED_BASE`、`WORKHUB_EMBED_MODEL`、`WORKHUB_EMBED_KEY`，调用 `https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal` 成功，返回 2 个非空 2048 维向量；测试输出未包含密钥。
- OpenViking MCP smoke test：临时作用域写入两条不同群记忆，语义查询命中并保留 scope，随后删除临时目录并确认目录不存在。
- 作用域验收：同群用户偏好更新后旧值为 `superseded` 且可追溯；跨群/跨群友不召回；群级 AI 画像和任务画像分别隔离并保留历史；无关娱乐消息不会进入技术问题上下文。
- 群聊风格画像使用独立 `group_style_profiles` 表；原始群聊同时保留在 `messages` 并镜像到 OpenViking 分级 `memories/messages/`，用于后续画像刷新和相关上下文召回。
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
- 通过 `python -m wechat_agent desktop --open-browser` 或 `start_desktop_ui.cmd` 打开可操作桌面 UI；简洁风配置中心可以编辑 `.env`，常用设置包含主动发言节奏、人设检查和每晚导出，保存后自动重载监听线程，事件面板每 2 秒刷新。
- 微信监听按 `WECHAT_GROUP_NAMES` 中的群名或 wxid 同时注册多个群；成员日志使用 `sender_username` 和联系人备注/昵称，不再把所有成员归为 `unknown`。`WECHAT_ALLOWED_MEMBERS` 按唯一 ID 校验。图片记录会尝试从本地附件解密并传给视觉模型。
- 通过平台 dry-run 适配器验证浏览、评论/回复计划和幂等键；尚未连接真实平台浏览器会话。
- 设置 `PLATFORM_BROWSER_ENABLED=true` 后，可在桌面控制台打开独立的抖音/小红书持久化浏览器，滚动读取公开内容和带稳定 DOM ID 的评论；评论/回复需 `PLATFORM_WRITE_ENABLED=true` 且二次确认。
- `DOUYIN_MENTION_ALLOWLIST` 和 `XIAOHONGSHU_MENTION_ALLOWLIST` 只按平台唯一用户 ID 配置；动作请求必须同时提交 `mention_ids`，服务层只按 ID 核对并把已核对的 ID 写入动作元数据。
- 设置 `WEB_SEARCH_ENABLED=true` 后，明确的实时/搜索类 @AI 问题可以调用 DSH 同款 `deepseek-search-api`（`POST /v1/tools/search`）、Brave、Tavily 或自建 SearXNG；网页正文继续使用本地 HTTP。`WEB_FETCH_ALLOWED_DOMAINS` 留空时允许公开 HTTP(S) 网站，填写后才收窄范围；本机和内网地址仍会阻断。搜索结果和平台公开内容只进入当前回答上下文，不写入 `memory_facts`。
- 设置 `IMAGE_MODEL_ENABLED=true` 并填写独立的 `IMAGE_MODEL_*` 后，同一个 agent 的生图意图会调用 `/v1/images/generations`；返回的 URL/base64 会先保存到本地，再由 `WeChatGUI.send_image` 发送原生图片附件。`IMAGE_UPLOAD_ENABLED=true` 时可额外上传 OSS，上传失败不会阻断微信图片发送。
- 微信引用/回复消息会尽量解析为 `<wechat_reply_to>`，最近同群消息会进入 `<wechat_context>`；引用正文参与问题去重。
- 默认开启群聊风格拟合：达到 `GROUP_STYLE_MIN_MESSAGES` 后自动生成摘要，每累计 `GROUP_STYLE_REFRESH_MESSAGES` 条普通消息刷新；桌面控制台实时记录会显示采集数量和当前画像。
- 鲸鱼娘人设输出约束已接入：默认拦截自称 AI/模型/助手、泡泡/冒泡口癖和过激脏话，表情最多一个；“傻傻的/笨蛋”等轻微互损保留。
- 主动发言默认关闭，可配置独立冷却（默认 300 秒）、每小时上限（默认 6）和概率（默认 0.4）；只在安慰、接梗、整活、明显提问等有限信号上触发，跳过明确 @ 其他人的消息，模型可返回 `[不回复]`。
- `export-training-data` 已从真实本地库导出 133 条可用消息；当前 183 条原始语料仍不足以做稳定权重微调，现阶段采用提示词风格包 + 数据管线。
- 新增 `--today` 按本地日历日导出；常驻进程可配置 `TRAINING_EXPORT_ENABLED=true` 在每天 23:30 自动导出当天语料，`.exported` 标记防止重启重复，夜间停机后下一次轮询补导前一天。
- 新增提示词自曝拦截：模型不复述系统提示/人设规则/过滤词表，也不会说“我不会吐泡泡”“我被禁止过激侮辱”；输出后处理再做一层替换。
- 审计允许和拒绝的发送动作。
- 用 STOP 文件停止监听。

### 当前还不能宣称可用

- 不能宣称已完成真实微信群自动回复。
- 不能确认 4.1.15.56 上消息行选择器、输入框和发送按钮长期稳定。
- 不能确认真实客户端中自动 @ 选择和发送后的成功回执。
- 不能宣称已在生产 PostgreSQL 上启用 pgvector；当前运行时使用本地 `embedding_json` 混合召回和可选 OpenViking REST bridge。
- 成员层画像已按账号/群/成员实现偏好、兴趣、工具、约束、纠正、来源历史和普通聊天行为统计；群级动态画像仍只提供表达层提示，不代表完整人格模型。OpenViking 使用 `ds` 用户的 `user-ds-peers-wechatagent/memories/accounts/<账号>/groups/<群>/members/<成员>` 分级目录。当前风格学习是提示词级风格包，不是模型权重训练；183 条语料（可用 133 条）不足以稳定微调，需继续积累真实对话后再评估 LoRA。
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
- 普通消息默认不触发回复；显式 @、消息开头名称或 `WECHAT_KEYWORDS` 命中才触发
- `WECHAT_PROACTIVE_REPLY=false`；开启后仍受独立冷却、每小时上限、概率、白名单和 STOP 约束
- `TRAINING_EXPORT_ENABLED=false`；开启后只按本地日期导出 SQL 语料到 `data/`，不联网、不自动训练权重
- 人设输出后处理拦截自称 AI、泡泡口癖、过激脏话和规则自曝
- 必须设置 `WECHAT_GROUP_NAMES`（旧的 `WECHAT_GROUP_NAME` 仍兼容单群）
- `state/STOP` 存在时停止监听
- 发送动作写入 `action_audit`
- 只处理配置群和配置成员

详细需求与验收标准见 [`PRD.md`](PRD.md)。
