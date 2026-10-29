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
WECHAT_GROUP_NAMES=测试群A,测试群B
WECHAT_CONTEXT_MESSAGES=12
WECHAT_CONTEXT_MAX_CHARS=6000
WECHAT_AI_ALIASES=AI,ai,机器人
WECHAT_KEYWORDS=问AI,小助手
# 触发规则不变；以下只限制已成功发出的回复频率，0 表示不额外限制。
WECHAT_REPLY_COOLDOWN_SECONDS=0
WECHAT_MAX_REPLIES_PER_HOUR=0
```

桌面控制台“设置”里的常用设置可以直接修改回复最短间隔、每小时回复上限，以及主动发言的间隔/上限/概率、人设检查和每晚导出时间；保存后会校验 `.env` 并重载助手。最短间隔用于避免连续快速回复；每小时上限用于控制总量。两项只作用于已经命中 `@`、开头名称或关键词的回复，不会让普通群消息绕过触发规则；主动发言另有独立冷却和概率门。

### 主动发言

主动发言默认关闭。打开后，普通消息仍不会全部进入模型；只有命中有限社交信号（明显的难受/疲惫、接梗、整活、直接提问）时，才按概率择机插话，并且不会抢答明确 `@` 了其他人的消息：

```ini
WECHAT_PROACTIVE_REPLY=false
WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS=300
WECHAT_PROACTIVE_MAX_PER_HOUR=6
WECHAT_PROACTIVE_PROBABILITY=0.4
```

主动发言有独立的冷却和每小时上限，不占用普通 `@` 回复的额度逻辑；发送仍受群白名单、`state/STOP`、每分钟熔断和动作审计约束。模型可以选择只输出 `[不回复]`，此时会记为跳过而不发消息。为避免无意义刷屏，默认值保持关闭；确认群氛围适合后再打开。

微信不同版本可能只在 UIAutomation 树中暴露“引用”按钮而不暴露原文。当前实现是 best-effort 解析；如果版本没有暴露引用正文，需要在运行机器上根据控件树再补选择器。

## 群聊风格拟合

打开后，监听器会把当前微信号、当前群里的普通成员消息汇总成一份群级表达画像。画像按 `account_id + group_id` 隔离，不会把一个群的说话习惯带到另一个群；也不会把群级画像写入某个成员的长期事实。原始消息仍按 `messages` 保留期限存储，并镜像到 OpenViking 的群级 `memories/messages/`，供后续画像刷新和相关上下文召回。画像达到阈值后生成，之后每累计指定数量的新消息刷新一次：

```ini
GROUP_STYLE_ENABLED=true
GROUP_STYLE_MIN_MESSAGES=20
GROUP_STYLE_REFRESH_MESSAGES=20
GROUP_STYLE_SAMPLE_MESSAGES=80
GROUP_STYLE_MAX_CHARS=1800
GROUP_STYLE_WINDOW_MESSAGES=300
PROFILE_PROMPT_MAX_CHARS=4000
PROFILE_SYNTHESIS_ENABLED=true
PROFILE_SYNTHESIS_MESSAGES=120
PROFILE_SYNTHESIS_MAX_INPUT_CHARS=6000
```

`GROUP_STYLE_WINDOW_MESSAGES` 控制只用最近多少条消息重建统计画像，避免永久累加；`PROFILE_PROMPT_MAX_CHARS` 控制注入提示词的画像摘要上限；`PROFILE_SYNTHESIS_*` 控制 LLM 语义画像（群文化、梗、称呼、回复示例、成员印象）的开关、窗口与输入上限。历史数据可用 `python -m wechat_agent rebuild-memory` 一次性回填清洗文本并重建画像。

语义画像的 JSON 还包含可选的 `bit_routines`（整活套路）、`tease_patterns`（互损/钓鱼）、`comfort_patterns`（安慰话术）、`meme_triggers`（接梗触发）和 `message_shape`（发消息习惯）。这些字段只在转写中有反复证据时生成；空字段不会编造。回复示例会经过人设输出检查，去掉泡泡口癖并限制表情数量。完整 JSON 保存在 SQL/OpenViking，进入提示词的是受 `PROFILE_PROMPT_MAX_CHARS` 限制的摘要，默认 4000 字符。

需要研究本地语料或准备后续 SFT 时，用下面的命令导出（只读 SQL、不联网、默认匿名化）：

```powershell
python -m wechat_agent export-training-data --out data/training --context 6
python -m wechat_agent export-training-data --out data/training --context 8 --no-anonymize
```

输出 `style_corpus.jsonl`、`behavior_pairs.jsonl` 和 `manifest.json`。默认只匿名化成员/群标签，消息正文仍原样保留，可能含群友昵称、地点或其他个人信息；对外分享或训练前必须人工检查。`behavior_pairs` 的 response 是群成员原话，不是 AI 回复；做角色 SFT 前需要改写为目标人设的回复。`data/` 已被 `.gitignore` 忽略，避免把本地群聊误提交。当前只有 183 条原始消息（清洗后 133 条可用于风格导出），不足以稳定微调权重，建议先把风格包和真实对话收集跑顺，再考虑 LoRA。

每天晚上的自动导出默认关闭，打开后由常驻监听进程在指定本地时间导出当天语料；只导出，不自动训练权重。进程夜间停机时，下一次轮询会补导前一天；`.exported` 标记防止重启后重复导出：

```ini
TRAINING_EXPORT_ENABLED=true
TRAINING_EXPORT_DIR=data/training/daily
TRAINING_EXPORT_HOUR=23
TRAINING_EXPORT_MINUTE=30
TRAINING_EXPORT_CONTEXT_MESSAGES=6
TRAINING_EXPORT_ANONYMIZE=true
```

只想手动导出当天时用 `python -m wechat_agent export-training-data --today`，默认输出到 `data/training/daily/<日期>/`。

这层画像只调整说话方式和互动倾向（句子长度、口语程度、标点、接梗/互损/安慰的轻重），不能改变静态人设、事实核验、`@`/关键词触发、群成员白名单、联网开关或工具权限。桌面控制台的“实时记录”区域会显示当前群的采集数量和最新画像；没有达到最小消息数时会显示“样本不足”。

监听目标由 `WECHAT_GROUP_NAMES` 中每个群名或群 wxid 分别解析，正常路径使用一个 `WeChatDB.Listener` 注册多个会话，所以你把微信窗口切到别的群也不会改变监听对象。旧的 `WECHAT_GROUP_NAME` 仍可作为单群兼容配置。只有微信本地消息库不可读时才使用 UIAutomation 回退；回退每轮会检查当前会话名称，无法确认是配置群就跳过该轮。数据库消息里的 `sender_username` 是成员唯一微信 ID，`get_nickname()` 只用于显示备注/昵称；`WECHAT_ALLOWED_MEMBERS` 应填写这些 ID，不要填写昵称。

```ini
WECHAT_ALLOWED_MEMBERS=成员微信唯一ID1,成员微信唯一ID2
```

### 图片消息

设置 `WECHAT_IMAGE_RECOGNITION_ENABLED=true` 后，图片记录会通过 `wechatauto.MediaDownloader` 从微信本地附件解密，按数量和大小上限作为视觉输入传给兼容 OpenAI 的模型：

```ini
WECHAT_IMAGE_RECOGNITION_ENABLED=true
WECHAT_IMAGE_MAX_COUNT=4
WECHAT_IMAGE_MAX_BYTES=12000000
WECHAT_MEDIA_DIR=state/wechat-media
IMAGE_OUTPUT_DIR=state/generated
```

如果图片尚未落盘、无法解密或模型不支持视觉输入，记录仍保留为 `[图片]`，不会伪造识别结果。

## 把运行数据放到其他磁盘

所有会持续增长的数据都可以整体搬到别的盘（例如 D 盘），只需改 `.env` 里的四项路径，不需要改代码：

```ini
DATABASE_URL=sqlite:///D:/wechat-agent-data/memory.db
WECHAT_MEDIA_DIR=D:/wechat-agent-data/wechat-media
IMAGE_OUTPUT_DIR=D:/wechat-agent-data/generated
TRAINING_EXPORT_DIR=D:/wechat-agent-data/training/daily
WECHAT_STOP_FILE=D:/wechat-agent-data/STOP
```

搬迁已有数据时按顺序做：先关闭助手进程 → 把旧的 `state/memory.db`（连同 `-wal`/`-shm`，或用 `sqlite3 .backup` 导出一份一致的副本）、`state/wechat-media/`、`state/generated/`、`data/training/` 复制到新目录 → 再改 `.env` → 启动。SQLite 路径在 Windows 上写成 `sqlite:///D:/...` 形式；换成 PostgreSQL 时只改 `DATABASE_URL`，其余目录仍然有效。

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

文字模型效果可以在桌面控制台“更多设置 → 模型”调整，也可以写入 `.env`：

```ini
LLM_TEMPERATURE=0.7
LLM_TOP_P=1
LLM_FREQUENCY_PENALTY=0
LLM_PRESENCE_PENALTY=0
LLM_MAX_OUTPUT_TOKENS=3000
```

温度越低回答越稳定，越高变化越多；重复惩罚用于减少重复表达；Top-P 和两项惩罚由 OpenAI 兼容服务按其能力处理，保持默认值时不会额外发送这些可选参数。`deepseek-flash` 这类推理模型的思考 token 也占用该额度，500 会在长回答里被思考过程吃光并返回空文本，所以默认给到 3000。

群里同时来消息时的排队节奏由 `WECHAT_REPLY_QUEUE_MAX_SIZE` 控制（默认 50）：忙不过来时消息按到达顺序进队列，每轮只回一条，超过上限丢最旧的一条并打日志。微信 4.x 的图片缓存是私有 `.wxgf` 容器（内部是 HEVC 裸流），发送给视觉模型前会自动用 ffmpeg 转成 PNG 并缓存在 `state/wechat-media/converted/`；本机取不到 ffmpeg 时该图直接跳过，文字回复照常。

图片进入提示词也有窗口，避免一张图被反复描述：`WECHAT_IMAGE_CONTEXT_TURNS`（默认 4）表示一张图只在之后这么多条消息内参与回答；更早的图要等群友重新提到（“上面那张图”“看图”“照片里”等）才会再次带上，并在之后同样保持这么多条。`WECHAT_IMAGE_REFERENCE_WINDOW`（默认 30）限制重新提到时最多往回找多少条消息。提示词同时要求模型：跟图片无关的话题不要主动提图，只有实在没内容可答时才允许顺带提一句。

```ini
IMAGE_MODEL_ENABLED=true
IMAGE_MODEL_BASE_URL=https://你的生图网关/v1
IMAGE_MODEL_API_KEY=在运行机器本地填写
IMAGE_MODEL=你的生图模型名
IMAGE_MODEL_SIZE=1024x1024
IMAGE_MODEL_RESPONSE_FORMAT=auto
IMAGE_MODEL_TIMEOUT_SECONDS=120
```

接口约定为 `POST /v1/images/generations`。`IMAGE_MODEL_RESPONSE_FORMAT=auto` 会省略兼容性较差的可选字段，并接受 `url` 或 `b64_json`。生成结果会先保存到 `state/generated/`，再作为微信原生图片附件发送；OSS 上传只是给其他客户端访问的可选增强：

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

`OSS_PUBLIC_BASE_URL` 为空时使用私有 bucket 的签名 GET URL；要长期公开访问，应使用公开 bucket/CDN 域名。`OSS_ACCESS_KEY_ID` 和 `OSS_ACCESS_KEY_SECRET` 只放在运行机器的 `.env`，不要提交到仓库。OSS 上传失败会记录 warning，但不阻断本地图片附件发送。

## 可配置人设

人设只包裹回答语气，不改变群/成员隔离、工具结果、事实准确性和安全规则。默认已内置你提供的 [dafeiyu-persona](https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona) 适配版：鲸鱼娘大肥鱼，知道自己是鲸鱼娘、爱吃白饭，性格傲娇、爱摸鱼但会把答应的事做好；不吐泡泡/冒泡/咕噜，表情少量且一条最多一个，说话像群里的活人，不自称 AI、模型或助手。可以轻微互损（如“傻傻的”“笨蛋”），但不能使用“傻逼”等过激侮辱。

模型会以配置的人设身份自然交流；这条表达约束不会覆盖事实核验、触发条件、白名单和安全规则。输出后处理会再做一层确定性检查，拦截漏出的自称 AI、泡泡口癖、过激脏话，并把表情压到上限内；也会拦截“我不会吐泡泡/我被禁止说脏话/我的提示词……”这类规则自曝，避免把系统提示或过滤词表发到群里：

```ini
PERSONA_ENABLED=true
PERSONA_NAME=大肥鱼
PERSONA_FILE=personas/dafeiyu/SKILL.md
PERSONA_SOURCE_URL=https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona
PERSONA_MAX_CHARS=12000
PERSONA_STYLE_FILTER=true
PERSONA_MAX_EMOJI=1
```

把 `PERSONA_FILE` 改成自己的 Markdown 文件即可添加其他人设；同目录下的 `references/lines.md` 和 `references/lore.md` 会作为风格参考一起读取。修改配置后在桌面 UI 点击“保存并重载”，或重启监听进程。人设文件不会在运行时从 URL 下载，`PERSONA_SOURCE_URL` 只是来源记录。

## 记忆库和向量模型

消息进入回复路径前会先按账号、群和群友稳定 ID 隔离，再从当前线程/任务、同群相关消息、用户画像、群级 AI 画像和任务历史组装上下文。无关的同群消息会在进入模型前过滤；原始消息保留 30 天，长期候选记忆保留来源消息、置信度、重要性、状态和版本历史。

Embedding 使用现有 `.env` 配置，不把密钥写入代码、日志或提示词：

```ini
EMBED_BASE=https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal
EMBED_MODEL=doubao-embedding-vision-251215
EMBED_KEY=运行机器上的密钥
EMBED_DIMENSION=0
EMBED_TIMEOUT_SECONDS=45
```

`EMBED_DIMENSION=0` 表示接受服务端实际维度，同时校验同一批向量维度一致；也可以填正整数进行严格校验。当前配置的真实接口使用 `input: [{"type":"text","text":"..."}]`，实际 smoke test 返回 2048 维。客户端也兼容标准 OpenAI `/embeddings` 批量响应。

OpenViking 是可选的远程语义记忆索引；SQL 是作用域和审计台账，远程服务不可用时仍可用本地词法/向量召回：

```ini
OPENVIKING_ENABLED=true
OPENVIKING_URL=
OPENVIKING_API_KEY=
# The configured API key belongs to the OpenViking user ``ds``.
OPENVIKING_NAMESPACE=ds
OPENVIKING_TIMEOUT_SECONDS=45
OPENVIKING_CONNECT_TIMEOUT_SECONDS=5
OPENVIKING_FAILURE_COOLDOWN_SECONDS=300
OPENVIKING_COMMIT=true
OPENVIKING_PROFILE_MIN_INTERVAL_SECONDS=30
```

远程记忆的 URI 按 `ai_account_id + group_id + member_id + task_id` 组织，检索后再次执行精确作用域过滤。当前运行配置的 API Key 对应 OpenViking 用户 `ds`，写入统一位于 `viking://user/ds/peers/user-ds-peers-wechatagent/memories/accounts/<账号>/groups/<群>/`：群目录保存群原文和群级画像，`members/<成员>/` 保存成员原文和成员画像，任务/事实保存在对应作用域下。普通聊天会累计成员软行为画像（主题、常用词、长度、问句、标点、表情、称呼和句尾），仅作为表达参考，不等同于确认事实。OpenViking 搜索从该 peer 的 `memories/` 根进入并按 payload 再过滤，旧扁平目录不参与召回；如果 URL 或 Key 留空，应用才会按可选能力降级到本地 SQL/向量召回。

## 运行边界

`PLATFORM_BROWSER_ENABLED=true` 只在有桌面会话的 Windows 机器上启用抖音/小红书可见浏览器。服务器可以长期运行 LLM、搜索、数据库和任务编排；微信 UIAutomation 以及需要可见浏览器登录态的平台采集仍需要 Windows 连接器在线。
