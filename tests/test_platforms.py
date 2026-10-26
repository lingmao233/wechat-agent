from wechat_agent.config import Settings
from wechat_agent.platforms.dry_run import DryRunPlatformAdapter
from wechat_agent.platforms.models import BrowseItem, PlatformComment
from wechat_agent.platforms.service import PlatformService
from wechat_agent.storage import Storage


def make_service(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'memory.db'}")
    storage = Storage(settings)
    storage.init_schema()
    item = BrowseItem(
        platform="douyin",
        item_id="video-1",
        url="https://www.douyin.com/video/video-1",
        title="测试视频",
        author_id="author-1",
        author_name="作者",
        text="公开内容",
    )
    comment = PlatformComment(
        platform="douyin",
        comment_id="comment-1",
        item_id="video-1",
        author_id="member-1",
        author_name="群友",
        text="这个怎么样？",
    )
    adapter = DryRunPlatformAdapter("douyin", items=[item], comments=[comment], account_id="platform-test")
    return storage, PlatformService(storage, {"douyin": adapter}), item, comment


def test_platform_browse_read_and_dry_run_action_is_idempotent(tmp_path):
    storage, service, item, comment = make_service(tmp_path)

    assert service.login_status()["douyin"]["mode"] == "dry_run"
    assert service.browse("douyin", query="测试") == [item]
    assert service.read_comments("douyin", item.item_id) == [comment]

    first = service.plan_comment("douyin", item.item_id, "这是一个测试评论")
    second = service.plan_comment("douyin", item.item_id, "这是一个测试评论")
    assert first.status == "dry_run"
    assert first.dry_run is True
    assert first.duplicate is False
    assert second.action_key == first.action_key
    assert second.duplicate is True
    assert len(service.actions()) == 1


def test_platform_reply_uses_current_comment_id_and_separate_ledger(tmp_path):
    storage, service, _, comment = make_service(tmp_path)
    receipt = service.plan_reply("douyin", comment.comment_id, "回复内容")
    assert receipt.status == "dry_run"
    action = storage.recent_platform_actions(1)[0]
    assert action["target_id"] == comment.comment_id
    assert action["action_type"] == "reply"
    assert action["dry_run"] is True


def test_unknown_target_is_rejected_before_ledger_write(tmp_path):
    _, service, _, _ = make_service(tmp_path)
    try:
        service.plan_comment("douyin", "missing", "评论")
    except ValueError as error:
        assert str(error) == "item_not_found:missing"
    else:
        raise AssertionError("missing platform target must be rejected")
