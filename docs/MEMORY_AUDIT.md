# 记忆模块审计报告

审计时间：2026-10-28
审计对象：`wechat-agent` 记忆/画像链路 + 外接 OpenViking（v0.4.21，namespace `ds`）
审计方式：读代码 + 连真实 OpenViking 只读枚举 + 读本地 `state/memory.db` + 按当前代码重算画像与提示词体积

> 本报告中的群名、成员名已做匿名化（群A/群B、成员A/成员B）；精确 wxid 与原文见本地数据库。

---

## 一、结论

| 需求 | 结论 | 说明 |
|---|---|---|
| 记忆目录按「群 → 群员」 | **基本达成（结构正确）** | OpenViking 真实目录已验证为 `memories/{messages,facts,profiles}/accounts/<账号>/groups/<群>/members/<成员>/`，画像是群级 `ai_profile.md`、`group_style_profile.md` 与成员级 `user_profile.md`。 |
| 按记录生成用户画像 | **雏形，未达标** | 只有「正则抽取的少量显式偏好 + 计数器统计」。183 条消息只抽出 1 条偏好；主题分类只有软件/考试等 7 类，抓不到该群真实的游戏、彩票、八卦话题。 |
| 按群聊记录生成群聊 AI 画像 | **未达标（且当前数据是错的）** | AI 画像的「常用词」被 XML 传输字段污染（`lt`/`gt`/`msgsource`/`appmsg`/`title`…），画像文本几乎是噪声；`rules`、`avoid_phrasing` 永远为空，没有群文化/梗/称呼/角色，本质只是统计表。 |
| AI 依据画像改变聊天习惯、越来越贴合 | **未达成，且有反向效果** | 注入机制存在，但注入的风格结论与事实相反（"愿意给出较完整的句子、平均 159 字" vs 实际中位 11 字、75% 短句）；单次提示词里画像 JSON 约 14.8 万字符（≈7 万 token）；没有任何发送后评估/反馈回路；画像没有滑动窗口，永远对全历史取平均。 |

一句话：**目录骨架已经对了，画像与"越聊越像"这条链路还没真正跑通，而且目前会把 AI 往反方向带。**

---

## 二、需求逐条对照

### 2.1 记忆目录按群-群员 — 达成

真实 OpenViking 枚举结果（只读）：

```
viking://user/ds/peers/user-ds-peers-wechatagent/memories/
├── messages/accounts/<账号>/groups/<群A>/
│   ├── <message-hash>.json                  # 群级原文
│   └── members/<wxid>/<message-hash>.json   # 成员级原文副本
├── facts/                                   # 事实（当前为空，见 3.6）
└── profiles/accounts/<账号>/groups/<群A>/
    ├── ai_profile.md                        # 群级 AI 画像
    ├── group_style_profile.md               # 群级风格画像
    └── members/<wxid>/user_profile.md       # 成员画像
```

实现位置：[`openviking_memory.py`](../src/wechat_agent/openviking_memory.py) 的 `_scope_uri` / `put_raw_message` / `put_profile` / `search`。作用域字段 `account_id + group_id + member_id (+ task_id)` 在 SQL、OpenViking payload 和检索过滤三处一致，测试也覆盖了跨群/跨成员不召回。

遗留问题（不影响"目录达标"，但影响长期可用）：

1. `group_id` 在生产数据里是**群显示名**（183 条全部如此），不是稳定的 `xxx@chatroom`。群改名 → 记忆换作用域；两个同名群 → 串记忆。代码里 DB 监听路径会用 chatroom id，UIA 回退路径 `group_id = visible_group_id or group_name`（[`weixin_ui.py:794`](../src/wechat_agent/weixin_ui.py#L794)），两条路径的作用域键不一致。
2. `account_id` 硬编码 `wechat-test-account`（[`agent.py:566`](../src/wechat_agent/agent.py#L566)），换微信号会复用同一记忆空间。
3. 群B 有 56 条 `member_id=unknown`，这些消息进不了成员画像，只能进群级统计。
4. 画像文件扩展名是 `.md`，内容却是紧凑 JSON；人读不友好，语义索引也会按 Markdown 切块。
5. 原文镜像到 OpenViking 后**没有保留期**，本地 30 天删除，远端永久累积，也没有删除接口。

### 2.2 按记录生成用户画像 — 雏形

已有：
- 显式规则抽取：喜欢/偏好/不喜欢/关注/常用/必须/更正（[`profiles.py:22`](../src/wechat_agent/profiles.py#L22)）。
- 软行为统计：消息长度、短句、问号/感叹号、表情、口头禅、句尾、称呼、话题、常用词（[`profiles.py:153`](../src/wechat_agent/profiles.py#L153)，[`storage.py:2041`](../src/wechat_agent/storage.py#L2041)）。
- 版本与来源：显式更正会把同 key 旧事实标 `superseded`，保留历史。

不足：
- **语义内容极少**：183 条消息只产出 1 条偏好（"喜欢你"，来自成员对 AI 的一句玩笑，属于误报——见 3.3）。
- **话题体系不匹配**：只有「软件开发 / 模型与 AI / 部署与运维 / 测试与验证 / 设计与内容 / 学习与考试 / 娱乐与生活」7 类，实际群聊里的「鹅鸭杀、王者荣耀、刮彩票、猜名字、退群」大多落不进去，所以 `common_topics` 大量为空。
- **没有 LLM 总结**：没有"这个人是什么样的人、爱聊什么、怎么说话、和 AI 什么关系"的自然语言画像。
- **常用词被污染**：成员画像虽然用了 `_profile_text` 清洗 XML，但 `@AI名称` 没剥离，导致每个成员的 top 词都是 `away`（AI 自己的名字），不是成员特征。

### 2.3 按群聊生成群 AI 画像 — 未达标

已有：`ai_profiles` 表 + `group_style_profiles` 表，按 `account_id + group_id` 隔离，有证据消息、历史、刷新阈值。

问题（实测，见 3.3）：
- **数据被 XML 污染**：AI 画像的 top 词是 `lt(135) / gt(135) / type(34) / wxid_xxx(34) / msgsource(32) / appmsg(16) / title(16) / refermsg(16)`，不是群文化。
- **风格结论与事实相反**：本地重算 80 条样本，原始文本平均 180 字、"0% 短句"，结论是"愿意给出较完整的句子"；清洗后平均 13 字、"78% 短句"，结论应是"短句、节奏快"。
- **`reply_length` 被误判**：`average_length >= 80` 才判 `detailed`，线上因为 XML 拉高到约 140，于是 AI 画像给出 `reply_length: "detailed"`；按干净文本应判 `short`（[`storage.py:2134`](../src/wechat_agent/storage.py#L2134)）。
- **没有"画像"该有的内容**：群的梗、群友之间的称呼、AI 在群里的角色/外号、什么话题会冷场、期望的回复长度与语气、禁止话题——一个都没有。`accepted_phrasing` 只从固定 7 个词里挑，`avoid_phrasing` 与 `rules` 永远保持空（[`storage.py:2174`](../src/wechat_agent/storage.py#L2174)）。
- **口径错误**：画像文本写"基于最近 N 条"，但计数器是全量累加、永不衰减（[`storage.py:529`](../src/wechat_agent/storage.py#L529)），所以永远对"历史总和"取平均，无法随群聊漂移。

### 2.4 AI 根据画像改变习惯 — 未达成

机制存在：`agent.py` 把 `profile_context`（user/ai/task）和 `group_style_profile` 注入 system prompt（[`llm.py:77`](../src/wechat_agent/llm.py#L77)、[`llm.py:94`](../src/wechat_agent/llm.py#L94)）。

但实测结果是反向的：

| 指标 | 群成员消息 | AI 回复 |
|---|---|---|
| 中位长度 | 11 字 | 72 字 |
| 平均长度 | 19.7 字 | 86 字 |
| ≤16 字占比 | 75% | 0% |

也就是说：系统一边告诉模型"这个群平均 159 字、愿意写完整句子"，一边要求它贴合群聊——实际群是"一两句话、10 来个字"的快节奏。模型只能听提示词，于是继续发长回复。

同时，注入的内容并不是"画像"，而是数据库结构体：

- `profile_context` 单次约 **147,763 字符**（粗估 7 万 token），其中 AI 画像块占 **143,376 字符 / 83 条历史**，原因是 `_profile_dict` 把完整 `history`（每条含上一版全量快照）塞进返回体（[`storage.py:1012`](../src/wechat_agent/storage.py#L1012)），`llm.py` 又整体 `json.dumps` 注入。
- 里面包含 `evidence_message_ids`（几十个 sha256）、`source_message_ids`、计数器、历史版本——对模型没用，只稀释注意力、推高成本，并在历史涨到 100 条后逼近上下文上限。

此外没有任何闭环：
- 回复发出后不评估"是否像这个群"；
- 不记录回复长度/句数/口头禅命中情况；
- 不从群友对 AI 的后续反应里学习；
- 没有"目标长度区间"这类可直接约束生成的参数。

---

## 三、实测证据

### 3.1 OpenViking 连通与目录

- `/health` 200，`v0.4.21`，`auth_mode=api_key`，`user_id=ds`。
- 目录枚举见 2.1，`profiles/.../groups/<群A>` 下确实存在 `ai_profile.md`、`group_style_profile.md`、`members/<wxid>/user_profile.md`。
- 写入正常；读取（`/api/v1/search/find`）偶发失败：10-28 的运行日志里该接口报 `HTTPStatusError` 32 次 + `ConnectTimeout` 1 次，每次失败触发 300 秒冷却，期间远程召回全部跳过。客户端把状态码吞掉了（只记录异常类型），无法定位是 4xx 还是 5xx。

### 3.2 本地台账

- 消息 183 条，2 个群，6 份成员画像，1 份 AI 画像，1 份群风格画像，1 份任务画像。
- `memory_facts` 64 条：63 条是旧版本写入的 `memory_type=chat` 原始聊天行（例：`wxid_xxx:\n@AI名 说出某人的缺点`），1 条偏好。
- 这 63 条 `chat` 事实仍为 `confirmed`、`valid_until` 为空，**会继续被召回并进入"可用记忆"**；新代码已不再写这类事实，但没有清理/迁移。
- 183 条消息中 93 条带 `wxid_xxx:` 发送者前缀，10 条是 `<msg><emoji>` / `<appmsg>` XML 载荷（最长 1744 字符）。

### 3.3 画像质量

群风格画像（线上实际值）：

```
基于最近 80 条普通群消息的本地统计，当前群聊整体是：愿意给出较完整的句子、口语化、互动感较强、偶尔分行表达。
平均每条约 159 个字符；约 0% 的消息属于短句。
...
```

同一批消息用 `_profile_text` 清洗后重算：

```
基于最近 78 条普通群消息的本地统计，当前群聊整体是：短句、节奏较快、口语化、互动感较强。
平均每条约 13 个字符；约 78% 的消息属于短句。
```

AI 画像常用词（线上实际值）：

```
lt 135, gt 135, type 34, wxid_xxx 34, msgsource 32, wxid_xxx 25,
appmsg 16, title 16, refermsg 16, svrid 16, fromusr 16, chatusr 16 ...
```

清理后应为：

```
away 23, 肥鱼 3, 我今天去刮彩票 3, 你推荐一个数字 3,
你猜一下我的名字 2, 空空如也 2, token 2, 那你锐评一下我 2 ...
```

成员画像示例（净化后仍以 `away` 为第一高频词）：

```json
{"expression_habits": ["short_turns", "uses_哈哈", "ends_with_啊"],
 "behavior": {"message_count": 30, "average_message_length": 10.3,
              "common_topics": [], "expression_habits": [...]}}
```

误报示例：成员对 AI 说"我喜欢你"，被抽成长期偏好 `preferences: ["喜欢你"]`。抽取规则没有判断"是不是在对 AI 说 / 是不是玩笑 / 是否值得长期记忆"。

### 3.4 提示词体积

按当前代码 + 当前数据组装一次真实回复的输入：

| 组成 | 字符数 |
|---|---|
| 基础 system + 人设 | ~3,200 |
| `group_style_profile` | 209 |
| `profile_context` JSON | **147,763** |
| 最近上下文消息 | 5,738 |
| 检索记忆 | ~100 |
| **合计** | **~157,000（粗估 7 万 token）** |

其中 AI 画像块 143,376 字符，83 条 history；history 上限 100 条，涨满后单块约 15~16 万字符。模型接口目前返回 200，但这个体积已经接近"每条回复都在为台账付费"的程度，再叠加长上下文/多图就会超限。

### 3.5 是否"越来越符合"

- 群风格与 AI 画像都是**全量累计计数器**，没有滑动窗口/时间衰减，"最近 N 条"只是文案。
- 没有任何回复后评估或参数回写。
- 因此当前实现只能做到"把一份可能错误的统计贴进 prompt"，不具备随时间收敛到群聊习惯的机制。

### 3.6 记忆召回

- 语义检索从 `memories/` 根进入，取回 top-N 后再在客户端按 scope 过滤（[`openviking_memory.py:493`](../src/wechat_agent/openviking_memory.py#L493)）。群/成员变多后，top-N 很容易被其他作用域占满，命中率会下降。
- 检索结果包含目录 `.abstract.md`（无 `memory_id`，被丢弃）和整份画像 JSON（会被当成"记忆"塞进 prompt）。
- `facts/` 目录**当前为空**：库里唯一的偏好事实没有出现在 OpenViking。需要一条新的显式偏好消息实测确认当前代码的 `put_memory` 是否真的落盘。
- 事实被 `superseded` 时只改 SQL，不更新/删除 OpenViking 里的旧事实文件，旧值仍可能被召回。
- `OPENVIKING_COMMIT=true` 是死配置（只在 `config.py:115` 定义，从未使用）；OpenViking 自带的 `sessions/{id}/commit`→`extract`、`memory-templates`、`agent-evolution` 都没有被使用。

---

## 四、缺陷清单

### P0（不修则画像必错）

| # | 缺陷 | 位置 |
|---|---|---|
| 1 | 群风格统计直接用原始文本，未剥离 `wxid_xxx:` 前缀与 XML 载荷，长度/短句/标点/口头禅全部失真 | `storage.py:526` → `group_style.py:65` |
| 2 | AI 画像抽取同样用原始文本，常用词变成 XML 标签与 wxid | `storage.py:796` → `profiles.py:293` |
| 3 | `profile_context` 把完整 `history`/证据 ID 注入 prompt，单次 ~14.8 万字符 | `storage.py:1012` → `llm.py:94` |
| 4 | 旧 `memory_type=chat` 事实未清理，原文持续进入"可用记忆" | 数据迁移缺失 |
| 5 | 画像每次消息都全量写入 OpenViking（user + ai + 原文 1~2 份），同步阻塞消息链路 | `storage.py:745/850` → `openviking_memory.py:298` |

### P1（影响"越来越像"）

| # | 缺陷 | 位置 |
|---|---|---|
| 6 | 群风格/AI 画像无滑动窗口与衰减，"最近 N 条"与口径不符 | `storage.py:529`、`group_style.py:116` |
| 7 | AI 画像没有群文化/梗/称呼/AI 角色/回复长度带，`rules`、`avoid_phrasing` 永远为空 | `storage.py:2174` |
| 8 | 无发送后评估与反馈，回复长度中位 72 字 vs 群成员 11 字 | 无实现 |
| 9 | 检索从根目录 top-N 后过滤，跨作用域挤占；superseded 旧事实仍可召回 | `openviking_memory.py:493` |
| 10 | `group_id` 用群显示名，且 DB/UIA 两条路径键不一致；`account_id` 硬编码 | `weixin_ui.py:794`、`agent.py:566` |
| 11 | OpenViking 原文无保留期、无删除接口，与 30 天留存承诺不符 | `openviking_memory.py:358` |
| 12 | 桌面控制台只展示群风格画像，看不到成员画像、群 AI 画像、OpenViking 目录与 prompt 体积 | `desktop.py:789` |

### P2（体验/精度）

- `@AI` 名字未从成员词表剥离，所有成员 top 词都是 `away`。
- 显式偏好抽取会把玩笑/对 AI 说的话当长期偏好，需要加"对象/语境/置信度"门槛。
- 话题分类是软件工程导向，缺少游戏、彩票、考试、感情、群内八卦等类目或改为 LLM 聚类。
- `peer_id_for_scope` / `scope_uri` 与运行时的单 peer + 子目录方案并存，属于死代码，易误导。
- 画像文件 `.md` 内容是 JSON；建议画像正文写 Markdown，结构化数据另存。
- 文档与实际路径不一致：`docs/ENVIRONMENT.md` 写的是 `memories/accounts/...`，实际有 `messages|facts|profiles` 这一层 `kind`。
- 同一消息在群目录和成员目录各存一份，检索时重复命中，靠 `memory_id` 去重。

---

## 五、改造建议

### P0：先把"输入"弄干净，再把"输出"收窄（1~2 天）

1. **统一清洗函数** `normalize_chat_text()`，在入库前调用一次，所有下游（原文镜像、成员画像、群风格、AI 画像、检索记忆）共用：
   - 去掉行首 `wxid_xxx:` / `昵称：`；
   - `<msg><emoji ...>` → `[表情]`，`<appmsg><title>` → `[分享:标题]`，`<refermsg>` → `[引用:文本]`；
   - `@AI别名` → `@AI`（不参与成员词表）；
   - 纯媒体/系统行不入画像。
   - 原始文本保留在 `messages.raw_text` 供审计。
2. **新增 `build_prompt_profile()`**：只输出自然语言摘要（偏好/兴趣/工具/约束、行为特征、群风格、群 AI 规则），**不带 history/证据/计数器**，硬上限（建议 2,000 字符/画像，总 4,000）。`llm.py` 改为注入这份投影。
3. **清理旧 `chat` 事实**：迁移为 `archived` 或直接删除，避免旧原文继续召回。
4. **画像写入去抖**：SQL 每条消息更新，OpenViking 每 20 条或 5 分钟批量写一次；history 只留 SQL。

### P1：让 AI 画像真的"像这个群"（3~5 天）

5. **周期 LLM 画像生成**：取最近 200~300 条清洗后消息 + 上一版画像，输出结构化 JSON：
   ```json
   {"group_culture": "...", "inside_jokes": ["鹅鸭杀", "刮彩票", "猜名字"],
    "members": [{"name": "...", "nickname": "...", "role": "...", "style": "..."}],
    "ai_identity": {"nicknames": ["肥鱼", "大肥鱼", "死肥鱼"], "role": "被调戏的群宠"},
    "reply_shape": {"sentences": [1,2], "chars": [8,30], "emoji": "偶尔",
                    "markers": ["哈哈", "啊", "呀", "吧"]},
    "avoid": ["长篇大论", "说教", "政治敏感"],
    "rules": ["先接梗再回答", "被叫死肥鱼要回嘴"]}
   ```
   写入 `ai_profiles.profile_json`，同时把正文写成 Markdown 到 OpenViking（不要 JSON）。
6. **滑动窗口**：群风格与 AI 画像按最近 300 条或 30 天重算，保留全量台账但统计口径改成窗口；文案与口径一致。
7. **闭环自适应**：
   - 发送后记录 `reply_length / sentences / marker_hits`；
   - 每 20 条回复计算 `style_fit = |median(reply) - median(member)| / median(member)`；
   - 把目标区间写进 prompt（"本群中位 11 字，请 1~2 句、8~30 字"）；
   - 把群友对 AI 的追问/吐槽作为"反应信号"，在下一次画像刷新时作为证据。
8. **检索精确化**：`target_uri` 分别指向成员目录与群目录各查一次；只取 `content_type` 为文件的条目；提高 limit；把失败状态码写进日志；`superseded` 时同步删除/覆盖 OpenViking 旧文件。
9. **作用域稳定化**：`group_id` 统一用 chatroom id，`group_name` 只做展示；写一次 name→wxid 映射迁移历史数据；`account_id` 从登录账号读取。
10. **保留期**：OpenViking 原文按 30 天清理，提供删除接口；控制台加"记忆"页展示三类目录、画像正文、prompt 体积。

### 验收标准（建议直接写进测试）

- 清洗后该群：平均长度 ≤ 30 字、短句比例 > 50%、top 词不含 XML 标签与 wxid。
- 单次 prompt 中画像部分 ≤ 4,000 字符。
- AI 回复中位长度落在群成员中位长度 ±50% 区间，且 ≤2 句的比例 > 70%。
- 新增 100 条消息后，画像文本出现可观测漂移（不是全量平均不动）。
- OpenViking 三类目录均可列出，`facts/` 非空，`superseded` 旧值不再召回。
- 跨群/跨成员召回仍为 0（现有测试保持通过）。

---

## 六、测试与运行状态

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`：**52 passed**（约 58s）。
- 本地 `python -X utf8` 可正常读取 `state/memory.db`；OpenViking 只读探测正常。
- 真实微信端到端仍未验收（`WECHAT_ALLOW_SEND=true` 已开，但 `state/STOP` 存在，最后运行停在 10-28 13:32）。
- 现有测试只覆盖作用域隔离、偏好多版本、群风格刷新、画像 history 存在性，**没有覆盖文本清洗、prompt 体积、回复长度贴合度、OpenViking 检索命中率**——这也是 P0 缺陷能一直通过测试的原因。

---

## 七、附：关键调用链

```
poll()
 └─ handle_event()
     ├─ record_message()                      # 原文入库（未清洗）
     ├─ mirror_raw_message()                  # OpenViking 原文（群级 + 已知成员级各一份）
     ├─ _ingest_scoped_memory()
     │   ├─ extract_memory_candidates()       # 显式偏好（少量）
     │   ├─ extract_member_profile_patch()    # 清洗后的软行为统计
     │   ├─ upsert_user_profile()  ──────────→ OpenViking put_profile(user) 每条消息
     │   ├─ observe_ai_profile()   ──────────→ OpenViking put_profile(ai)   每条消息
     │   └─ upsert_task_profile()  ──────────→ OpenViking put_profile(task)
     ├─ observe_group_style()                 # 未清洗，每 20 条刷新一次
     └─ _reply_with_context()
         ├─ search_scoped_memories()          # 本地混合召回 + OpenViking 根目录检索
         ├─ user_profile()/ai_profile()/task_profile()   # 含 history，直接注入
         ├─ group_style_profile()             # 含错误风格结论
         └─ LLMClient.reply()                 # profile_context 整体 json.dumps
```

---

## 八、修复落地与复测（2026-10-28）

> 第一至七节是修复前基线。本节记录按该基线完成的实际改动与真实数据复测；旧结论保留用于对照。

### 8.1 已落地改动

**P0：文本与画像链路**

- 新增 [`textnorm.py`](../src/wechat_agent/textnorm.py)：统一 `normalize_chat_text()`。只去掉可确认的 `wxid_xxx:` 或已知发送者前缀（不误删“注意：”这类正文），XML 转 `[表情]/[图片]/[分享]/[引用]`，`@别名` 归一为 `@AI`，系统/时间行标记 `kind=system`；原文始终保留在 `messages.text`，派生文本写入 `text_normalized`。
- `messages` 新增 `text_normalized`、`text_kind`、`text_meta_json`；启动时自动补列。发送者身份仍来自 `member_id`/`member_name` 结构化列，**不再依赖文本前缀**。
- 画像统计只读 `text_normalized`；`recent_messages`/`related_messages` 用清洗文本并跳过系统行。当前消息进入检索、任务抽取、联网研究和 LLM prompt 前也统一清洗（含被引用消息），模型不再看到 `wxid_xxx:` 或 XML。
- 群风格与 AI 画像改为**滑动窗口重建**（默认最近 300 条），`message_count` 与“最近 N 条”标签一致；不再是永久累加的全历史平均。
- 新增 [`prompt_profiles.py`](../src/wechat_agent/prompt_profiles.py)：只向 prompt 注入 ≤4,000 字的自然语言摘要，丢弃 `history`、`evidence_message_ids`、原始计数器；旧 `profile_context` JSON 仅作兼容回退。
- 历史 `memory_type=chat` 的 63 条噪声事实统一标为 `archived`，检索路径显式排除 `chat` 类型。
- OpenViking 历史 `raw_message` 在召回读取时按 `raw_message` 类型二次清洗前缀/XML，避免远端旧数据绕过本地归一化进入 prompt。
- 新增 `python -m wechat_agent rebuild-memory [--window N]`：回填清洗文本、归档旧事实、重建全部群/成员画像。
- 自回声防护：除 `unknown` 文本比对，还把配置里的 bot 名称/别名视为 self；重建时按已发送回复文本跳过历史回声。

**P1：语义画像与反馈回路**

- 新增 [`profile_synthesis.py`](../src/wechat_agent/profile_synthesis.py)：用最近群消息调用 LLM 生成结构化群画像（`culture`、`reply_shape`、`reply_examples`、`nicknames`、`jokes`、`taboos`、`rules`、`member_notes`），严格 JSON 校验、失败时用更短 schema 重试；结果存入 `ai_profiles.profile_json.synthesized`，成员印象写入 `user_profiles.profile_json.synthesized_note`。
- `rebuild-memory` 会触发一次语义合成；群风格每 20 条刷新后，也会在后台线程按需刷新，不阻塞回复。
- 新增风格贴合反馈：发送成功后 `record_reply_style()` 按群目标长度区间记录 `style_fit`（样本数、命中区间数、平均长度），并即时重写群风格文本；`build_group_style_profile()` 增加“近期 AI 回复平均 N 字、X% 落在建议区间”。

### 8.2 真实数据复测（183 条消息，2 个群）

| 指标 | 修复前（审计基线） | 修复后（本地库实测） |
|---|---|---|
| 群风格平均长度 | 159 字（原始 XML/前缀） | **7.9 / 10.1 字** |
| 群风格短句比例 | 0% | **86% / 88%** |
| 群风格目标区间 | 无 | **5~30 字** |
| AI 画像 top 词 XML 污染 | `lt/gt/type/wxid/msgsource/appmsg/title` | **0 个 XML token** |
| AI 画像 `reply_length` | `detailed` | **short** |
| 单次 prompt 画像体积 | 147,763 字符 | **438~544 字符**（含群友段；仅群 AI 段 374~465） |
| 群级语义画像 | 无 | **2/2 群**，8 个字段（culture/jokes/rules/…） |
| 历史 chat 噪声事实 | 63 条活跃 | **63 条 archived** |
| 测试 | 52 passed | **76 passed**（新增 textnorm / prompt / synthesis / style-fit / prompt-input / policy-normalization / legacy-fact / remote-raw） |

补充：成员消息中位长度 10 字，历史 AI 回复中位 83.5 字（54 条旧回复）。新回复的目标区间与示例已改为 5~30 字；该指标要等真实运行后才会下降，不能把旧回复算作修复后效果。

### 8.3 仍需注意

- `facts/` 目前仍只有 1 条显式偏好。成员“偏好”抽取依旧依赖正则，语义画像补的是群文化/成员印象；要把“用户画像”做厚，下一步应让 LLM 按成员汇总（P1 剩余项）。
- OpenViking 中历史 `messages/` 原文仍是旧 XML（新消息已清洗）；远程召回时已按 `raw_message` 在读取侧二次清洗，不会再进 prompt。若要让远端目录内容本身也干净，可再做一次 raw resync（会重写历史消息文件）。
- 反馈回路已能记录 `style_fit`，但还没有“连续偏离时自动收紧目标区间”的策略；当前是下一轮 prompt 直接读目标区间，属于单步闭环。
- 真实微信端到端仍未跑（`state/STOP` 存在）；本次只做本地库重建与只读验证，未发送任何消息。

### 8.4 风格包、训练数据与主动发言补充

- 语义画像新增 `bit_routines`、`tease_patterns`、`comfort_patterns`、`meme_triggers`、`message_shape`；本次真实数据合成后两个群均写入前四项中的可用字段，但语料里没有真实安慰样本，`comfort_patterns` 为空，因此不伪造。
- 注入提示词的画像摘要实测约 **737 / 582 字**，低于 4000 字上限；回复示例已去掉泡泡口癖并限制表情数量。
- 新增 `export-training-data`：183 条原始消息中导出 **133 条**可用消息（排除自身回显、系统消息、疑似机器人文本和占位），行为计数 `joke=4, ordinary=122, question=7`。导出只读、匿名化成员/群标签、不联网；消息正文仍可能含昵称等个人信息，需人工检查。
- 新增人设输出检查和主动发言策略：主动发言默认关闭，实际 `.env` 已显式设为 300 秒冷却、每小时 6 次、概率 0.4；当前 `state/STOP` 仍存在，因此没有真实发送。
- 新增 `--today` 按本地日历日导出；常驻进程可配置每天 23:30 自动导出当天语料到 `data/training/daily/<日期>/`，`.exported` 标记防止重启重复，停机后补导前一天。只导出，不自动训练权重。
- 新增提示词自曝拦截：系统提示和 `SKILL.md` 明确禁止复述规则/过滤词表；输出后处理会把“我不会吐泡泡”“我被禁止过激侮辱”“我的提示词……”改写为自然说法。
- 桌面控制台改为简洁中性色，并把主动发言间隔/上限/概率、人设检查、表情上限和每晚导出时间放进了常用设置；概览页直接显示主动发言节奏和导出时间。
- 全量测试 **96 passed**。
