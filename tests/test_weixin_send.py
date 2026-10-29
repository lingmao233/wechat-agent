from wechat_agent.config import Settings
from wechat_agent.models import OutboundAction
from wechat_agent.weixin_ui import WeixinUiAdapter


class FakeGui:
    def __init__(self):
        self.calls = []

    def send_msg(self, text, who, verify=False):
        self.calls.append(("text", text, who, verify))
        return {"status": "成功", "message": "消息已发送"}

    def send_image(self, path, who, verify=False):
        self.calls.append(("image", path, who, verify))
        return {"status": "成功", "message": "图片已发送"}


def make_adapter(monkeypatch):
    import wechat_agent.weixin_ui as module

    monkeypatch.setattr(module, "_WeChatGUI", FakeGui)
    adapter = object.__new__(WeixinUiAdapter)
    adapter.settings = Settings(
        wechat_group_name="测试群",
        wechat_send_retry_count=2,
        wechat_send_verify_seconds=3,
    )
    adapter._gui_sender = None
    adapter._gui_sender_error = ""
    return adapter


def test_media_paths_use_native_image_sender(monkeypatch):
    adapter = make_adapter(monkeypatch)
    action = OutboundAction(
        account_id="a",
        group_id="g",
        group_name="测试群",
        text="已生成图片",
        media_paths=["C:/tmp/generated.png"],
    )

    adapter.send(action)

    assert adapter._gui_sender.calls == [
        ("image", "C:/tmp/generated.png", "测试群", True)
    ]


def test_text_action_uses_text_sender(monkeypatch):
    adapter = make_adapter(monkeypatch)
    action = OutboundAction("a", "g", "测试群", "收到")

    adapter.send(action)

    assert adapter._gui_sender.calls == [("text", "收到", "测试群", True)]

