from datetime import datetime, timedelta, timezone

from wechat_agent.image_context import references_image, select_image_paths
from wechat_agent.models import ConversationMessage


START = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def message(index, text, *, images=None):
    return ConversationMessage(
        sender_name="成员",
        text=text,
        observed_at=START + timedelta(seconds=index),
        image_paths=list(images or []),
    )


def test_reference_detection_ignores_ordinary_words():
    assert references_image("图书馆在哪里") is False
    assert references_image("我想吃白饭") is False
    assert references_image("上面那张图是什么牌子") is True
    assert references_image("照片里那个瓶子好喝吗") is True
    assert references_image("照片拍得不错") is True
    assert references_image("看图") is True
    assert references_image("[图片]") is True


def test_current_message_image_is_always_kept():
    paths, _ = select_image_paths(["now.png"], "随便聊聊", recent_messages=[])

    assert paths == ["now.png"]


def test_recent_image_is_attached():
    recent = [message(0, "看看", images=["bottle.png"]), message(1, "好看")]

    paths, _ = select_image_paths([], "今天吃什么", recent_messages=recent, turns=4)

    assert paths == ["bottle.png"]


def test_old_image_is_dropped_for_unrelated_topic():
    recent = [message(0, "看看", images=["bottle.png"])] + [
        message(index, f"闲聊{index}") for index in range(1, 6)
    ]

    paths, state = select_image_paths([], "今天吃什么", recent_messages=recent, turns=4)

    assert paths == []
    assert state == {}


def test_old_image_returns_when_member_mentions_it():
    recent = [message(0, "看看", images=["bottle.png"])] + [
        message(index, f"闲聊{index}") for index in range(1, 8)
    ]

    paths, state = select_image_paths(
        [], "上面那张图是什么饮料", recent_messages=recent, turns=4
    )

    assert paths == ["bottle.png"]
    assert state == {"bottle.png": 4}


def test_refreshed_image_stays_for_following_turns_then_expires():
    recent = [message(0, "看看", images=["bottle.png"])] + [
        message(index, f"闲聊{index}") for index in range(1, 10)
    ]
    paths, state = select_image_paths(
        [], "这张照片里是什么", recent_messages=recent, turns=4
    )
    assert state == {"bottle.png": 4}

    outcomes = []
    for _ in range(4):
        paths, state = select_image_paths(
            [], "好喝吗", recent_messages=recent, turns=4, refreshed=state
        )
        outcomes.append(bool(paths))

    assert outcomes == [True, True, True, False]
    assert state == {}


def test_quoted_image_placeholder_refills_the_picture():
    recent = [message(0, "看看", images=["bottle.png"])] + [
        message(index, f"闲聊{index}") for index in range(1, 8)
    ]

    paths, _ = select_image_paths(
        [], "这个呢", quoted_text="[图片]", recent_messages=recent, turns=4
    )

    assert paths == ["bottle.png"]


def test_reference_beyond_the_reference_window_is_ignored():
    recent = [message(0, "看看", images=["bottle.png"])] + [
        message(index, f"闲聊{index}") for index in range(1, 12)
    ]

    paths, state = select_image_paths(
        [],
        "上面那张图是什么",
        recent_messages=recent,
        turns=2,
        reference_window=5,
    )

    assert paths == []
    assert state == {}


def test_cap_keeps_the_newest_picture_first():
    recent = [
        message(0, "旧图", images=["old.png"]),
        message(1, "新图", images=["new.png"]),
        message(2, "聊天"),
    ]

    paths, _ = select_image_paths(
        [], "聊点别的", recent_messages=recent, turns=4, max_count=1
    )

    assert paths == ["new.png"]
    assert "old.png" not in paths


def test_related_old_picture_does_not_leak_back_without_reference():
    """The semantic context search must not resurrect pictures on its own."""

    recent = [message(index, f"消息{index}") for index in range(6)]
    related = [message(0, "[图片]", images=["old.png"])]

    paths, _ = select_image_paths(
        [], "在吗", recent_messages=[*recent, *related], turns=2
    )

    assert paths == []
