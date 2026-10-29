import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechat_agent.agent import AgentStats
from wechat_agent.config import Settings
from wechat_agent.desktop import DASHBOARD_HTML, DesktopController, DesktopRequestHandler, DesktopServer, create_server


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
        for path in ("/api/platforms", "/api/platform-actions"):
            platform_request = Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                headers={"Authorization": "Bearer secret-token"},
            )
            with urlopen(platform_request) as response:
                platform_payload = json.loads(response.read().decode("utf-8"))
            assert platform_payload is not None
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


def test_desktop_saves_reply_frequency_and_model_controls(tmp_path):
    config_path = tmp_path / ".env"
    config_path.write_text(
        f"DATABASE_URL=sqlite:///{tmp_path / 'memory.db'}\nWECHAT_STOP_FILE={tmp_path / 'STOP'}\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_stop_file=tmp_path / "STOP",
    )
    controller = DesktopController(settings, agent_factory=FakeAgent, config_path=config_path)

    result = controller.save_config(
        {
            "values": {
                "WECHAT_REPLY_COOLDOWN_SECONDS": "12.5",
                "WECHAT_MAX_REPLIES_PER_HOUR": "8",
                "LLM_TEMPERATURE": "1.1",
                "LLM_TOP_P": "0.85",
                "LLM_FREQUENCY_PENALTY": "0.3",
                "LLM_PRESENCE_PENALTY": "-0.1",
            }
        }
    )

    assert result["config"]["values"]["WECHAT_REPLY_COOLDOWN_SECONDS"] == 12.5
    assert result["config"]["values"]["WECHAT_MAX_REPLIES_PER_HOUR"] == 8
    assert result["config"]["values"]["LLM_TEMPERATURE"] == 1.1
    assert controller.settings.wechat_reply_cooldown_seconds == 12.5
    assert controller.settings.wechat_max_replies_per_hour == 8
    assert controller.settings.llm_top_p == 0.85
    assert "WECHAT_REPLY_COOLDOWN_SECONDS=12.5" in config_path.read_text(encoding="utf-8")


def test_desktop_saves_multiple_groups(tmp_path):
    config_path = tmp_path / ".env"
    config_path.write_text(
        f"DATABASE_URL=sqlite:///{tmp_path / 'memory.db'}\nWECHAT_STOP_FILE={tmp_path / 'STOP'}\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_stop_file=tmp_path / "STOP",
    )
    controller = DesktopController(settings, agent_factory=FakeAgent, config_path=config_path)
    result = controller.save_config({"values": {"WECHAT_GROUP_NAMES": "群A, 群B"}})
    assert result["config"]["values"]["WECHAT_GROUP_NAMES"] == "群A,群B"
    assert controller.settings.wechat_group_names == ["群A", "群B"]


def test_dashboard_exposes_proactive_persona_and_training_controls():
    for marker in (
        'id="basicProactive"',
        'id="basicProactiveInterval"',
        'id="basicProactiveMax"',
        'id="basicProactiveProbability"',
        'id="basicPersonaFilter"',
        'id="basicPersonaMaxEmoji"',
        'id="basicTrainingExport"',
        'id="basicTrainingExportHour"',
        'id="basicTrainingExportMinute"',
        'id="proactiveSummary"',
        'id="trainingSummary"',
    ):
        assert marker in DASHBOARD_HTML
    assert "WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS" in DASHBOARD_HTML
    assert "TRAINING_EXPORT_ENABLED" in DASHBOARD_HTML


def test_desktop_status_reports_proactive_and_training_config(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_stop_file=tmp_path / "STOP",
        wechat_proactive_reply=True,
        wechat_proactive_min_interval_seconds=300,
        wechat_proactive_max_per_hour=6,
        wechat_proactive_probability=0.4,
        training_export_enabled=True,
        training_export_hour=23,
        training_export_minute=30,
    )
    controller = DesktopController(settings, agent_factory=FakeAgent)
    status = controller.status()
    assert status["proactive_enabled"] is True
    assert status["proactive_min_interval_seconds"] == 300
    assert status["proactive_max_per_hour"] == 6
    assert status["proactive_probability"] == 0.4
    assert status["training_export_enabled"] is True
    assert status["training_export_hour"] == 23
    assert status["training_export_minute"] == 30


def test_desktop_saves_proactive_persona_and_training_controls(tmp_path):
    config_path = tmp_path / ".env"
    config_path.write_text(
        f"DATABASE_URL=sqlite:///{tmp_path / 'memory.db'}\nWECHAT_STOP_FILE={tmp_path / 'STOP'}\n",
        encoding="utf-8",
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_stop_file=tmp_path / "STOP",
    )
    controller = DesktopController(settings, agent_factory=FakeAgent, config_path=config_path)
    result = controller.save_config(
        {
            "values": {
                "WECHAT_PROACTIVE_REPLY": True,
                "WECHAT_PROACTIVE_MIN_INTERVAL_SECONDS": 120,
                "WECHAT_PROACTIVE_MAX_PER_HOUR": 4,
                "WECHAT_PROACTIVE_PROBABILITY": 0.25,
                "PERSONA_STYLE_FILTER": False,
                "PERSONA_MAX_EMOJI": 0,
                "TRAINING_EXPORT_ENABLED": True,
                "TRAINING_EXPORT_HOUR": 22,
                "TRAINING_EXPORT_MINUTE": 45,
            }
        }
    )
    assert result["config"]["values"]["WECHAT_PROACTIVE_REPLY"] is True
    assert controller.settings.wechat_proactive_reply is True
    assert controller.settings.wechat_proactive_min_interval_seconds == 120
    assert controller.settings.wechat_proactive_max_per_hour == 4
    assert controller.settings.wechat_proactive_probability == 0.25
    assert controller.settings.persona_style_filter is False
    assert controller.settings.persona_max_emoji == 0
    assert controller.settings.training_export_enabled is True
    assert controller.settings.training_export_hour == 22
    assert controller.settings.training_export_minute == 45
    assert "WECHAT_PROACTIVE_REPLY=true" in config_path.read_text(encoding="utf-8")
