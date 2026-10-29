# WeChat Agent（Weixin 4.1.x 原型）

这是一个 Windows 本机原型，直接接入桌面版 Weixin 的测试群。它优先使用 `WeChatDB.Listener` 按群 wxid 读取消息、按 `sender_username` 解析成员，再由 `uiautomation`/`wechatauto-replica` 负责窗口校验和文本发送；数据库不可读时才回退到经过当前群校验的可见 UIA 消息。记忆层使用 SQLAlchemy，支持 SQLite 本地试跑和 PostgreSQL 连接串。PostgreSQL 部署时可在迁移阶段加入 pgvector；当前原型先把群/成员隔离、事实版本和时间召回跑通。

当前适配目标是 Weixin `4.1.x`。本机已核验到 `4.1.15.56`。`wechatauto-replica==1.2.7.0` 会在冷启动只暴露 Qt 外壳时热激活微信进程内的 accessibility gate，再由本项目自己的适配器读取消息和发送文本。原始 `wxauto` GitHub 项目声明支持 3.9.X，因此这里没有把它作为 4.1.x 运行时依赖；`LTEnjoy/easyChat` 的 README 明确列出 4.1.15 和群聊/@，但仓库没有可确认的开源许可证，本项目只参考其公开的版本选择和 UI 结构，不复制其代码。

## 安全开关

默认配置不会发送消息：

- `WECHAT_ALLOW_SEND=false`
- `WECHAT_GROUP_NAMES` 为空时拒绝打开会话
- `WECHAT_GROUP_NAMES` 用逗号分隔多个群名或群 wxid，程序会同时监听这些群；旧的 `WECHAT_GROUP_NAME` 仍兼容
- 运行时优先通过 WeChatDB `Listener` 监听该群的唯一 wxid，不依赖微信当前停留在哪个聊天页；只有本地消息库不可读时才回退 UIAutomation，并先校验当前页确实是配置群
- 群成员从消息行的 `sender_username` 解析微信唯一 ID，再用本地联系人表解析备注/昵称；`WECHAT_ALLOWED_MEMBERS` 只按这个 ID 校验
- `state/STOP` 存在时立即停止监听
- 所有发送动作写入 `action_audit`
- 发送没有每日硬额度，但有每分钟异常速率熔断；白名单、审计和停机开关始终生效
- 普通问句不会触发模型；默认只有显式 `@`、消息开头的 AI 名称或 `WECHAT_KEYWORDS` 命中才进入回复流程。可选开启 `WECHAT_PROACTIVE_REPLY`，只在安慰、接梗、明显提问等有限社交信号上择机插话
- 已成功回答的问题写入持久化 `reply_records`，重启后也不会重复回答
- 默认加载 `personas/dafeiyu/SKILL.md`：鲸鱼娘大肥鱼，知道自己是鲸鱼娘、爱吃白饭，傲娇爱摸鱼但会把事做好；不吐泡泡，表情每条最多一个，不自称 AI，可以轻微互损但不能使用“傻逼”等过激侮辱
- 输出后处理会拦截自称 AI、泡泡口癖、过激脏话和“我不会吐泡泡/我被禁止……”这类提示词自曝；可通过 `PERSONA_STYLE_FILTER=false` 关闭检查
- 默认按当前群自动拟合一层动态聊天风格；它只影响表达方式，不会覆盖静态人设或安全规则

专用测试号准备好后，先复制 `.env.example` 为 `.env`，只填写测试群名称和模型密钥：

```powershell
Copy-Item .env.example .env
# 编辑 .env：WECHAT_GROUP_NAMES=测试群A,测试群B
# 可选：WECHAT_ALLOWED_MEMBERS=成员微信唯一ID1,成员微信唯一ID2
python -m pip install -r requirements.txt
python -m wechat_agent init-db
# 已有历史数据时，回填统一清洗文本并重建群/成员画像：
python -m wechat_agent rebuild-memory
# 导出本地风格/行为数据（默认匿名化，不联网）：
python -m wechat_agent export-training-data --out data/training --context 6
# 只导出本地当天，供每天晚上跑一次：
python -m wechat_agent export-training-data --today
python -m wechat_agent ready
```

`ready` 只检查 Weixin 窗口，不打开群、不发送消息。检查过程中如果只发现 `Qt51514QWindowIcon` 外壳，会先调用 `wechatauto-replica` 热激活 accessibility gate，再检查 `mmui::MainWindow`/`MainView`。确认窗口和群名称正确后，才显式开启发送：

```powershell
Remove-Item state/STOP -ErrorAction SilentlyContinue
python -m wechat_agent run --allow-send
```

本机桌面 UI（浏览器窗口）：

```powershell
python -m wechat_agent desktop --open-browser
```

打开后可以直接点击“启动自动回复 / 关闭自动回复”，在“设置”里修改回复频率、触发范围、主动发言节奏、人设检查和每晚语料导出，保存后会校验 `.env` 并重载；在“实时记录”查看轮询日志、回复去重台账和发送审计。页面每 2 秒自动刷新；关闭浏览器窗口不会停止后端，需在 UI 点击停止或创建 `state/STOP`。界面使用简洁的中性色和单一强调色，概览页会直接显示主动发言的间隔/上限和每日导出时间。

“回复最短间隔”和“每小时最多回复数”只限制已经命中 `@`、开头名称或关键词且实际成功发出的回复。主动发言有独立的 `WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS`（默认 300 秒）、`WECHAT_PROACTIVE_MAX_PER_HOUR`（默认 6）和 `WECHAT_PROACTIVE_PROBABILITY`（默认 0.4）；默认关闭，白名单、`state/STOP` 和发送审计仍然生效。模型温度、Top-P、重复词惩罚和新话题倾向在“更多设置 → 模型”中调整；默认值保持原有行为。

Windows 下也可以双击仓库根目录的 `start_desktop_ui.cmd`（或运行 `start_desktop_ui.ps1`）打开这个 UI。

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
WEB_FETCH_ALLOWED_DOMAINS=
```

请求会发到 `POST /v1/tools/search`；网页正文继续使用本地 HTTP。`WEB_FETCH_ALLOWED_DOMAINS` 留空即可抓取所有公开 HTTP(S) 网站，填写后才会收窄范围；本机和内网地址仍会阻断。不使用 Exa 或官方 DeepSeek Messages 搜索。也可以选择 `brave`、`tavily` 或自建 `searxng`。搜索结果、网页正文和抖音/小红书公开评论只进入当前问题上下文，不写入群友长期画像。

同一个 agent 还支持可选的第二个生图模型。把 `IMAGE_MODEL_ENABLED=true`、`IMAGE_MODEL_BASE_URL`、`IMAGE_MODEL_API_KEY` 和 `IMAGE_MODEL` 配好后，明确 @AI 的“生成图片/画一张/生图”消息会调用 `POST /v1/images/generations`，文字问题仍走 `LLM_*`。生图返回的 URL 或 `b64_json` 会先落到 `state/generated/`，再由微信连接器作为原生图片附件发送；`IMAGE_MODEL_RESPONSE_FORMAT=auto` 会省略容易被 GPT-image 网关拒绝的可选字段。群聊记录中的图片会从本地微信附件解密，并以视觉输入交给支持图片理解的 OpenAI 兼容模型。

如果要让其他客户端访问结果，可再开启 `IMAGE_UPLOAD_ENABLED=true`，配置阿里云 OSS。程序会把生成图片直接上传到 `OSS_BUCKET`，不经过其他应用网关；配置 `OSS_PUBLIC_BASE_URL` 时返回公开 URL，否则返回 OSS 签名 URL（由 `OSS_SIGNED_URL_TTL_SECONDS` 控制有效期）。`OSS_PREFIX` 下会使用 `wechat-agent/generated/` 目录保存对象。上传失败会记录 warning，但仍继续把本地图片附件发到微信。运行账号只需要目标 bucket 的最小上传和读取权限，真实密钥只放在运行机器的 `.env`。

微信引用消息和最近同群消息会分别注入 `<wechat_reply_to>`、`<wechat_context>`，引用正文会参与问题去重。UIAutomation 没有暴露引用正文时只能按当前版本控件树做 best-effort 解析，需你本地实测。

群聊风格拟合默认打开。系统按 `account_id + group_id` 隔离统计普通成员消息的长度、短句比例、问号/感叹号、表情和常见口头表达；同时把原始消息按保留策略保存，供后续画像刷新和相关上下文召回。达到最小消息数后生成画像，每累计一批新消息刷新一次：

```ini
GROUP_STYLE_ENABLED=true
GROUP_STYLE_MIN_MESSAGES=20
GROUP_STYLE_REFRESH_MESSAGES=20
GROUP_STYLE_SAMPLE_MESSAGES=80
GROUP_STYLE_MAX_CHARS=1800
GROUP_STYLE_WINDOW_MESSAGES=300
PROFILE_PROMPT_MAX_CHARS=4000
PROFILE_SYNTHESIS_ENABLED=true
```

统计画像只用最近 `GROUP_STYLE_WINDOW_MESSAGES` 条消息重建；语义画像由 LLM 从最近群聊合成群文化、梗、称呼、回复示例和成员印象。进入提示词的只有 ≤`PROFILE_PROMPT_MAX_CHARS` 的自然语言摘要，完整画像和历史仍保存在 SQL/OpenViking。历史数据用 `python -m wechat_agent rebuild-memory` 回填与重建。

桌面控制台的“实时记录”会显示采集数量和当前画像。群聊画像放在独立的 `<group_style_profile>` 块中，只能调整说话方式和互动倾向；`@`/关键词触发、白名单、联网权限、事实边界和工具调用规则仍由静态配置决定。

语义画像除了群文化、梗、称呼和回复示例，还会记录可选的整活套路、互损/钓鱼、安慰话术、接梗触发和发消息习惯；这些字段只在最近群聊里反复出现证据时才生成，空字段不会强行编造。图片里的完整画像仍保存在 SQL/OpenViking，进入提示词的摘要受 `PROFILE_PROMPT_MAX_CHARS` 限制（默认 4000）。

`export-training-data` 只读取本地 SQL，输出到被 `.gitignore` 忽略的 `data/training/`：

- `style_corpus.jsonl`：每条群成员消息一行，适合做风格适配或继续训练；
- `behavior_pairs.jsonl`：前文上下文 + 成员实际回复 + 轻量行为标签（`prank`/`comfort`/`tease`/`joke`/`question`/`ordinary`）；
- `manifest.json`：数量、分组、匿名化状态、时间范围和格式版本。

`--today` 会按本地日历日过滤，默认写到 `data/training/daily/<日期>/`。常驻监听进程也可以自动执行：到 `TRAINING_EXPORT_HOUR:TRAINING_EXPORT_MINUTE` 后把当天语料导出一次，并用 `.exported` 标记防止重启重复导出；进程在夜间停机时，下一次轮询会补导前一天。它只导出数据，不会自动训练权重：

```ini
TRAINING_EXPORT_ENABLED=true
TRAINING_EXPORT_DIR=data/training/daily
TRAINING_EXPORT_HOUR=23
TRAINING_EXPORT_MINUTE=30
TRAINING_EXPORT_CONTEXT_MESSAGES=6
TRAINING_EXPORT_ANONYMIZE=true
```

导出默认把成员/群名替换成 `群友A`、`群B`，不会联网；但消息正文仍原样保留，可能包含群友昵称、地点或其他个人信息，分享或用于训练前必须人工检查。注意 `behavior_pairs` 的目标是群成员的原话，不是“鲸鱼娘应该怎么回”的 AI 回复；它适合研究群的说话方式，真正做角色 SFT 前还需要把目标改写成大肥鱼口吻或使用经审核的线上回复。当前 183 条原始消息（实际可用 133 条）远不足以稳定微调权重，因此现在优先用提示词风格包 + 本地数据管线，等样本量足够再做 LoRA。

当 `PLATFORM_BROWSER_ENABLED=true` 且平台浏览器已人工登录时，群友明确询问“抖音/小红书”内容会读取当前公开条目和公开评论。遇到验证码、安全验证、IP 风险或登录失效会停止，不绕过平台限制。完整变量说明见 [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)，候选 GitHub 项目核验见 [`docs/OPEN_SOURCE_RESEARCH.md`](docs/OPEN_SOURCE_RESEARCH.md)。

默认只监听 `127.0.0.1:8765`。需要从其他机器访问时，设置 `DESKTOP_TOKEN`，再通过反向代理暴露；控制台的启动、停止、回复台账和动作审计都使用同一套发送策略。

停止方式：

```powershell
python -m wechat_agent --stop
```

当前账号尚未进行真实发送测试。等专用测试号准备好后，先运行 `ready`，再做一条由你指定的测试群 @AI 消息回放；每一步都会先查看审计记录和运行日志。

## 数据边界

`messages` 保存原始消息 30 天，并记录外部消息 ID、线程、任务、引用和发送者元数据；`memory_facts` 长期保存事实和画像候选，记录 `observed_at`、`valid_from`、`valid_until`、置信度、重要性、状态、来源消息、Embedding 模型和版本关系。检索先按 `account_id + group_id + member_id`（任务记忆再加 `task_id`）过滤，再综合词法重合、Embedding 余弦相似度、新鲜度、置信度和重要性；Embedding 不可用时回退到本地词法召回。

用户画像按 `account_id + group_id + member_id` 保存。除了明确偏好，普通聊天还会累计消息长度、常用词、主题、问句/感叹号、表情、称呼和句尾等软行为信号；这些信号只用于贴近该群员的表达习惯，不会被当成已确认事实。群级 AI 画像按 `account_id + group_id` 保存，任务画像按 `account_id + group_id + task_id` 保存；每次更新都记录历史和来源。明确的新偏好会把同一作用域、同一记忆键的旧记录标为 `superseded`，不跨群或跨群友召回。

OpenViking REST bridge 使用配置 API Key 对应的 `ds` 用户，并以一个 `user-ds-peers-wechatagent` peer 保存分级目录：`memories/accounts/<账号>/groups/<群>/` 下保存群级原文、群级 AI/风格画像；`members/<成员>/` 下保存该群员原文和成员画像；任务和事实也位于对应作用域下。原文和画像都带账号、群、成员字段，召回从该 peer 的 `memories/` 根进入后再次精确过滤；旧的扁平 peer 不参与召回。

Embedding 配置沿用运行机器 `.env` 中的 `EMBED_*` 或现有 `WORKHUB_EMBED_*` 名称。当前实际配置的多模态 Ark 模型是 `doubao-embedding-vision-251215`，真实调用返回 2048 维；`EMBED_DIMENSION=0` 表示接受服务端维度并校验批内一致性。OpenViking 的 `OPENVIKING_NAMESPACE` 应与 API Key 对应的用户一致；本项目运行配置使用 `ds`，不会读取 Codex 的 `default` 用户空间。URL 或 Key 为空时，应用继续使用本地 SQL、词法和本地向量字段，不阻断消息处理。远程记忆连接最多等待 `OPENVIKING_CONNECT_TIMEOUT_SECONDS`，失败后按 `OPENVIKING_FAILURE_COOLDOWN_SECONDS` 熔断，后续消息直接走本地回退。

平台账号绑定接口预留在 `identity_bindings`，绑定必须由群友明确确认；同一平台身份可以映射到多个群，但记忆和画像不会跨群直接混用。

## 目录

- `src/wechat_agent/weixin_ui.py`：Weixin 4.1.x UIAutomation 适配器
- `src/wechat_agent/agent.py`：群消息、画像抽取、RAG 和回复编排
- `src/wechat_agent/group_style.py`：群聊风格统计和画像生成
- `src/wechat_agent/persona_style.py`：人设输出检查（自称 AI、泡泡口癖、脏话和表情上限）
- `src/wechat_agent/training_data.py`：本地风格/行为 JSONL 导出
- `src/wechat_agent/storage.py`：消息、事实、绑定和动作审计
- `src/wechat_agent/policy.py`：群白名单、@AI、主动发言和停机策略
- `src/wechat_agent/desktop.py`：本地 Web 桌面控制台
- `src/wechat_agent/platforms/`：抖音/小红书统一适配层；默认 dry-run，可显式开启 Playwright 可见浏览器
- `docs/PLATFORM_ADAPTERS.md`：抖音/小红书浏览器登录、滚动读取、评论队列和安全边界
- `docs/DEPLOYMENT.md`：Windows 微信连接器与服务器核心的 24 小时拓扑

## 已知限制

- Weixin UI 结构随客户端版本变化；4.1.x 版本需在专用号上实测消息读取和发送。
- UIAutomation 只能读取当前可见聊天内容，历史加载和媒体消息需要后续适配。
- 个人微信账号存在平台协议和封禁风险；本项目不承诺普通个人微信群长期稳定运行。

服务器 24 小时运行建议采用“服务器核心服务 + Windows 微信连接器”拓扑：微信 UIAutomation 仍需在带桌面会话的 Windows 主机保持登录，服务器负责模型、台账、平台任务和控制台。当前代码没有执行远端部署，也不会把微信登录态迁移到服务器。
