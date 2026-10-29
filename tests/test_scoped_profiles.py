from datetime import datetime, timezone

from sqlalchemy import select

from wechat_agent.agent import WeChatAgent
from wechat_agent.config import Settings
from wechat_agent.models import MemoryFact, MessageEvent
from wechat_agent.openviking_memory import OpenVikingHit
from wechat_agent.storage import FactRow, Storage


def make_settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        embedding_enabled=False,
        openviking_enabled=False,
        group_style_enabled=False,
        wechat_stop_file=tmp_path / "STOP",
        wechat_group_name="测试群",
    )


def event(
    text,
    *,
    group="g-a",
    member="m-x",
    external_id=None,
    thread_id=None,
    task_id=None,
):
    return MessageEvent(
        account_id="account-1",
        group_id=group,
        group_name=group,
        member_id=member,
        member_name=member,
        text=text,
        observed_at=datetime.now(timezone.utc),
        external_message_id=external_id,
        thread_id=thread_id,
        task_id=task_id,
    )


def test_user_profile_update_is_scoped_and_superseded_history_is_retained(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))

    assert agent.handle_event(event("我喜欢极简、深色界面。", external_id="dark")) is False
    assert agent.handle_event(event("我现在更喜欢浅色界面。", external_id="light")) is False
    assert agent.handle_event(
        event("我喜欢浅色界面。", group="g-b", external_id="other-group")
    ) is False
    assert agent.handle_event(
        event("我喜欢浅色界面。", member="m-y", external_id="other-member")
    ) is False

    profile = agent.storage.user_profile("account-1", "g-a", "m-x")
    assert profile["preferences"] == ["喜欢浅色界面"]
    assert len(profile["history"]) == 2
    assert set(profile["source_message_ids"]) == {"1", "2"}

    current = agent.storage.search_scoped_memories(
        account_id="account-1",
        group_id="g-a",
        member_id="m-x",
        query="界面偏好",
        half_life_days=90,
    )
    assert [fact.content for fact in current] == ["喜欢浅色界面"]
    assert agent.storage.search_scoped_memories(
        account_id="account-1",
        group_id="g-b",
        member_id="m-x",
        query="界面偏好",
        half_life_days=90,
    )[0].content == "喜欢浅色界面"
    assert agent.storage.search_scoped_memories(
        account_id="account-1",
        group_id="g-a",
        member_id="m-y",
        query="界面偏好",
        half_life_days=90,
    )[0].content == "喜欢浅色界面"

    with agent.storage.Session() as session:
        rows = list(session.scalars(select(FactRow).order_by(FactRow.id.asc())))
    assert rows[0].content == "喜欢极简、深色界面"
    assert rows[0].status == "superseded"
    assert rows[0].valid_until is not None


def test_ordinary_chat_builds_soft_member_behavior_without_cross_group_leak(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))
    for index, text in enumerate(
        ["API 部署怎么配置？", "API 部署完成！", "请再验证 API 接口", "哈哈，API 接口不错"],
        start=1,
    ):
        assert agent.handle_event(event(text, external_id=f"behavior-{index}")) is False

    profile = agent.storage.user_profile("account-1", "g-a", "m-x")
    behavior = profile["behavior"]
    assert behavior["message_count"] == 4
    assert behavior["evidence_message_ids"] == ["1", "2", "3", "4"]
    assert "部署与运维" in behavior["common_topics"]
    assert "软件开发" in behavior["common_topics"]
    assert "short_turns" in behavior["expression_habits"]
    assert profile["expression_habits"] == behavior["expression_habits"]
    assert agent.storage.user_profile("account-1", "g-b", "m-x")["behavior"]["message_count"] == 0


def test_unknown_sender_updates_group_ai_profile_but_not_member_profile(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))
    assert agent.handle_event(event("哈哈，API 部署完成", member="unknown", external_id="unknown-1")) is False
    assert agent.storage.ai_profile("account-1", "g-a")["message_count"] == 1
    assert agent.storage.user_profile("account-1", "g-a", "unknown")["behavior"]["message_count"] == 0


def test_task_profile_keeps_updates_and_related_message_history(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))
    first = event(
        "任务 task-1 目标是完成接口，需求是支持流式，进度是已完成一半，阻塞是等待密钥，下一步是补测试。",
        external_id="task-1-a",
        task_id="task-1",
    )
    second = event(
        "任务 task-1 决定是使用 Ark，进度是已完成接口，下一步是运行回归。",
        external_id="task-1-b",
        task_id="task-1",
    )

    agent.handle_event(first)
    agent.handle_event(second)

    profile = agent.storage.task_profile("account-1", "g-a", "task-1", member_id="m-x")
    assert profile["goal"] == "完成接口"
    assert profile["requirements"] == ["支持流式"]
    assert profile["decisions"] == ["使用 Ark"]
    assert profile["blockers"] == ["等待密钥"]
    assert profile["next_action"] == "运行回归"
    assert profile["progress"] == ["已完成一半", "已完成接口"]
    assert profile["related_message_ids"] == ["1", "2"]
    assert len(profile["history"]) == 2


def test_text_inferred_task_stays_group_scoped(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))
    inferred = event(
        "任务 task-shared 目标是完成群接口，下一步是联调。",
        member="m-x",
        external_id="task-shared-x",
    )
    agent.handle_event(inferred)

    group_profile = agent.storage.task_profile("account-1", "g-a", "task-shared")
    member_profile = agent.storage.task_profile(
        "account-1", "g-a", "task-shared", member_id="m-x"
    )
    assert group_profile["goal"] == "完成群接口"
    assert "member_id" not in group_profile["scope"]
    assert member_profile["goal"] == ""


def test_ai_profile_is_group_scoped_and_keeps_evidence_history(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))

    agent.handle_event(event("API 部署完成了，哈哈！", external_id="ai-a-1"))
    agent.handle_event(event("请验证数据库接口，谢谢。", external_id="ai-a-2"))
    agent.handle_event(
        event("您好，结论请写得详细一些。", group="g-b", external_id="ai-b-1")
    )

    group_a = agent.storage.ai_profile("account-1", "g-a")
    group_b = agent.storage.ai_profile("account-1", "g-b")
    assert group_a["message_count"] == 2
    assert group_a["common_topics"]
    assert "部署与运维" in group_a["common_topics"]
    assert len(group_a["history"]) == 2
    assert group_b["message_count"] == 1
    assert group_b["formality"] == "formal"
    assert group_b["history"]
    assert set(group_a["evidence_message_ids"]).isdisjoint(group_b["evidence_message_ids"])


def test_group_style_profile_refreshes_per_group_only(tmp_path):
    settings = make_settings(tmp_path)
    settings.group_style_enabled = True
    settings.group_style_min_messages = 2
    settings.group_style_refresh_messages = 1
    settings.group_style_sample_messages = 10
    agent = WeChatAgent(settings)

    agent.handle_event(event("哈哈，行吗？", external_id="style-a-1"))
    assert agent.storage.group_style_profile("account-1", "g-a") == ""
    agent.handle_event(event("确实可以！", external_id="style-a-2"))
    profile_a = agent.storage.group_style_profile("account-1", "g-a")
    assert "普通群消息" in profile_a
    assert "口语化" in profile_a
    assert agent.storage.group_style_profile("account-1", "g-b") == ""


def test_remote_legacy_raw_message_is_sanitized_on_read(tmp_path):
    storage = Storage(make_settings(tmp_path))
    storage.init_schema()

    class FakeOpenViking:
        def search(self, *args, **kwargs):
            return [
                OpenVikingHit(
                    memory_id="raw-1",
                    value="wxid_member: 旧消息",
                    scope={
                        "account_id": "account-1",
                        "group_id": "g-a",
                        "member_id": "m-x",
                    },
                    memory_type="raw_message",
                )
            ]

    storage.openviking = FakeOpenViking()
    results = storage.search_scoped_memories(
        account_id="account-1",
        group_id="g-a",
        member_id="m-x",
        query="旧消息",
        half_life_days=90,
    )

    assert [fact.content for fact in results] == ["旧消息"]


def test_archived_legacy_chat_facts_are_not_recalled(tmp_path):
    storage = Storage(make_settings(tmp_path))
    storage.init_schema()
    storage.add_scoped_memory(
        MemoryFact(
            account_id="account-1",
            group_id="g-a",
            member_id="m-x",
            memory_type="chat",
            content="旧格式 wxid_member: <msg>文本</msg>",
            status="active",
        )
    )

    assert storage.archive_legacy_chat_facts() == 1
    assert (
        storage.search_scoped_memories(
            account_id="account-1",
            group_id="g-a",
            member_id="m-x",
            query="旧格式",
            half_life_days=90,
        )
        == []
    )


def test_ai_profile_keeps_synthesized_payload_after_new_messages(tmp_path):
    agent = WeChatAgent(make_settings(tmp_path))
    agent.handle_event(event("API 部署完成", external_id="synth-1"))
    agent.storage.save_synthesized_group_profile(
        "account-1",
        "g-a",
        {"culture": "测试文化", "reply_shape": "short"},
    )
    agent.handle_event(event("数据库接口验证完成", external_id="synth-2"))

    profile = agent.storage.ai_profile("account-1", "g-a")
    assert profile["synthesized"]["culture"] == "测试文化"
    assert profile["synthesized"]["reply_shape"] == "short"


def test_reply_style_fit_is_tracked_against_group_band(tmp_path):
    settings = make_settings(tmp_path)
    settings.group_style_enabled = True
    settings.group_style_min_messages = 2
    settings.group_style_refresh_messages = 1
    settings.group_style_sample_messages = 10
    agent = WeChatAgent(settings)

    agent.handle_event(event("哈哈，行吗？", external_id="fit-1"))
    agent.handle_event(event("确实可以！", external_id="fit-2"))
    assert agent.storage.group_style_profile("account-1", "g-a")

    first = agent.storage.record_reply_style("account-1", "g-a", "哈哈，可以")
    assert first["samples"] == 1
    assert first["target_low"] <= first["last_length"] <= first["target_high"]

    agent.storage.record_reply_style("account-1", "g-a", "好")
    agent.storage.record_reply_style("account-1", "g-a", "行")
    profile = agent.storage.group_style_profile("account-1", "g-a")
    assert "近期 AI 回复" in profile
    assert "落在建议区间" in profile


def test_recent_context_filters_unrelated_same_group_messages(tmp_path):
    storage = Storage(make_settings(tmp_path))
    storage.init_schema()
    storage.record_message(event("今晚看电影和综艺。", external_id="entertainment"))
    storage.record_message(event("API 部署已经完成，日志正常。", external_id="technical"))

    context = storage.recent_messages(
        account_id="account-1",
        group_id="g-a",
        limit=10,
        max_chars=2000,
        query="如何配置 API 部署？",
    )

    assert [message.text for message in context] == ["API 部署已经完成，日志正常。"]


def test_related_context_ignores_single_character_overlap_and_honors_scope(tmp_path):
    storage = Storage(make_settings(tmp_path))
    storage.init_schema()
    storage.record_message(event("我今晚看电影和综艺。", external_id="entertainment"))
    storage.record_message(event("接口部署已经完成，日志正常。", external_id="technical"))
    storage.record_message(
        event(
            "完全不同的线程背景。",
            external_id="threaded",
            thread_id="thread-1",
        )
    )

    related = storage.related_messages(
        account_id="account-1",
        group_id="g-a",
        query="帮我设计一个接口。",
        limit=10,
        max_chars=2000,
    )
    assert [message.text for message in related] == ["接口部署已经完成，日志正常。"]
    assert storage.related_messages(
        account_id="account-1",
        group_id="g-a",
        query="完全不同的提问。",
        limit=0,
        max_chars=2000,
    ) == []

    thread_related = storage.related_messages(
        account_id="account-1",
        group_id="g-a",
        query="没有共同词。",
        thread_id="thread-1",
        limit=10,
        max_chars=2000,
    )
    assert [message.text for message in thread_related] == ["完全不同的线程背景。"]

