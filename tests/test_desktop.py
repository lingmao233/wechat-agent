import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechat_agent.agent import AgentStats
from wechat_agent.config import Settings
from wechat_agent.desktop import DesktopController, DesktopRequestHandler, DesktopServer, create_server


class FakeAgent:
    def __init__(self, settings):
        self.settings = settings
        self.stats = AgentStats()

    def run(self):
        while not self.settings.stop_file.exists():
            time.sleep(0.01)


def test_desktop_controller_start_stop_and_status(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_stop_file=tmp_path / "STOP",
    )
    controller = DesktopController(settings, agent_factory=FakeAgent)
    controller.start()
    for _ in range(50):
        if controller.status()["phase"] == "running":
            break
        time.sleep(0.01)
    assert controller.status()["thread_alive"] is True
    stopped = controller.stop()
    assert stopped["thread_alive"] is False
    assert stopped["stop_file_present"] is True


def test_desktop_http_status_and_token_guard(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        desktop_token="secret-token",
        wechat_stop_file=tmp_path / "STOP",
    )
    controller = DesktopController(settings, agent_factory=FakeAgent)
    server = DesktopServer(("127.0.0.1", 0), DesktopRequestHandler, controller, settings.desktop_token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/status"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 401
        request = Request(url, headers={"Authorization": "Bearer secret-token"})
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["phase"] == "stopped"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_non_local_desktop_bind_requires_token(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        desktop_host="0.0.0.0",
        desktop_token="",
        wechat_stop_file=tmp_path / "STOP",
    )
    with pytest.raises(ValueError, match="DESKTOP_TOKEN"):
        create_server(settings)
