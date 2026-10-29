# 微信 AI 群聊 Agent

在 Windows 上接管一个**专用测试微信号**，在指定微信群里读消息、学着这群人怎么说话，再按固定人设接话。消息从微信本地数据库读，发送用屏幕坐标 + OCR 模拟真人操作，长期记忆存放在 OpenViking。

定位是个人/小群自用的原型，不是多租户产品。真实群里的端到端联调尚未完成验收，边界见[已知限制](#已知限制)。

---

## 一览

| 项目 | 说明 |
|---|---|
| 支持的微信版本 | **Weixin 4.1.x**，本机实测 `4.1.15.56`。4.1.12+ 才改成自绘聊天界面，坐标 + OCR 发送依赖这套布局 |
| 系统 | Windows 10/11，需要可交互的桌面会话（远程服务器无桌面时会失败） |
| 收消息 | 读微信本地 SQLCipher 消息库（`wechatauto-replica` 的 `wechatauto.db.Listener`），按群 wxid 监听，不要求微信停在哪个聊天页面 |
| 发消息 | 「屏幕坐标 + 本地 OCR」模拟操作（`wechatauto.guia.WeChatGUI`）：定位输入框 → 粘贴 → 回读比对 → 回车；失败回退 UIAutomation，并用本地数据库回读确认 |
| 启动前置 | 先开 **Windows 讲述人（Narrator）** 再重启微信：4.x 只在无障碍模式打开时暴露 UIAutomation 控件树，窗口自检、回退读取和部分发送路径都依赖它 |
| 记忆 | **OpenViking** 分级记忆（账号 → 群 → 成员 → 任务）；本地 SQL 负责审计、离线回退和混合召回 |
| 模型 | 任何 OpenAI 兼容接口（默认 DeepSeek）；Embedding、生图、视觉理解可以分别接不同服务 |
| 控制台 | 本地网页 `http://127.0.0.1:8765`：启动/停止、实时记录、配置编辑 |

## 工作原理

- **读**：项目不截图识字来读消息，而是直接解密读取微信本地消息库。启动时把 `WECHAT_GROUP_NAMES` 里的群名解析成群 wxid 并注册监听，消息按群、按成员隔离入库；图片附件会从本地解密后交给视觉模型。
- **发**：优先走坐标 + OCR 路径——用 Windows 自带 OCR（`winsdk`）识别界面元素，`SendInput` 模拟点击和键入，回车前回读输入框确认内容没落空；这条路径不可用时回退 UIAutomation，并以数据库是否出现这条消息作为发送成功的最终判据。
- **记**：消息原文 30 天滚动保留，事实、偏好和画像长期保存。配置了 OpenViking 时写入分级记忆目录并按作用域召回；不可达时自动回退到本地 SQL + 词法召回，不阻塞消息处理。
- **说**：命中触发规则后组装提示词（静态人设 + 群风格画像 + 成员画像 + 相关记忆 + 最近上下文），调用模型生成回复，再过一道人设检查（拦截自称 AI、口癖、过激脏话）。

## 运行环境

- Windows 10 / 11，已安装并能正常登录 **Weixin 4.1.x**
- Python **3.11+**
- 一个**专用测试微信号**（不要用主号，个人号自动化有封号风险）
- 微信保持已登录状态，且与程序用同一个 Windows 账号运行（要读它的数据目录）

## 快速开始

### 1. 打开讲述人，然后重启微信

Windows 设置 → 辅助功能 → 讲述人 → 打开（或按 `Win + Ctrl + Enter`）。

开启后**重启微信客户端**，让它重新暴露无障碍控件树；讲述人的语音可以关掉，但进程要留在后台，程序运行期间不要退出。

> 只读数据库这条路本身不依赖讲述人；但启动自检（确认 `mmui::MainWindow` / `MainView`）、数据库不可读时的回退读取、以及部分发送兜底都需要控件树。不开讲述人时这一步通常会卡在"已找到窗口但没有暴露 MainView"。

### 2. 安装依赖

```powershell
git clone https://github.com/lingmao233/wechat-agent.git
cd wechat-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

OCR 走 Windows 自带的 OCR 引擎（依赖 `winsdk`，已随 `wechatauto-replica` 安装）。如果系统缺少中文 OCR 语言包，在"设置 → 时间和语言 → 语言"里给中文补装"光学字符识别"。

### 3. 写配置

```powershell
Copy-Item .env.example .env
```

最少需要改这几项（完整变量见 [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)）：

```ini
# 要接管的群：群名或群 wxid，逗号分隔；启动时会同时监听
WECHAT_GROUP_NAMES=测试群A,测试群B
WECHAT_AI_ALIASES=AI,大肥鱼

# 先保持关闭，只观察入库和生成的回复
WECHAT_ALLOW_SEND=false

LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=你的密钥
LLM_MODEL=deepseek-chat

# 本地库；也可以指向 PostgreSQL
DATABASE_URL=sqlite:///state/memory.db

# 留空则只用本地记忆；填上则写入 OpenViking
OPENVIKING_URL=
OPENVIKING_API_KEY=
EMBED_BASE=https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal
EMBED_KEY=
```

### 4. 初始化并自检

```powershell
python -m wechat_agent init-db
python -m wechat_agent ready
```

`ready` 只检查窗口和配置，不打开群、不读取聊天、不发送任何消息。它会返回窗口类名、进程号和已配置的群；这里报错说明讲述人或微信登录态没准备好。

### 5. 启动

```powershell
# 只读模式：消息入库、画像更新、生成候选回复，但不发送
python -m wechat_agent run

# 确认无误后再允许发送
python -m wechat_agent run --allow-send
```

### 6. 打开控制台（可选）

```powershell
python -m wechat_agent desktop --open-browser
```

也可以双击仓库根目录的 `start_desktop_ui.cmd`。页面上能启动/停止、看实时记录与发送审计、直接改 `.env`（保存后校验并重载监听线程）。关掉浏览器不会停止后端，要用页面上的停止按钮或 `state/STOP`。

常用命令：

| 命令 | 作用 |
|---|---|
| `python -m wechat_agent ready` | 只读自检：窗口、进程、配置的群 |
| `python -m wechat_agent run [--allow-send]` | 常驻监听（不带参数只读） |
| `python -m wechat_agent desktop [--open-browser]` | 本地控制台 |
| `python -m wechat_agent init-db` | 建表 |
| `python -m wechat_agent rebuild-memory` | 回填清洗文本、重建群/成员画像 |
| `python -m wechat_agent export-training-data [--today]` | 导出本地语料（默认匿名化，不联网） |
| `python -m wechat_agent --stop` | 创建 `state/STOP` 并停止 |

## 什么时候会回话

只有这几种情况会进入回复流程，普通聊天只入库不回复：

- 显式 `@` 机器人名称（`WECHAT_AI_ALIASES` / 微信昵称 / `WECHAT_SELF_NAME`）
- 消息以机器人名称开头
- 命中 `WECHAT_KEYWORDS`

安全默认值：

- `WECHAT_ALLOW_SEND=false`：不改这个开关，程序只读不写
- 群和成员白名单（`WECHAT_ALLOWED_MEMBERS` 只认微信唯一 ID，昵称不算）
- `state/STOP` 文件存在即停
- 所有发送动作写 `action_audit`，每分钟异常速率会熔断
- 同一问题只回答一次（持久化去重，重启后依然生效）
- 主动发言（`WECHAT_PROACTIVE_REPLY`）默认关闭；开启后也有独立冷却、每小时上限和概率门，只在安慰、接梗、明显提问等有限信号上插话
- 回复频率限制只作用于真正发出去的回复；主动发言另有一套额度

## 记忆与画像

- **按作用域隔离**：记忆键是 `account_id + group_id + member_id`（任务记忆再加 `task_id`），跨群、跨群友不召回；偏好被明确更新后旧记录标记为 `superseded`，可追溯。
- **成员画像**：偏好、兴趣、工具、约束、纠正和普通聊天行为统计（长度、标点、表情、称呼等），只用于贴近该群员的表达方式，不当成事实。
- **群风格画像**：默认开启，统计群消息长度、短句比例、表情和口头禅，并让 LLM 从最近群聊合成群文化、梗、称呼和回复示例；统计只取最近 `GROUP_STYLE_WINDOW_MESSAGES` 条，避免永久累加漂移。
- **OpenViking 目录**：`memories/accounts/<账号>/groups/<群>/` 存群级原文和群级画像，`members/<成员>/` 存成员原文和画像。配置 `OPENVIKING_URL` / `OPENVIKING_API_KEY` 即启用；连接超时或失败后进入冷却，直接走本地回退。
- **进入提示词的只有摘要**，上限 `PROFILE_PROMPT_MAX_CHARS`（默认 4000 字）；完整画像和历史留在 SQL/OpenViking。
- 历史数据重建：`python -m wechat_agent rebuild-memory`。

当前画像与"越聊越像"这条链路的评估见 [`docs/MEMORY_AUDIT.md`](docs/MEMORY_AUDIT.md)。

## 可选能力

默认全关，按需在 `.env` 打开：

- **联网搜索 / 网页读取**：`WEB_SEARCH_ENABLED=true`，支持 DeepSeek relay、Brave、Tavily、SearXNG；搜索结果只进当前回答上下文，不写入长期记忆。
- **生图**：`IMAGE_MODEL_ENABLED=true`，返回的图片存到 `state/generated/` 后作为原生图片消息发出；另可上传 OSS 换 URL。
- **抖音 / 小红书**：`PLATFORM_BROWSER_ENABLED=true` 打开独立持久化浏览器读取公开内容；写入还需 `PLATFORM_WRITE_ENABLED=true` 和二次确认，@ 目标只认平台唯一 ID。见 [`docs/PLATFORM_ADAPTERS.md`](docs/PLATFORM_ADAPTERS.md)。
- **训练语料导出**：`export-training-data` 从本地库导出 `style_corpus.jsonl`（群成员原话）和 `behavior_pairs.jsonl`（上下文 + 真实回复 + 行为标签）到被忽略的 `data/training/`；可以设 `TRAINING_EXPORT_ENABLED=true` 每晚自动导出。**只导数据，不训练权重**。

## 目录结构

```
src/wechat_agent/
  weixin_ui.py         微信适配器：数据库监听、UIA 回退、坐标/OCR 发送
  agent.py             消息入库、上下文组装、画像抽取、回复编排
  policy.py            触发规则、白名单、频率闸门、STOP
  storage.py           消息、事实、画像、去重台账、动作审计
  openviking_memory.py OpenViking 分级记忆读写与作用域过滤
  profiles.py          画像抽取
  profile_synthesis.py LLM 群画像合成
  prompt_profiles.py   画像 → 提示词摘要
  group_style.py       群聊风格统计
  persona_style.py     人设输出检查
  embedding.py         Embedding 合约
  image*.py            图片解密、视觉输入、生图落盘
  desktop.py           本地控制台
  platforms/           抖音 / 小红书适配层
  training_data.py     本地语料导出
migrations/            PostgreSQL / pgvector 迁移
personas/             人设（默认鲸鱼娘「大肥鱼」）
docs/                 环境变量、部署、审计、平台适配说明
```

## 已知限制

- **真实端到端未验收**：尚未完成真实微信群的自动回复联调，发送成功目前以客户端里实际出现消息为准。
- 选择器与坐标布局随微信版本变化，升级客户端后需要重新跑 `ready` 并复测发送。
- 个人号跑自动化违反平台协议、有封号风险；建议只用专用测试号，先在测试群里跑。
- 抖音/小红书连接器只完成唯一 ID 白名单校验，原生 @ 候选选择器未在真实页面验收，真实写入会安全阻断。
- 语料量不足（当前可用百余条），所以风格学习停在提示词级，没有做权重微调。

## 文档

- [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md)：全部环境变量与行为说明
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)：24 小时运行拓扑
- [`docs/PLATFORM_ADAPTERS.md`](docs/PLATFORM_ADAPTERS.md)：抖音/小红书连接器与安全边界
- [`docs/MEMORY_AUDIT.md`](docs/MEMORY_AUDIT.md)：记忆/画像链路审计
- [`STATUS.md`](STATUS.md)：当前进度、已验证项和阻塞点
