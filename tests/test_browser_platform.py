import pytest

from wechat_agent.platforms.browser import (
    BrowserPlatformAdapter,
    PlatformChallengeDetected,
    PlatformWriteBlocked,
)


def make_adapter(tmp_path, **kwargs):
    return BrowserPlatformAdapter(
        "douyin",
        data_dir=tmp_path,
        account_id="test-account",
        **kwargs,
    )


def test_browser_profiles_are_separate_and_lazy(tmp_path):
    douyin = make_adapter(tmp_path)
    xhs = BrowserPlatformAdapter("xiaohongshu", data_dir=tmp_path)

    assert douyin.data_dir != xhs.data_dir
    assert douyin.login_status()["connected"] is False
    assert xhs.login_status()["connected"] is False


def test_real_write_requires_local_write_switch(tmp_path):
    adapter = make_adapter(tmp_path, write_enabled=False)
    action = {
        "dry_run": False,
        "action_type": "comment",
        "target_id": "douyin:video:1",
        "target_url": "https://www.douyin.com/video/1",
        "text": "经过人工确认的测试评论",
    }

    with pytest.raises(PlatformWriteBlocked, match="platform_write_disabled"):
        adapter.execute_action(action)


def test_challenge_marker_stops_before_any_write(tmp_path):
    adapter = make_adapter(tmp_path)

    with pytest.raises(PlatformChallengeDetected):
        adapter._ensure_no_challenge("页面提示：安全验证")


def test_write_target_must_stay_on_platform_host(tmp_path):
    adapter = make_adapter(tmp_path, write_enabled=True)
    action = {
        "dry_run": False,
        "action_type": "comment",
        "target_id": "douyin:video:1",
        "target_url": "https://example.com/video/1",
        "text": "测试",
    }

    with pytest.raises(PlatformWriteBlocked, match="action_target_host_not_allowed"):
        adapter.execute_action(action)
