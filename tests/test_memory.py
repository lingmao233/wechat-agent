from datetime import datetime, timedelta, timezone

from wechat_agent.config import Settings
from wechat_agent.models import MemoryFact, MessageEvent
from wechat_agent.storage import Storage


def test_memory_isolated_by_group_and_member(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'memory.db'}")
    storage = Storage(settings)
    storage.init_schema()
    now = datetime.now(timezone.utc)
    storage.add_fact(
        MemoryFact(
            account_id="a",
            group_id="g1",
            member_id="m1",
            memory_type="interest",
            content="喜欢摄影",
            confidence=0.9,
            status="confirmed",
            observed_at=now,
        )
    )
    assert storage.search_facts("a", "g1", "m1", "摄影", 90)
    assert storage.search_facts("a", "g1", "m2", "摄影", 90) == []
    assert storage.search_facts("a", "g2", "m1", "摄影", 90) == []


def test_newer_fact_scores_above_old_fact(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'memory.db'}")
    storage = Storage(settings)
    storage.init_schema()
    now = datetime.now(timezone.utc)
    for days, text in [(180, "喜欢足球"), (1, "喜欢足球和摄影")]:
        storage.add_fact(
            MemoryFact(
                account_id="a",
                group_id="g",
                member_id="m",
                memory_type="interest",
                content=text,
                confidence=0.8,
                status="inferred",
                observed_at=now - timedelta(days=days),
            )
        )
    results = storage.search_facts("a", "g", "m", "足球", 90, limit=2)
    assert results[0].content == "喜欢足球和摄影"


def test_message_hash_is_deduplicated(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'memory.db'}")
    storage = Storage(settings)
    storage.init_schema()
    event = MessageEvent("a", "g", "群", "m", "成员", "你好")
    assert storage.record_message(event) == storage.record_message(event)
