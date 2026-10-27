# 抖音与小红书适配边界

当前仓库先完成微信的消息幂等、明确触发和桌面控制。抖音与小红书放在独立平台适配层，沿用 `dsh-world` 中“统一身份、会话、出站队列和审计”的结构思路；参考 `c1overzz/dsh-bili-scroll` 的“本机持久化登录态、窄工具面、动作台账和发送后可见性核对”做法，但不复制平台私有签名或绕过验证码、风控和登录保护的代码。

当前代码提供两种实现：默认的 fixture-backed `dry-run` 适配器，以及显式开启 `PLATFORM_BROWSER_ENABLED=true` 后使用 Playwright 的可见浏览器适配器。后者为抖音和小红书各使用一个持久化目录，支持人工扫码登录、有限滚动读取公开内容、读取带稳定 DOM ID 的评论，并把评论/回复放入 `platform_actions` 队列。真实写入还需要 `PLATFORM_WRITE_ENABLED=true`、计划记录的 `dry_run=false` 和控制台二次确认。

## 统一接口

后续适配器按平台提供以下能力：

- `login_status()`：只报告当前浏览器登录态；
- `browse(query, limit)`：浏览公开内容；
- `read_comments(target_id, limit)`：读取公开评论；
- `comment(target_id, text)`：创建评论计划；
- `reply(comment_id, text)`：创建回复计划。

评论和回复默认 `dry_run=true`，先进入动作台账并要求人工确认。真正执行时必须经过账号绑定、目标白名单、频率限制、幂等键、异常熔断和证据记录。评论 ID 或目标 URL 必须来自当前页面读取结果，不能只按昵称猜测。

### @ 目标白名单

抖音和小红书分别使用 `DOUYIN_MENTION_ALLOWLIST` 与
`XIAOHONGSHU_MENTION_ALLOWLIST`。每个条目只写唯一的平台用户 ID，多个条目用逗号分隔。例如：

```ini
DOUYIN_MENTION_ALLOWLIST=uid_123,uid_456
XIAOHONGSHU_MENTION_ALLOWLIST=xhs_789
```

评论或回复文本出现 `@张三` 时，调用方还必须提交从当前页面选择出的 `mention_ids=["uid_123"]`；动作规划器只按唯一 ID 核对白名单。昵称不参与授权，也不能单独放行动作。没有 ID、白名单为空或 ID 不在白名单时，动作不会进入队列。`ACCOUNT_ID` 只是发信账号的审计命名，平台登录仍通过可见浏览器人工扫码完成。

服务层会把通过核对的 ID 写入 `mention_target_ids` 和 `mention_allowlist_checked`。真实浏览器执行目前还缺少抖音/小红书原生提及候选选择器，因此带提及 ID 的动作会在执行阶段安全阻断，而不会发送普通文本冒充原生 @。

## 桌面端操作

复制 `.env.example` 到 `.env` 后打开：

```powershell
$env:PLATFORM_BROWSER_ENABLED="true"
$env:PLATFORM_WRITE_ENABLED="false"
python -m wechat_agent desktop --open-browser
```

在控制台点击“打开抖音”或“打开小红书”，分别完成平台自己的登录。随后可以滚动读取内容和评论。评论/回复计划不会直接发出；需要先把计划建成 `dry_run=false` 的队列记录，再在控制台粘贴 `action_key` 二次确认。遇到验证码、安全验证、行为异常或访问受限页面时，连接器会停止当前动作并保留错误记录。

## 平台实现顺序

1. 用持久化浏览器登录态完成公开页面浏览和评论读取。
2. 在测试账号上只生成评论/回复计划，不发送。
3. 验证动作台账和页面证据后，再为单个平台开放人工确认发送。
4. 抖音和小红书分别维护选择器、会话目录和速率配置，不能共用登录态。

`dsh-bili-scroll` 是 B 站插件，MIT 许可；它的 `vendor/bilibili-mcp` 提供 B 站专用 API，不能直接当成抖音或小红书接口。当前项目只借鉴其架构和安全闸门，未复制其源代码。

平台页面结构会变化，真实适配需要在专用账号和真实网络环境中逐个平台验收。没有完成页面可见结果和动作台账核对前，不能称为评论或回复已接入。
