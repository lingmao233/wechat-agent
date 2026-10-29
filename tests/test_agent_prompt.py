from datetime import datetime, timedelta, timezone

from wechat_agent.agent import WeChatAgent
from wechat_agent.config import Settings
from wechat_agent.llm import LLMReply
from wechat_agent.models import ConversationMessage, MessageEvent, QuotedMessage


class CaptureLLM:
    def __init__(self):
        self.calls = []

    def reply(self, text, *args, **kwargs):
        self.calls.append((text, args, kwargs))
        return LLMReply(text="收到", model="fake", used_api=False)


def make_agent(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        embedding_enabled=False,
        openviking_enabled=False,
        group_style_enabled=False,
        wechat_stop_file=tmp_path / "STOP",
        wechat_group_name="测试群",
        # Keep the alias set explicit: the operator's .env is not loaded here.
        wechat_ai_aliases=["AI", "Away"],
    )
    llm = CaptureLLM()
    return WeChatAgent(settings, llm=llm), llm


def event(text, *, reply_to=None):
    return MessageEvent(
        account_id="account-1",
        group_id="g-a",
        group_name="测试群",
        member_id="wxid_member",
        member_name="成员",
        text=text,
        observed_at=datetime.now(timezone.utc),
        reply_to=reply_to,
    )


def test_current_message_and_quote_are_normalized_before_prompt(tmp_path):
    agent, llm = make_agent(tmp_path)

    agent._reply_with_context(
        event(
            "wxid_member: @Away 你好",
            reply_to=QuotedMessage(text="wxid_other: 原话", sender_name="别人"),
        ),
        [],
        [],
        {},
    )

    question, _, kwargs = llm.calls[0]
    assert question == "@AI 你好"
    assert kwargs["reply_to"].text == "原话"
    assert kwargs["reply_to"].sender_name == "别人"


def test_system_rows_are_not_style_candidates(tmp_path):
    agent, _ = make_agent(tmp_path)

    assert agent._is_style_candidate(event("16:18")) is False
    assert agent._is_style_candidate(event("查看更多消息")) is False
    assert agent._is_style_candidate(event("wxid_member: 哈哈，行吗？")) is True


def test_old_picture_only_returns_when_someone_mentions_it(tmp_path, monkeypatch):
    agent, llm = make_agent(tmp_path)
    picture = tmp_path / "bottle.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    now = datetime.now(timezone.utc)
    recent = [
        ConversationMessage(
            sender_name="成员",
            text="[图片]",
            observed_at=now - timedelta(minutes=10),
            image_paths=[str(picture)],
        )
    ] + [
        ConversationMessage(
            sender_name="成员",
            text=f"闲聊{index}",
            observed_at=now - timedelta(minutes=9) + timedelta(seconds=index),
        )
        for index in range(6)
    ]
    monkeypatch.setattr(agent, "_recent_image_window", lambda _event: recent)

    agent._reply_with_context(event("今天吃什么"), [], [], {})
    agent._reply_with_context(event("上面那张图是什么饮料"), [], [], {})

    first_kwargs = llm.calls[0][2]
    second_kwargs = llm.calls[1][2]
    assert first_kwargs["image_paths"] == []
    assert first_kwargs["context_images"] is False
    assert second_kwargs["image_paths"] == [str(picture)]
    assert second_kwargs["context_images"] is False


def test_image_window_covers_the_reference_lookback(tmp_path, monkeypatch):
    agent, _ = make_agent(tmp_path)
    captured = {}

    def fake_recent_messages(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(agent.storage, "recent_messages", fake_recent_messages)
    agent._recent_image_window(event("在吗"))

    assert captured["query"] is None
    assert captured["limit"] == 31


