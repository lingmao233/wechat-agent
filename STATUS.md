# 微信 AI Agent 当前状态

更新时间：2026-10-26  
结论：**离线原型已完成，真实微信端到端联调尚未通过。** 当前没有发送微信消息、@、评论或私信。

## 已完成的代码

| 文件 | 已完成内容 |
|---|---|
| [`src/wechat_agent/weixin_ui.py`](src/wechat_agent/weixin_ui.py) | Weixin 4.1.x UIAutomation 适配器；窗口连接、打开群、读取可见文字、文本发送、@ 输入 |
| [`src/wechat_agent/agent.py`](src/wechat_agent/agent.py) | 消息入库、聊天事实、简单兴趣抽取、时间召回、模型回复和动作编排 |
| [`src/wechat_agent/storage.py`](src/wechat_agent/storage.py) | SQLite/PostgreSQL SQLAlchemy 表、原文留存、事实、身份绑定、动作审计、过期清理 |
| [`src/wechat_agent/policy.py`](src/wechat_agent/policy.py) | 群白名单、成员白名单、`@AI` 必答、普通消息相关性门槛、发送开关、速率熔断、STOP 文件 |
| [`src/wechat_agent/llm.py`](src/wechat_agent/llm.py) | DeepSeek/OpenAI 兼容调用；没有 Key 时使用离线 stub |
| [`src/wechat_agent/config.py`](src/wechat_agent/config.py) | `.env` 配置、30 天原文留存和 14/90/365 天半衰期参数 |
| [`migrations/001_pgvector.sql`](migrations/001_pgvector.sql) | PostgreSQL/pgvector 迁移草案 |
| [`tests/`](tests/) | 记忆隔离、时间召回、去重、策略和无 Key stub 测试 |

## 已验证结果

- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q`：**6 passed**。
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
3. 进入主窗口后，当前 UIAutomation 查询只看到 `Qt51514QWindowIcon` 和渲染窗格，未稳定暴露适配器需要的 `MainView`。
4. 因此 `python -m wechat_agent ready` 当前不能通过，尚未进入读取消息和发送验证阶段。

## 当前可以做什么

### 已可离线运行

- 用构造的消息事件测试群/成员隔离。
- 生成聊天事实和简单兴趣事实。
- 按时间半衰期和关键词召回。
- 在无模型密钥时生成 stub 回复。
- 审计允许和拒绝的发送动作。
- 用 STOP 文件停止监听。

### 当前还不能宣称可用

- 不能宣称已完成真实微信群自动回复。
- 不能确认 4.1.15.56 上消息行选择器、输入框和发送按钮长期稳定。
- 不能确认真实客户端中自动 @ 选择和发送后的成功回执。
- 不能宣称已经接入 PostgreSQL 向量检索。
- 不能宣称已经接入抖音、B 站、小红书采集或评论。

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
- 必须设置 `WECHAT_GROUP_NAME`
- `state/STOP` 存在时停止监听
- 发送动作写入 `action_audit`
- 只处理配置群和配置成员

详细需求与验收标准见 [`PRD.md`](PRD.md)。
