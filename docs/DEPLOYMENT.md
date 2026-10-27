# 24 小时运行拓扑

当前微信适配器使用 Windows UIAutomation，抖音/小红书适配器使用可见浏览器，都必须在有桌面会话的 Windows 主机运行。普通 Linux 服务器不能直接替代这个连接器。

建议把长期运行拆成两部分：

```text
Windows 桌面连接器
  ├─ 读取可见消息
  ├─ 发送经策略批准的消息
  └─ 保持微信登录态
  ├─ 保持抖音/小红书独立登录态
  └─ 执行有限浏览与人工确认后的平台动作
          │ HTTPS + Token
          ▼
服务器核心服务
  ├─ LLM 调用
  ├─ PostgreSQL/pgvector
  ├─ reply_records 与 action_audit
  ├─ 抖音/小红书任务编排与台账
  ├─ 可选的 OSS 生图结果上传与公开/签名 URL 转发
  └─ 控制台与监控
```

本地桌面 UI 可用 `python -m wechat_agent desktop --open-browser` 启动，默认只绑定 `127.0.0.1:8765`。页面可以启动/停止监听、编辑 `.env` 并校验重载、查看实时事件和审计台账。绑定到其他地址前必须设置 `DESKTOP_TOKEN`，再通过 HTTPS 反向代理访问。生产部署还需要把 `state/`、数据库和浏览器登录态放在持久化卷中，配置单独的 STOP/健康检查和进程守护。

本阶段没有执行服务器登录、Docker 重启或远端配置写入。部署前要先确定服务器是 Linux 核心节点加 Windows 连接器，还是带 GUI 的 Windows VM，并分别验证网络可达性、微信及平台登录态、模型调用和客户端可见发送结果。

生图上传要求运行节点能访问阿里云 OSS；`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` 和 bucket 配置保存在运行节点的 `.env`。wechat-agent 直接使用 `alibabacloud-oss-v2==1.4.0` 上传对象并生成公开或签名 URL。生产环境应为运行账号授予目标 bucket 的最小权限，并将 `OSS_PUBLIC_BASE_URL` 留空以使用私有 bucket 签名 URL，或明确配置公开 CDN/OSS 域名。
