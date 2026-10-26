from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlparse

from .agent import WeChatAgent
from .config import Settings
from .platforms.dry_run import DryRunPlatformAdapter
from .platforms.service import PlatformService
from .storage import Storage


class DesktopController:
    """Small local control plane for the Windows connector.

    The dashboard deliberately controls the same guarded ``WeChatAgent`` used
    by the CLI. It does not expose credentials or provide a second sending
    path. A server deployment can put the core service behind its own proxy;
    the Windows UI connector remains the process that owns the Weixin session.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        agent_factory: Callable[[Settings], WeChatAgent] = WeChatAgent,
    ):
        self.settings = settings
        self._agent_factory = agent_factory
        self._storage = Storage(settings)
        self._storage.init_schema()
        self._platforms = PlatformService(
            self._storage,
            {
                "douyin": DryRunPlatformAdapter("douyin"),
                "xiaohongshu": DryRunPlatformAdapter("xiaohongshu"),
            },
        )
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._agent: WeChatAgent | None = None
        self._phase = "stopped"
        self._last_error: str | None = None
        self._started_at: str | None = None

    def start(self) -> dict[str, object]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self.settings.stop_file.unlink(missing_ok=True)
            self._phase = "starting"
            self._last_error = None
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._agent = None
            self._thread = threading.Thread(target=self._run_agent, name="wechat-agent", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, timeout: float = 8.0) -> dict[str, object]:
        self.settings.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.stop_file.write_text("stop\n", encoding="utf-8")
        with self._lock:
            thread = self._thread
            if thread and thread.is_alive():
                self._phase = "stopping"
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                if self._phase != "error":
                    self._phase = "stopped"
            return self.status()

    def clear_stop(self) -> dict[str, object]:
        self.settings.stop_file.unlink(missing_ok=True)
        with self._lock:
            if self._phase == "stopping":
                self._phase = "stopped"
        return self.status()

    def status(self) -> dict[str, object]:
        with self._lock:
            thread_alive = bool(self._thread and self._thread.is_alive())
            agent_stats = asdict(self._agent.stats) if self._agent else {}
            phase = self._phase
            if phase == "starting" and thread_alive and self._agent:
                phase = "running"
            return {
                "phase": phase,
                "thread_alive": thread_alive,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "group_configured": bool(self.settings.wechat_group_name.strip()),
                "group_name": self.settings.wechat_group_name,
                "send_enabled": self.settings.wechat_allow_send,
                "stop_file_present": self.settings.stop_file.exists(),
                "stats": agent_stats,
                "processing": self._storage.processing_summary(),
            }

    def audits(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_audits(limit)

    def replies(self, limit: int = 50) -> list[dict[str, object]]:
        return self._storage.recent_reply_records(limit)

    def platform_status(self) -> dict[str, dict[str, object]]:
        return self._platforms.login_status()

    def platform_actions(self, limit: int = 50) -> list[dict[str, object]]:
        return self._platforms.actions(limit)

    def _run_agent(self) -> None:
        try:
            agent = self._agent_factory(self.settings)
            with self._lock:
                self._agent = agent
                self._phase = "running"
            agent.run()
        except Exception as exc:  # surfaced in the dashboard for diagnosis
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                self._phase = "error"
        finally:
            with self._lock:
                if self._phase != "error":
                    self._phase = "stopped"


class DesktopServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address, handler, controller: DesktopController, token: str):
        super().__init__(server_address, handler)
        self.controller = controller
        self.token = token.strip()


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server: DesktopServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # The console has its own audit/error view; avoid duplicating request
        # text into stdout where it may contain chat content.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(DASHBOARD_HTML)
        elif path == "/api/status":
            self._send_json(self.server.controller.status())
        elif path == "/api/audit":
            self._send_json(self.server.controller.audits())
        elif path == "/api/replies":
            self._send_json(self.server.controller.replies())
        elif path == "/api/platforms":
            self._send_json(self.server.controller.platform_status())
        elif path == "/api/platform-actions":
            self._send_json(self.server.controller.platform_actions())
        else:
            self._send_json({"error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/start":
            payload = self.server.controller.start()
        elif path == "/api/stop":
            payload = self.server.controller.stop()
        elif path == "/api/clear-stop":
            payload = self.server.controller.clear_stop()
        else:
            self._send_json({"error": "not_found"}, 404)
            return
        self._send_json(payload)

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied.removeprefix("Bearer ").strip()
        supplied = supplied or self.headers.get("X-Desktop-Token", "")
        if supplied == token:
            return True
        self._send_json({"error": "unauthorized"}, 401)
        return False

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def create_server(settings: Settings, controller: DesktopController | None = None) -> DesktopServer:
    host = settings.desktop_host.strip() or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"} and not settings.desktop_token.strip():
        raise ValueError("DESKTOP_TOKEN 必须在桌面控制台绑定非本机地址时设置")
    controller = controller or DesktopController(settings)
    return DesktopServer((host, settings.desktop_port), DesktopRequestHandler, controller, settings.desktop_token)


def serve(settings: Settings, *, open_browser: bool = False) -> None:
    server = create_server(settings)
    url = f"http://{settings.desktop_host}:{settings.desktop_port}/"
    print(f"desktop control console: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.stop()
        server.server_close()


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WeChat AI Agent 控制台</title>
  <style>
    body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:1100px;margin:32px auto;padding:0 18px;color:#202124;background:#f6f7f9}
    .card{background:white;border:1px solid #e2e5e9;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 2px 8px #0000000a}
    button{border:0;border-radius:8px;padding:9px 14px;margin-right:8px;cursor:pointer;background:#1f6feb;color:white}
    button.stop{background:#cf222e}.muted{color:#667085}pre{white-space:pre-wrap;word-break:break-word;background:#f6f8fa;padding:12px;border-radius:8px;max-height:320px;overflow:auto}
  </style>
</head>
<body>
  <h1>WeChat AI Agent 控制台</h1>
  <p class="muted">控制本机 Windows 微信连接器。所有发送仍经过群白名单、STOP 和审计策略。</p>
  <div class="card"><button onclick="post('/api/start')">启动监听</button><button class="stop" onclick="post('/api/stop')">停止监听</button><button onclick="post('/api/clear-stop')">清除 STOP</button></div>
  <div class="card"><h2>状态</h2><pre id="status">加载中…</pre></div>
  <div class="card"><h2>回复台账</h2><pre id="replies">加载中…</pre></div>
  <div class="card"><h2>动作审计</h2><pre id="audit">加载中…</pre></div>
  <div class="card"><h2>抖音 / 小红书</h2><pre id="platforms">加载中…</pre><pre id="platformActions">加载中…</pre></div>
  <script>
    async function get(path){const r=await fetch(path);return r.json()}
    async function post(path){await fetch(path,{method:'POST'});await refresh()}
    async function refresh(){
      document.getElementById('status').textContent=JSON.stringify(await get('/api/status'),null,2);
      document.getElementById('replies').textContent=JSON.stringify(await get('/api/replies'),null,2);
      document.getElementById('audit').textContent=JSON.stringify(await get('/api/audit'),null,2);
      document.getElementById('platforms').textContent=JSON.stringify(await get('/api/platforms'),null,2);
      document.getElementById('platformActions').textContent=JSON.stringify(await get('/api/platform-actions'),null,2);
    }
    refresh();setInterval(refresh,3000);
  </script>
</body>
</html>"""
