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


def test_prefixed_alias_is_recognized_and_system_rows_are_ignored(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(settings)
    storage.init_schema()
    policy = ActionPolicy(settings, storage)

    prefixed = MessageEvent("a", "测试群", "测试群", "wxid_member", "成员", "wxid_member: AI 你好")
    assert policy.should_reply_to(prefixed) == (True, "ai_mentioned")

    system = MessageEvent("a", "测试群", "测试群", "wxid_member", "成员", "16:18")
    assert policy.should_reply_to(system) == (False, "system_message")


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


def test_proactive_reply_is_opt_in_and_respects_social_cues(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
        wechat_proactive_reply=True,
        wechat_proactive_probability=1.0,
        wechat_proactive_min_interval_seconds=0,
        wechat_proactive_max_per_hour=0,
    )
    storage = Storage(settings)
    storage.init_schema()
    policy = ActionPolicy(settings, storage)

    comfort = MessageEvent("a", "测试群", "测试群", "m", "成员", "今天好累啊")
    assert policy.should_reply_to(comfort) == (True, "proactive_comfort")

    addressed_elsewhere = MessageEvent("a", "测试群", "测试群", "m", "成员", "@Alice 今天好累")
    assert policy.should_reply_to(addressed_elsewhere) == (False, "ai_not_mentioned")

    explicit = MessageEvent("a", "测试群", "测试群", "m", "成员", "@AI 今天好累")
    assert policy.should_reply_to(explicit) == (True, "ai_mentioned")


def test_proactive_reply_is_off_by_default_and_respects_cooldown(tmp_path):
    base = dict(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(Settings(**base))
    storage.init_schema()
    event = MessageEvent("a", "测试群", "测试群", "m", "成员", "笑死，太离谱了")

    assert ActionPolicy(Settings(**base, wechat_proactive_reply=False), storage).should_reply_to(event) == (
        False,
        "ai_not_mentioned",
    )

    settings = Settings(
        **base,
        wechat_proactive_reply=True,
        wechat_proactive_probability=1.0,
        wechat_proactive_min_interval_seconds=300,
        wechat_proactive_max_per_hour=0,
    )
    policy = ActionPolicy(settings, storage)
    policy.mark_sent()
    assert policy.should_reply_to(event) == (False, "proactive_cooldown")


def test_proactive_probability_zero_never_joins(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
        wechat_proactive_reply=True,
        wechat_proactive_probability=0.0,
        wechat_proactive_min_interval_seconds=0,
        wechat_proactive_max_per_hour=0,
    )
    storage = Storage(settings)
    storage.init_schema()
    event = MessageEvent("a", "测试群", "测试群", "m", "成员", "笑死，太离谱了")

    assert ActionPolicy(settings, storage).should_reply_to(event) == (
        False,
        "proactive_probability",
    )


def test_reply_frequency_limits_only_triggered_messages(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
        wechat_reply_cooldown_seconds=30,
        wechat_max_replies_per_hour=1,
    )
    storage = Storage(settings)
    storage.init_schema()
    policy = ActionPolicy(settings, storage)
    event = MessageEvent("a", "测试群", "测试群", "m", "成员", "@AI 你好吗")

    should_reply, reason = policy.should_reply_to(event)
    assert should_reply
    assert reason == "ai_mentioned"

    policy.mark_sent()
    should_reply, reason = policy.should_reply_to(event)
    assert not should_reply
    assert reason == "reply_cooldown_active"

    # A non-addressed message remains blocked by the trigger policy, even
    # while the frequency guard is active.
    ordinary = MessageEvent("a", "测试群", "测试群", "m2", "成员", "普通聊天")
    should_reply, reason = policy.should_reply_to(ordinary)
    assert not should_reply
    assert reason == "ai_not_mentioned"


def test_multiple_configured_groups_are_allowlisted(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_names="测试群,另一个群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(settings)
    storage.init_schema()
    policy = ActionPolicy(settings, storage)

    allowed = MessageEvent("a", "另一个群", "另一个群", "m", "成员", "@AI 你好")
    blocked = MessageEvent("a", "陌生群", "陌生群", "m", "成员", "@AI 你好")
    assert policy.should_reply_to(allowed)[0] is True
    assert policy.should_reply_to(blocked) == (False, "group_not_allowlisted")
