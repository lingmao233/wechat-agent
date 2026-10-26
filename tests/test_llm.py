from wechat_agent.config import Settings
from wechat_agent.llm import LLMClient


def test_missing_api_key_uses_non_network_stub():
    settings = Settings(llm_api_key="")
    reply = LLMClient(settings).reply("@AI 你好", "成员", "测试群", [])
    assert reply.used_api is False
    assert "AI测试" in reply.text
