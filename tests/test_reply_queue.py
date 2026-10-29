from wechat_agent.agent import WeChatAgent
from wechat_agent.config import Settings
from wechat_agent.llm import LLMReply
from wechat_agent.models import MessageEvent
from wechat_agent.reply_queue import ReplyQueue


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


class Named:
    def __init__(self, name):
        self.member_name = name


def make_agent(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
        wechat_allow_send=True,
        wechat_proactive_reply=False,
    )
    return WeChatAgent(settings, ui=FakeUi(), llm=FakeLlm())


def event(text, external_id, member="成员"):
    return MessageEvent(
        account_id="a",
        group_id="g",
        group_name="测试群",
        member_id="m",
        member_name=member,
        text=text,
        external_message_id=external_id,
    )


def test_queue_is_fifo():
    queue = ReplyQueue()
    for name in ("a", "b", "c"):
        queue.push(Named(name))

    assert [queue.pop().member_name for _ in range(3)] == ["a", "b", "c"]
    assert queue.pop() is None


def test_overflow_drops_oldest_and_keeps_newest():
    queue = ReplyQueue(maxsize=2)
    queue.push(Named("first"))
    queue.push(Named("second"))

    dropped = queue.push(Named("third"))

    assert dropped is not None and dropped.member_name == "first"
    assert len(queue) == 2
    assert queue.stats.dropped == 1
    assert [queue.pop().member_name for _ in range(2)] == ["second", "third"]


def test_zero_maxsize_is_unbounded():
    queue = ReplyQueue(maxsize=0)
    for index in range(5):
        assert queue.push(Named(str(index))) is None

    assert len(queue) == 5
    assert queue.stats.peak_depth == 5


def test_clear_reports_pending():
    queue = ReplyQueue()
    queue.push(Named("a"))
    queue.push(Named("b"))

    assert queue.clear() == 2
    assert len(queue) == 0


def test_agent_answers_one_queued_message_per_cycle(tmp_path):
    agent = make_agent(tmp_path)
    handled = []
    agent.handle_event = lambda item: handled.append(item.external_message_id) or True
    queue = ReplyQueue()

    agent._enqueue_reply(queue, event("@AI 第一条", "one"))
    agent._enqueue_reply(queue, event("@AI 第二条", "two"))
    agent._enqueue_reply(queue, event("@AI 第三条", "three"))

    assert len(queue) == 3
    agent._reply_next(queue)
    assert handled == ["one"]
    assert len(queue) == 2

    agent._reply_next(queue)
    agent._reply_next(queue)
    agent._reply_next(queue)
    assert handled == ["one", "two", "three"]


def test_agent_logs_dropped_message_when_queue_full(tmp_path):
    agent = make_agent(tmp_path)
    agent.handle_event = lambda item: True
    queue = ReplyQueue(maxsize=1)
    agent._enqueue_reply(queue, event("@AI 旧消息", "old"))
    agent._enqueue_reply(queue, event("@AI 新消息", "new"))

    assert queue.stats.dropped == 1
    assert queue.pop().external_message_id == "new"
