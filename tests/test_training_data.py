import json
from datetime import datetime, timedelta, timezone

from wechat_agent.config import Settings
from wechat_agent.main import build_parser
from wechat_agent.models import MessageEvent
from wechat_agent.storage import Storage
from wechat_agent.training_data import classify_behavior, export_training_data


def make_storage(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'memory.db'}",
        embedding_enabled=False,
        openviking_enabled=False,
        group_style_enabled=False,
        wechat_group_name="测试群",
        wechat_ai_aliases=["AI"],
    )
    storage = Storage(settings)
    storage.init_schema()
    return storage


def record(storage, member_id, member_name, text, external_id, observed_at=None):
    storage.record_message(
        MessageEvent(
            account_id="account-1",
            group_id="g-a",
            group_name="测试群",
            member_id=member_id,
            member_name=member_name,
            text=text,
            observed_at=observed_at or datetime.now(timezone.utc),
            external_message_id=external_id,
        )
    )


def test_behavior_labels_cover_comfort_joke_prank_tease_and_question():
    assert classify_behavior("抱抱，别想太多") == "comfort"
    assert classify_behavior("笑死，这也太离谱了") == "joke"
    assert classify_behavior("骗你的啦，上钩了") == "prank"
    assert classify_behavior("你是不是傻傻的") == "tease"
    assert classify_behavior("今晚吃什么？") == "question"
    assert classify_behavior("今天下午开会") == "ordinary"
    assert classify_behavior("6") == "ordinary"


def test_export_writes_anonymized_style_and_behavior_pairs(tmp_path):
    storage = make_storage(tmp_path)
    record(storage, "m-alice", "Alice", "今天好累啊", "e1")
    record(storage, "m-bob", "Bob", "抱抱，别难过", "e2")
    record(storage, "m-alice", "Alice", "笑死，这也太离谱了", "e3")
    record(storage, "m-bob", "Bob", "骗你的啦", "e4")

    report = export_training_data(storage, tmp_path / "export", window_messages=2)

    assert report["messages"] == 4
    assert report["anonymized"] is True
    assert report["behavior_counts"] == {"comfort": 1, "joke": 1, "ordinary": 1, "prank": 1}

    styles = [
        json.loads(line)
        for line in (tmp_path / "export" / "style_corpus.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    pairs = [
        json.loads(line)
        for line in (tmp_path / "export" / "behavior_pairs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(styles) == 4
    assert len(pairs) == 4
    assert "Alice" not in json.dumps(styles, ensure_ascii=False)
    assert "Alice" not in json.dumps(pairs, ensure_ascii=False)
    assert pairs[0]["speaker"].startswith("群友")
    assert pairs[1]["context"] == ["群友A：今天好累啊"]
    assert pairs[1]["response"] == "抱抱，别难过"
    assert pairs[1]["behavior"] == "comfort"
    assert pairs[3]["context"][-1] == "群友A：笑死，这也太离谱了"


def test_export_can_keep_names_when_requested(tmp_path):
    storage = make_storage(tmp_path)
    record(storage, "m-alice", "Alice", "笑死", "e1")

    report = export_training_data(storage, tmp_path / "export", anonymize=False)

    assert report["anonymized"] is False
    pair = json.loads(
        (tmp_path / "export" / "behavior_pairs.jsonl").read_text(encoding="utf-8").strip()
    )
    assert pair["speaker"] == "Alice"


def test_export_skips_likely_bot_text(tmp_path):
    storage = make_storage(tmp_path)
    record(storage, "unknown", "unknown", "我是群里的 AI 助手，有什么想问的", "e1")
    record(storage, "m-alice", "Alice", "正常聊天", "e2")

    report = export_training_data(storage, tmp_path / "export")

    assert report["messages"] == 1
    style = (tmp_path / "export" / "style_corpus.jsonl").read_text(encoding="utf-8")
    assert "AI 助手" not in style
    assert "正常聊天" in style


def test_export_filters_to_requested_window(tmp_path):
    storage = make_storage(tmp_path)
    start = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)
    record(storage, "m-a", "Alice", "第一天", "e1", observed_at=start + timedelta(hours=1))
    record(storage, "m-b", "Bob", "第二天", "e2", observed_at=start + timedelta(hours=25))

    report = export_training_data(
        storage,
        tmp_path / "export",
        since=start,
        until=start + timedelta(days=1),
    )

    assert report["messages"] == 1
    assert report["since"] == start.isoformat()
    assert report["until"] == (start + timedelta(days=1)).isoformat()
    style = (tmp_path / "export" / "style_corpus.jsonl").read_text(encoding="utf-8")
    assert "第一天" in style
    assert "第二天" not in style


def test_cli_exposes_export_command_options():
    args = build_parser().parse_args(
        [
            "export-training-data",
            "--out",
            "data/custom",
            "--context",
            "8",
            "--no-anonymize",
            "--today",
        ]
    )

    assert args.command == "export-training-data"
    assert args.out == "data/custom"
    assert args.context == 8
    assert args.no_anonymize is True
    assert args.today is True
