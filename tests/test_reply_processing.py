from datetime import datetime, timedelta, timezone

from wechat_agent.agent import WeChatAgent
from wechat_agent.config import Settings
from wechat_agent.llm import LLMReply
from wechat_agent.models import MessageEvent
from wechat_agent.storage import Storage
from wechat_agent.weixin_ui import UiMessage, WeixinUiAdapter


class FakeUi:
    def __init__(self):
        self.sent = []

    def send(self, action):
        self.sent.append(action)


class FakeLlm:
    def __init__(self):
        self.calls = []

    def reply(self, incoming, member_name, group_name, memories):
        self.calls.append((incoming, member_name, group_name, memories))
        return LLMReply(text=f"已回答：{incoming}", model="test", used_api=False)


def make_event(text, *, external_id, observed_at=None, is_self=False):
    return MessageEvent(
        account_id="a",
        group_id="g",
        group_name="测试群",
        member_id="m",
        member_name="成员",
        text=text,
        observed_at=observed_at or datetime.now(timezone.utc),
        external_message_id=external_id,
        is_self=is_self,
    )


def make_agent(tmp_path, *, allow_send=True):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
        wechat_allow_send=allow_send,
    )
    ui = FakeUi()
    llm = FakeLlm()
    agent = WeChatAgent(settings, ui=ui, llm=llm)
    return agent, ui, llm


def test_plain_question_does_not_invoke_ai(tmp_path):
    agent, ui, llm = make_agent(tmp_path)

    assert agent.handle_event(make_event("为什么下雨？", external_id="plain-1")) is False
    assert llm.calls == []
    assert ui.sent == []
    assert agent.storage.recent_reply_records(1)[0]["reason"] == "ai_not_mentioned"


def test_ai_mention_is_sent_once_and_duplicate_question_is_persistent(tmp_path):
    agent, ui, llm = make_agent(tmp_path)
    first_time = datetime(2026, 10, 26, tzinfo=timezone.utc)

    first = make_event("@AI 你好吗？", external_id="row-1", observed_at=first_time)
    repeat_same_row = make_event(
        "@AI 你好吗？",
        external_id="row-1",
        observed_at=first_time + timedelta(seconds=30),
    )
    repeat_new_row = make_event(
        "AI，你好吗",
        external_id="row-2",
        observed_at=first_time + timedelta(minutes=1),
    )

    assert agent.handle_event(first) is True
    assert agent.handle_event(repeat_same_row) is False
    assert agent.handle_event(repeat_new_row) is False
    assert len(llm.calls) == 1
    assert len(ui.sent) == 1
    assert agent.stats.duplicate_questions == 1
    assert agent.storage.processing_summary()["sent"] == 1

    # A fresh process using the same database still sees the answered question.
    restarted, restarted_ui, restarted_llm = make_agent(tmp_path)
    assert restarted.handle_event(
        make_event("@AI 你好吗?", external_id="row-3", observed_at=first_time + timedelta(minutes=2))
    ) is False
    assert restarted_llm.calls == []
    assert restarted_ui.sent == []


def test_self_message_never_triggers_reply(tmp_path):
    agent, ui, llm = make_agent(tmp_path)

    assert agent.handle_event(make_event("@AI 自己的上一条回答", external_id="self-1", is_self=True)) is False
    assert llm.calls == []
    assert ui.sent == []


def test_recent_outbound_echo_with_unknown_sender_never_triggers_reply(tmp_path):
    agent, ui, llm = make_agent(tmp_path)
    first = make_event("@AI 回声测试", external_id="echo-in")
    assert agent.handle_event(first) is True
    assert len(ui.sent) == 1

    echo = MessageEvent(
        account_id="a",
        group_id="g",
        group_name="测试群",
        member_id="unknown",
        member_name="unknown",
        text=ui.sent[0].text,
        external_message_id="echo-out",
    )
    assert agent.handle_event(echo) is False
    assert len(llm.calls) == 1
    assert len(ui.sent) == 1


def test_send_block_is_not_recorded_as_answered(tmp_path):
    blocked, blocked_ui, blocked_llm = make_agent(tmp_path, allow_send=False)
    event = make_event("@AI 稍后可以回答吗", external_id="blocked-1")

    assert blocked.handle_event(event) is False
    assert blocked_llm.calls
    assert blocked_ui.sent == []
    assert blocked.storage.processing_summary()["skipped"] == 1

    allowed, allowed_ui, allowed_llm = make_agent(tmp_path, allow_send=True)
    # The same visible row is retryable after the send gate is opened; it was
    # skipped, not recorded as a successful answer.
    retry = make_event("@AI 稍后可以回答吗", external_id="blocked-1")
    assert allowed.handle_event(retry) is True
    assert len(allowed_llm.calls) == 1
    assert len(allowed_ui.sent) == 1


def test_ui_startup_baseline_does_not_reappear_on_next_poll():
    adapter = object.__new__(WeixinUiAdapter)
    adapter.settings = type(
        "SettingsStub",
        (),
        {
            "wechat_group_name": "测试群",
            "wechat_skip_existing_on_start": True,
            "wechat_self_name": "",
        },
    )()
    adapter._group_open = True
    adapter._primed = False
    adapter._seen_keys = set()
    messages = [UiMessage(sender="成员", text="历史消息", fingerprint="history")]
    adapter.read_messages = lambda: list(messages)

    assert adapter.poll("a") == []
    assert adapter.poll("a") == []

    messages.append(UiMessage(sender="成员", text="@AI 新问题", fingerprint="new"))
    events = adapter.poll("a")
    assert [event.text for event in events] == ["@AI 新问题"]
