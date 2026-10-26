from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Iterable

import pyperclip

try:  # Keep imports friendly to documentation/build steps outside Windows.
    import uiautomation as auto
except ImportError:  # pragma: no cover - exercised only on non-Windows hosts
    auto = None  # type: ignore[assignment]

from .config import Settings
from .models import MessageEvent, OutboundAction, utc_now


@dataclass(slots=True)
class UiMessage:
    sender: str
    text: str
    fingerprint: str
    is_self: bool = False


class WeixinUiAdapter:
    """Minimal Weixin 4.1.x adapter.

    The selectors are based on the current Weixin 4.1.x accessibility tree:
    ``mmui::MainWindow``, ``mmui::ChatInputField`` and
    ``chat_message_page``. The adapter does not log in or send anything during
    construction; the caller must explicitly open a configured group and pass
    the outbound action policy first.
    """

    def __init__(self, settings: Settings):
        if auto is None:
            raise RuntimeError("uiautomation is required on Windows")
        self.settings = settings
        auto.SetGlobalSearchTimeout(1.5)
        self.window = None
        self._group_open = False
        self._primed = False
        self._seen_keys: set[str] = set()

    def connect(self) -> None:
        classes = [self.settings.wechat_window_class, "mmui::MainWindow", "Qt51514QWindowIcon"]
        self.window = None
        for class_name in dict.fromkeys(classes):
            candidate = auto.WindowControl(ClassName=class_name)
            if candidate.Exists(1):
                self.window = candidate
                break
        if self.window is None:
            raise RuntimeError(
                "未找到 Weixin 主窗口。请先启动并登录专用测试微信号，"
                "然后确认窗口 class=mmui::MainWindow 或 Qt51514QWindowIcon。"
            )
        # Show/activate is a local UI action; it does not transmit a message.
        self.window.Show()
        self.window.SetActive()
        self.window.SetFocus()
        if not self.window.GroupControl(AutomationId="MainView").Exists(1):
            raise RuntimeError(
                "已找到微信窗口，但没有暴露 MainView 控件。请在微信设置中开启"
                "讲述人/辅助功能模式后重试；本程序不会自动修改 Windows 辅助功能设置。"
            )

    def check_ready(self) -> dict[str, str]:
        self.connect()
        return {
            "window_class": self.window.ClassName,
            "window_name": self.window.Name,
            "process_id": str(self.window.ProcessId),
            "group_configured": self.settings.wechat_group_name,
        }

    def open_group(self, group_name: str | None = None) -> None:
        self._require_window()
        name = (group_name or self.settings.wechat_group_name).strip()
        if not name:
            raise ValueError("WECHAT_GROUP_NAME 必须设置为专用测试群名称")
        self._click_first(
            self.window.ButtonControl(Name="微信", ClassName="mmui::XTabBarItem")
        )
        search = self._find_search()
        self._click(search)
        pyperclip.copy(name)
        auto.SendKeys("{Ctrl}v")
        time.sleep(0.6)
        items = auto.ListControl(Depth=5).GetChildren()
        candidates = [item for item in items if "XTableCell" not in item.ClassName]
        if not candidates:
            raise RuntimeError(f"未找到测试群：{name}")
        self._click(candidates[0])
        time.sleep(0.4)
        self._group_open = True

    def send(self, action: OutboundAction) -> None:
        """Send one text action after policy authorization."""
        self._require_window()
        if not self._group_open or action.group_name != self.settings.wechat_group_name:
            self.open_group(action.group_name)
        input_box = self.window.EditControl(ClassName="mmui::ChatInputField")
        if not input_box.Exists(1):
            raise RuntimeError("未找到 Weixin 聊天输入框")
        self._click(input_box)
        for at_name in action.at_names:
            if at_name:
                auto.SendKeys("@" + at_name)
                auto.SendKeys("{enter}")
        pyperclip.copy(action.text)
        auto.SendKeys("{Ctrl}v")
        send_button = self.window.ButtonControl(Name="发送")
        if send_button.Exists(1):
            self._click(send_button)
        else:
            auto.SendKeys("{enter}")

    def read_messages(self, limit: int = 30) -> list[UiMessage]:
        self._require_window()
        page = self.window.GroupControl(AutomationId="chat_message_page")
        if not page.Exists(1):
            return []
        rows = page.GetChildren()
        parsed: list[UiMessage] = []
        for row in rows[-limit:]:
            text = self._row_text(row)
            if not text or text in {"查看更多消息", "以下为新消息"}:
                continue
            sender = self._row_sender(row) or "unknown"
            fingerprint = hashlib.sha256(f"{sender}\x1f{text}".encode("utf-8")).hexdigest()
            parsed.append(
                UiMessage(
                    sender=sender,
                    text=text,
                    fingerprint=fingerprint,
                    is_self=self._is_self_sender(sender),
                )
            )
        return parsed

    def poll(self, account_id: str, group_name: str | None = None) -> list[MessageEvent]:
        """Read visible messages from the configured group and deduplicate rows."""
        group = (group_name or self.settings.wechat_group_name).strip()
        if not self._group_open:
            self.open_group(group)
        events: list[MessageEvent] = []
        for msg in self.read_messages():
            key = f"{group}\x1f{msg.fingerprint}"
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            events.append(
                MessageEvent(
                    account_id=account_id,
                    group_id=group,
                    group_name=group,
                    member_id=msg.sender,
                    member_name=msg.sender,
                    text=msg.text,
                    observed_at=utc_now(),
                    source="weixin_ui_4.1",
                    external_message_id=msg.fingerprint,
                    is_self=msg.is_self,
                )
            )
        if not self._primed and self.settings.wechat_skip_existing_on_start:
            # The first visible page often contains historical rows.  Treat
            # that snapshot as the startup baseline; only later polls can
            # trigger the agent.  The durable reply ledger remains the second
            # line of defence for rows seen during runtime and after restart.
            self._primed = True
            return []
        self._primed = True
        return events

    def _is_self_sender(self, sender: str) -> bool:
        normalized = sender.strip().casefold()
        configured = self.settings.wechat_self_name.strip().casefold()
        if configured and normalized == configured:
            return True
        # Weixin commonly exposes the local sender as 我/自己/You instead of
        # the account nickname.  This is only a trigger guard; the configured
        # self name remains the authoritative match when available.
        return normalized in {"我", "自己", "me", "you", "you (me)"}

    def _find_search(self):
        for name in ("搜索", "Search"):
            box = self.window.EditControl(Name=name)
            if box.Exists(0.5):
                return box
        box = self.window.EditControl(Depth=15)
        if not box.Exists(1):
            raise RuntimeError("未找到 Weixin 搜索框")
        return box

    @staticmethod
    def _click(control) -> None:
        x, y = control.GetPosition()
        auto.Click(x, y)

    def _click_first(self, control) -> None:
        if not control.Exists(1):
            raise RuntimeError(f"未找到控件：{control.ControlTypeName} {control.Name}")
        self._click(control)

    def _require_window(self) -> None:
        if self.window is None:
            self.connect()

    @staticmethod
    def _row_text(row) -> str:
        names = []

        def walk(control, depth: int = 0) -> None:
            if depth > 4:
                return
            try:
                name = (control.Name or "").strip()
                if name and name not in names:
                    names.append(name)
                for child in control.GetChildren():
                    walk(child, depth + 1)
            except Exception:
                return

        walk(row)
        # The row name is usually the actual message on 4.1.x. When it is a
        # container name, the last non-system descendant is the best fallback.
        candidates = [n for n in names if n not in {"消息", "消息列表"}]
        return candidates[-1] if candidates else ""

    @staticmethod
    def _row_sender(row) -> str:
        try:
            buttons = row.GetChildren()
            for child in buttons:
                if child.ControlTypeName == "ButtonControl" and child.Name:
                    return child.Name.strip()
        except Exception:
            pass
        return ""
