# 24 小时运行拓扑

当前微信适配器使用 Windows UIAutomation，必须在有桌面会话、已登录微信的 Windows 主机运行。普通 Linux 服务器不能直接替代这个连接器。

建议把长期运行拆成两部分：

```text
Windows 微信连接器
  ├─ 读取可见消息
  ├─ 发送经策略批准的消息
  └─ 保持微信登录态
          │ HTTPS + Token
          ▼
服务器核心服务
  ├─ LLM 调用
  ├─ PostgreSQL/pgvector
  ├─ reply_records 与 action_audit
  ├─ 抖音/小红书浏览器任务
  └─ 控制台与监控
```

本地控制台可用 `python -m wechat_agent desktop` 启动，默认只绑定 `127.0.0.1:8765`。绑定到其他地址前必须设置 `DESKTOP_TOKEN`，再通过 HTTPS 反向代理访问。生产部署还需要把 `state/`、数据库和浏览器登录态放在持久化卷中，配置单独的 STOP/健康检查和进程守护。

本阶段没有执行服务器登录、Docker 重启或远端配置写入。部署前要先确定服务器是 Linux 核心节点加 Windows 连接器，还是带 GUI 的 Windows VM，并分别验证网络可达性、微信登录态、模型调用和客户端可见发送结果。
