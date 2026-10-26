from wechat_agent.config import Settings
from wechat_agent.models import MessageEvent, OutboundAction
from wechat_agent.policy import ActionPolicy
from wechat_agent.storage import Storage


def test_policy_requires_group_and_send_opt_in(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_allow_send=False,
    )
    storage = Storage(settings)
    storage.init_schema()
    policy = ActionPolicy(settings, storage)
    action = OutboundAction("a", "测试群", "测试群", "你好")
    allowed, reason = policy.authorize(action)
    assert not allowed
    assert reason == "sending_disabled"


def test_ai_mention_always_enters_reply_path(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(settings)
    storage.init_schema()
    event = MessageEvent("a", "测试群", "测试群", "m", "成员", "@AI 你好吗")
    should_reply, reason = ActionPolicy(settings, storage).should_reply_to(event)
    assert should_reply
    assert reason == "ai_mentioned"


def test_ai_alias_does_not_match_inside_an_ordinary_word(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(settings)
    storage.init_schema()
    event = MessageEvent("a", "测试群", "测试群", "m", "成员", "I said this already")
    should_reply, reason = ActionPolicy(settings, storage).should_reply_to(event)
    assert not should_reply
    assert reason == "ai_not_mentioned"
