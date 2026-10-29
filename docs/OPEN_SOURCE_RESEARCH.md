# 联网搜索与平台采集方案核验

核验日期：2026-10-27。这里记录的是公开 README、源码入口和 LICENSE 的静态核验结果，不代表本仓库已经安装或跑通这些项目。

## 采用的边界

- 联网问答优先使用明确的搜索服务（Brave、Tavily 或自建 SearXNG），搜索结果带标题、URL、摘要，按当前问题注入模型上下文。
- 页面正文抓取只有在 `WEB_FETCH_ENABLED=true` 时启用；`WEB_FETCH_ALLOWED_DOMAINS` 留空允许公开 HTTP(S) 网站，填写后才收窄到指定域名。本机和内网地址仍阻断。搜索结果、网页正文和平台评论不会写入群友的长期画像。
- 抖音和小红书内容采集保留在可见浏览器适配器中。用户在群里明确询问平台内容时，才按查询调用当前已登录的页面，读取公开条目和公开评论；遇到登录、验证码、IP 风险或安全验证立即停止。
- 评论和回复仍走现有动作台账、白名单和人工确认。任何仓库都不能证明可以稳定绕过平台风控或获得官方评论 API。

## GitHub 候选

| 项目 | 许可证/维护证据 | 能力边界 | 本项目处理方式 |
|---|---|---|---|
| [chenkezhen480/dsh-world](https://github.com/chenkezhen480/dsh-world) | README、`package.json` 和根目录 LICENSE 均标注 MIT；README 当前版本 0.5.3，`package.json` 为 0.5.6 | 跨会话近期记忆、按天原文归档、日记、`我是谁` 人设自述、世界控制台、Windows 微信独立桥；公开 README 明确提醒个人微信桥使用本地数据库/DLL，存在账号风险 | 参考其“记忆归档 + 人设控制台 + 独立渠道桥 + 出站队列/回执”的边界；不复制其原生微信桥，不把它的真实微信能力当成本仓库已验证能力 |
| [c1overzz/dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) | README 声明 MIT；根目录有 MIT LICENSE；README 展示定时检索、字幕/评论读取、报告和人工开启评论 | 目标是 B 站插件，不是抖音或小红书接口 | 只借鉴任务编排、报告、幂等和人工确认边界，不复制平台代码 |
| [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | README 链接 Apache-2.0；`pyproject.toml` 标注 Apache-2.0；提供 REST/MCP、自托管归档和公开作品/作者/评论采集 | 适合公开抖音内容采集、归档和 AI 调用；写入权限、账号评论稳定性仍需单独核验 | 可作为独立采集服务或 MCP 的能力参考；暂不把它作为本仓库的写入依赖 |
| [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) | 当前静态读取未找到可确认的根目录 LICENSE | README 自述包含 Cookie/私有签名、作品/评论/私信和写入接口 | 仅作能力对照，禁止直接复制或作为生产依赖；私有接口、Cookie 和签名会随平台变化 |
| [63can/xiaohongshu-spider](https://github.com/63can/xiaohongshu-spider) | 当前静态读取未找到可确认的根目录 LICENSE；README 依赖 Fiddler、ChromeDriver 和小红书小程序抓包 | 可抓取文章/视频、评论/回复，但方案较旧，依赖抓包文件 | 仅作历史能力参考，不接入代码 |
| [Big-Buffer/XiaohongshuSpider](https://github.com/Big-Buffer/XiaohongshuSpider) | 当前静态读取未找到可确认的根目录 LICENSE | README 依赖 Appium、mitmproxy 和 Android 模拟器，重点是抓包图片/文章数据 | 仅作历史能力参考，不接入代码 |
| [mem0ai/mem0](https://github.com/mem0ai/mem0) | MIT；事实抽取、更新和多种向量存储适配较成熟 | 重点是通用用户/应用记忆，不知道本项目的微信账号、群、成员隔离和发送审计 | 借鉴事实更新与冲突处理思路；不直接替换现有存储，避免把跨群记忆混在一起 |
| [getzep/graphiti](https://github.com/getzep/graphiti) | Apache-2.0；时间知识图谱和事实失效建模较完整 | 需要图数据库和 embedding/LLM 依赖，部署复杂度明显高于当前测试群原型 | 作为后续长期画像/关系记忆的候选，不作为当前运行时依赖 |
| [letta-ai/letta](https://github.com/letta-ai/letta) | Apache-2.0；带可持久化上下文和工具的 agent 运行时 | 整体运行时较重，边界与本项目的微信 UI 连接器不同 | 只参考记忆分层和显式更新机制，不复制 agent 主循环 |

当前核实到的 `dsh-world` 是 `chenkezhen480/dsh-world`。现有实现借鉴的是身份隔离、任务队列、审计和桌面控制台的结构思想，记忆库则采用更严格的 `account_id + group_id + member_id` 命名空间和可审计事实记录。当前默认不需要向量模型：小规模测试群用关键词重合、置信度和时间衰减即可；要做中文语义相似召回时，再增加 embedding 模型和 PostgreSQL/pgvector，并保留 lexical fallback。

## 为什么没有直接抄爬虫

公开平台的采集项目通常分成两类：浏览器/页面采集可以作为低频、可见、人工登录的读取参考；Cookie、私有签名或逆向接口项目即使能读评论，也不能证明长期稳定，更不能证明获得了平台授权。许可证缺失的仓库不能直接复制代码，代码许可证也不等于平台内容的转载或商业使用授权。

本仓库当前实现的是受控的 `WebResearchClient` 和可见浏览器采集桥接：模型只能收到当前请求的来源内容，平台动作依旧需要独立的人工确认和风险停机。
