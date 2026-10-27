from __future__ import annotations

import hashlib
import logging
import queue
import re
import time
from dataclasses import dataclass
from typing import Iterable
import unicodedata

import pyperclip

try:  # Keep imports friendly to documentation/build steps outside Windows.
    import uiautomation as auto
except ImportError:  # pragma: no cover - exercised only on non-Windows hosts
    auto = None  # type: ignore[assignment]

try:  # The mature 4.1.x bridge can materialize Qt's hidden accessibility tree.
    from wechatauto.uia_driver import WeChatUIA as _ReplicaWeChatUIA
except ImportError:  # pragma: no cover - installation guard on non-Windows hosts
    _ReplicaWeChatUIA = None  # type: ignore[assignment,misc]

try:  # The same bridge exposes the encrypted local message database reader.
    from wechatauto.db import Listener as _WeChatListener
    from wechatauto.db import WeChatDB as _WeChatDB
except ImportError:  # pragma: no cover - installation guard on non-Windows hosts
    _WeChatListener = None  # type: ignore[assignment,misc]
    _WeChatDB = None  # type: ignore[assignment,misc]

from .config import Settings
from .models import MessageEvent, OutboundAction, QuotedMessage, utc_now


log = logging.getLogger(__name__)


@dataclass(slots=True)
class UiMessage:
    sender: str
    text: str
    fingerprint: str
    sender_id: str = ""
    is_self: bool = False
    quoted_text: str = ""
    quoted_sender: str = ""


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
        self._message_db = None
        self._db_listener = None
        self._db_listener_group = ""
        self._db_message_queue: queue.Queue[dict[str, object]] = queue.Queue()
        self._last_read_from_database = False
        self._db_error_logged = False
        self._replica_driver = None
        # UIA sometimes exposes the local account's message row as
        # ``unknown``. Keep the database identity and a short content index
        # so our own replies cannot re-enter the model as user questions.
        self._self_wxid = ""
        self._self_display_name = ""
        self._self_content_cache_at = 0.0
        self._self_content_cache_group = ""
        self._self_content_cache: set[str] = set()

    def connect(self) -> None:
        self.window = self._find_window()
        if self.window is None:
            raise RuntimeError(
                "未找到 Weixin 主窗口。请先启动并登录专用测试微信号，"
                "然后确认窗口 class=mmui::MainWindow 或 Qt51514QWindowIcon。"
            )
        # Show/activate is a local UI action; it does not transmit a message.
        self.window.Show()
        self.window.SetActive()
        self.window.SetFocus()
        if not self._main_view_exists():
            # Weixin 4.1.x often exposes only a Qt shell until its internal
            # accessibility gate is materialized.  Use the maintained bridge
            # for this one preparation step, then keep this adapter's own
            # selectors and message/context behavior.
            self._materialize_accessibility_tree()
            self.window = self._find_window()
        if self.window is None or not self._main_view_exists():
            raise RuntimeError(
                "已找到微信窗口，但没有暴露 MainView 控件。请先安装 "
                "wechatauto-replica 并重试；如果仍失败，再开启 Windows 讲述人/辅助功能模式。"
            )

    def _find_window(self):
        classes = [self.settings.wechat_window_class, "mmui::MainWindow", "Qt51514QWindowIcon"]
        for class_name in dict.fromkeys(classes):
            candidate = auto.WindowControl(ClassName=class_name)
            if candidate.Exists(1):
                return candidate
        return None

    def _main_view_exists(self) -> bool:
        return bool(self.window is not None and self.window.GroupControl(AutomationId="MainView").Exists(1))

    @staticmethod
    def _materialize_accessibility_tree() -> bool:
        if _ReplicaWeChatUIA is None:
            return False
        try:
            driver = _ReplicaWeChatUIA(timeout=15.0, search_timeout=2.0)
            return bool(driver.ensure_materialized(timeout=8.0, force=True))
        except Exception:
            return False

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
        """Send through the validated 4.1.x driver and confirm it in the DB.

        The direct ``uiautomation`` implementation used to click a button and
        return immediately.  On Weixin 4.1.x that can report success even when
        focus was not in the chat input or the click did not create an outbound
        message.  ``wechatauto-replica`` checks that the pasted text landed
        before pressing Enter; the local DB read-back below is the second
        confirmation used by the previously working agent.
        """

        if _ReplicaWeChatUIA is None:
            raise RuntimeError("缺少 wechatauto-replica 的 WeChatUIA 发送驱动")
        db = self._get_message_db()
        target_wxid = self._resolve_group_wxid(db, action.group_name)
        before = db.get_messages(target_wxid, limit=1)
        before_seq = int(before[0].get("sort_seq") or 0) if before else 0

        sender = _ReplicaWeChatUIA(timeout=15.0, search_timeout=2.0)
        if not sender.send_text_to(action.text, action.group_name):
            raise RuntimeError(f"Weixin UIA 未确认发送到群：{action.group_name}")

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            for message in db.get_messages(target_wxid, limit=20):
                sequence = int(message.get("sort_seq") or 0)
                if sequence <= before_seq:
                    continue
                if not self._message_content_matches(message.get("content"), action.text):
                    continue
                sender_username = str(message.get("sender_username") or "")
                self_info = db.get_self_info()
                self_wxid = str(self_info.get("username") or db.wxid or "")
                if sender_username and self_wxid and sender_username != self_wxid:
                    continue
                self._group_open = True
                return
            time.sleep(0.5)

        raise RuntimeError(
            f"Weixin UIA 已返回发送成功，但数据库未回读到机器人消息：{action.group_name}"
        )

    def _get_message_db(self):
        if self._message_db is None:
            if _WeChatDB is None:
                raise RuntimeError("缺少 wechatauto-replica 的微信数据库读取器")
            self._message_db = _WeChatDB()
        return self._message_db

    def _get_replica_driver(self):
        if _ReplicaWeChatUIA is None:
            return None
        if self._replica_driver is None:
            self._replica_driver = _ReplicaWeChatUIA(timeout=15.0, search_timeout=2.0)
        return self._replica_driver

    def _start_db_listener(self, group_name: str) -> bool:
        """Listen to one database conversation, independent of the open UI tab."""

        if _WeChatListener is None:
            return False
        db = self._get_message_db()
        target_wxid = self._resolve_group_wxid(db, group_name)
        if self._db_listener is not None and self._db_listener_group == target_wxid:
            return True
        self._stop_db_listener()
        watermark = None
        if not self.settings.wechat_skip_existing_on_start:
            watermark = {target_wxid: 0}
        listener = _WeChatListener(
            db,
            interval=max(0.5, float(self.settings.wechat_poll_seconds)),
            watermark=watermark,
            watermark_file="",
        )
        listener.add_listener(target_wxid, self._queue_db_message)
        listener.start()
        self._db_listener = listener
        self._db_listener_group = target_wxid
        log.info("WeChat DB listener active group=%s wxid=%s", group_name, target_wxid)
        return True

    def _stop_db_listener(self) -> None:
        listener = self._db_listener
        self._db_listener = None
        self._db_listener_group = ""
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        while True:
            try:
                self._db_message_queue.get_nowait()
            except queue.Empty:
                break

    def _queue_db_message(self, message: dict[str, object], _listener=None) -> None:
        self._db_message_queue.put(dict(message))

    def _drain_db_messages(self, limit: int = 30) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _ in range(max(1, limit)):
            try:
                rows.append(self._db_message_queue.get_nowait())
            except queue.Empty:
                break
        return rows

    def close(self) -> None:
        """Stop the database listener when the agent thread exits."""

        self._stop_db_listener()

    def _self_identity(self) -> tuple[str, str]:
        """Return the stable wxid and display name of the logged-in account."""
        if self._self_wxid:
            return self._self_wxid, self._self_display_name
        try:
            db = self._get_message_db()
            info = db.get_self_info() or {}
            self._self_wxid = str(info.get("username") or getattr(db, "wxid", "") or "").strip()
            self._self_display_name = str(
                info.get("remark") or info.get("nick_name") or self._self_wxid
            ).strip()
        except Exception:
            # UIA remains usable when the local DB is temporarily locked; the
            # configured nickname and recent-outbound fallback still apply.
            self._self_wxid = ""
            self._self_display_name = ""
        return self._self_wxid, self._self_display_name

    def _db_self_content(self, group_name: str) -> set[str]:
        """Read a short set of self-authored texts for UIA sender recovery."""
        now = time.monotonic()
        if (
            group_name == self._self_content_cache_group
            and now - self._self_content_cache_at < 1.0
        ):
            return self._self_content_cache
        texts: set[str] = set()
        try:
            db = self._get_message_db()
            self_wxid, _ = self._self_identity()
            target_wxid = self._resolve_group_wxid(db, group_name)
            if self_wxid and target_wxid:
                for row in db.get_messages(target_wxid, limit=100):
                    sender = str(row.get("sender_username") or "").strip()
                    if sender != self_wxid:
                        continue
                    texts.update(self._text_variants(row.get("content")))
        except Exception:
            # The send path and the UIA path must remain independently usable;
            # failure here only removes the database-assisted self guard.
            texts = set()
        self._self_content_cache_group = group_name
        self._self_content_cache_at = now
        self._self_content_cache = texts
        return texts

    @staticmethod
    def _normalize_message_text(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        # Qt/UIA can expose zero-width formatting marks and four-per-em spaces
        # that are absent from the database row.  Case folding also makes the
        # fallback independent of how the sender label was rendered.
        text = "".join(char for char in text if unicodedata.category(char) not in {"Cf"})
        return " ".join(text.split()).strip().casefold()

    @classmethod
    def _text_variants(cls, value: object) -> set[str]:
        raw = str(value or "").strip()
        if not raw:
            return set()
        variants: set[str] = set()
        normalized = cls._normalize_message_text(raw)
        if normalized:
            variants.add(normalized)
        # Some database builds prefix content with the sender on its own line
        # (``wxid\nmessage``), while UIA exposes only ``message``.  Include the
        # suffix as a comparison variant without changing what gets displayed.
        lines = [cls._normalize_message_text(line) for line in raw.splitlines()]
        lines = [line for line in lines if line]
        if len(lines) > 1:
            suffix = " ".join(lines[1:]).strip()
            if suffix:
                variants.add(suffix)
        # A few database exports put a short sender label and the message on
        # one line (``Away：message``).  Treat the part after the first
        # full-width/ASCII colon as another comparison form, while avoiding
        # URL schemes such as ``https://``.
        inline = re.split(r"[:：]", raw, maxsplit=1)
        if len(inline) == 2 and "://" not in raw:
            prefix, suffix = inline[0].strip(), inline[1].strip()
            if prefix and suffix and len(prefix) <= 80:
                normalized_suffix = cls._normalize_message_text(suffix)
                if normalized_suffix:
                    variants.add(normalized_suffix)
        return variants

    @staticmethod
    def _resolve_group_wxid(db, group_name: str) -> str:
        configured = (group_name or "").strip()
        groups = db.get_groups()
        exact = [
            item
            for item in groups
            if str(item.get("name") or "").strip() == configured
            or str(item.get("username") or "").strip() == configured
        ]
        if len(exact) == 1:
            return str(exact[0].get("username") or "")
        if not exact:
            fuzzy = [
                item
                for item in groups
                if configured.casefold() in str(item.get("name") or "").casefold()
            ]
            if len(fuzzy) == 1:
                return str(fuzzy[0].get("username") or "")
        raise RuntimeError(f"微信数据库中找不到唯一目标群：{configured}")

    @staticmethod
    def _message_content_matches(content: object, expected: str) -> bool:
        return bool(
            WeixinUiAdapter._text_variants(content)
            & WeixinUiAdapter._text_variants(expected)
        )

    def _db_row_to_ui_message(
        self,
        row: dict[str, object],
        self_texts: set[str],
    ) -> UiMessage | None:
        text = str(row.get("content") or "").strip()
        if not text or text in {"查看更多消息", "以下为新消息"}:
            return None
        message_type = str(row.get("type") or "").casefold()
        if message_type and message_type not in {"text", "text_message", "unknown"}:
            # Keep text rows with an empty/unknown type, but do not turn image,
            # sticker, or system placeholders into member questions.
            if text.startswith("[") and text.endswith("]"):
                return None
        sender_id = str(row.get("sender_username") or "").strip()
        self_wxid, self_name = self._self_identity()
        is_self = bool(sender_id and self_wxid and sender_id == self_wxid)
        if is_self:
            sender = self_name or self.settings.wechat_self_name.strip() or sender_id
        elif sender_id:
            try:
                sender = self._get_message_db().get_nickname(sender_id) or sender_id
            except Exception:
                sender = sender_id
        else:
            # ``sender_id`` is the raw contact id when an old message shard
            # cannot resolve Name2Id.  It is still more useful than assigning
            # every member the literal string ``unknown``.
            sender_id = str(row.get("sender_id") or "").strip()
            sender = sender_id or "unknown"
        # Content-only matching is safe only when the database could not
        # provide any sender identity. A real member who repeats the bot's
        # sentence must remain a member message.
        if not is_self and not sender_id and self._text_variants(text) & self_texts:
            is_self = True
            sender = self_name or self.settings.wechat_self_name.strip() or sender
        sort_seq = str(row.get("sort_seq") or "")
        local_id = str(row.get("local_id") or "")
        shard = str(row.get("shard") or "")
        stable_id = f"db:{shard}:{sort_seq}:{local_id}" if (sort_seq or local_id) else ""
        fingerprint = hashlib.sha256(
            f"{stable_id}\x1f{sender_id or sender}\x1f{text}".encode("utf-8")
        ).hexdigest()
        return UiMessage(
            sender=sender,
            sender_id=sender_id,
            text=text,
            fingerprint=fingerprint,
            is_self=is_self,
        )

    def _read_database_messages(self, group_name: str, limit: int = 30) -> list[UiMessage] | None:
        if _WeChatListener is None:
            return None
        try:
            self._start_db_listener(group_name)
            rows = self._drain_db_messages(limit)
            self_texts = self._db_self_content(group_name)
            messages: list[UiMessage] = []
            for row in rows:
                message = self._db_row_to_ui_message(row, self_texts)
                if message is not None:
                    messages.append(message)
            self._db_error_logged = False
            return messages
        except Exception:
            # A locked/unavailable DB should not prevent the existing UIA
            # fallback from being used on installations without a readable
            # message shard.
            if not self._db_error_logged:
                log.warning("微信数据库监听不可用，回退 UIA 消息读取", exc_info=True)
                self._db_error_logged = True
            return None

    def _group_name_matches(self, current: str, configured: str) -> bool:
        current = (current or "").strip()
        configured = (configured or "").strip()
        if not current or not configured:
            return False
        if current.casefold() == configured.casefold():
            return True
        # WECHAT_GROUP_NAME may intentionally be the stable wxid. Resolve it
        # through the local contact DB before comparing the UI's display name.
        try:
            db = self._get_message_db()
            target = self._resolve_group_wxid(db, configured)
            for item in db.get_groups():
                if str(item.get("username") or "") != target:
                    continue
                return current.casefold() == str(item.get("name") or "").strip().casefold()
        except Exception:
            return False
        return False

    def _ensure_visible_group(self, group_name: str) -> bool:
        """Ensure the UIA fallback is actually showing the configured group.

        The database listener is the normal path and does not need a visible
        conversation. This guard exists only for installations where the local
        message DB cannot be read; it prevents a manually selected chat from
        being treated as the configured group.
        """

        group = (group_name or "").strip()
        if not group:
            return False
        driver = self._get_replica_driver()
        if driver is not None:
            try:
                if driver.ensure_window(wake=False):
                    current = (driver.current_chat() or "").strip()
                    if self._group_name_matches(current, group):
                        self._group_open = True
                        return True
                if driver.open_chat(group):
                    current = (driver.current_chat() or "").strip()
                    if self._group_name_matches(current, group):
                        self._group_open = True
                        return True
            except Exception:
                log.debug("无法校验或打开配置群 %s", group, exc_info=True)
            return False

        # Older installations without the replica driver retain the original
        # selector-based opener. It is used only when no database listener is
        # available; a later poll will retry the open rather than trusting a
        # stale _group_open flag after the operator changes chats manually.
        try:
            self.open_group(group)
            return True
        except Exception:
            log.debug("UIA 回退无法打开配置群 %s", group, exc_info=True)
            return False

    def _read_visible_messages(self, limit: int = 30) -> list[UiMessage]:
        self._require_window()
        page = self.window.GroupControl(AutomationId="chat_message_page")
        if not page.Exists(1):
            return []
        group_name = self.settings.wechat_group_name.strip()
        self_texts = self._db_self_content(group_name) if group_name else set()
        rows = page.GetChildren()
        parsed: list[UiMessage] = []
        for row in rows[-limit:]:
            text, quoted = self._row_content(row)
            if not text or text in {"查看更多消息", "以下为新消息"}:
                continue
            sender = self._row_sender(row) or "unknown"
            is_self = self._is_self_sender(sender)
            if (
                not is_self
                and sender.strip().casefold() in {"unknown", "我", "自己", "me", "you", "you (me)"}
                and self._text_variants(text) & self_texts
            ):
                is_self = True
            if is_self and sender.casefold() == "unknown" and self._self_display_name:
                sender = self._self_display_name
            quoted_text = quoted.text if quoted else ""
            quoted_sender = quoted.sender_name if quoted else ""
            fingerprint = hashlib.sha256(
                f"{sender}\x1f{text}\x1f{quoted_sender}\x1f{quoted_text}".encode("utf-8")
            ).hexdigest()
            parsed.append(
                UiMessage(
                    sender=sender,
                    sender_id=sender,
                    text=text,
                    fingerprint=fingerprint,
                    is_self=is_self,
                    quoted_text=quoted_text,
                    quoted_sender=quoted_sender,
                )
            )
        return parsed

    def read_messages(self, limit: int = 30) -> list[UiMessage]:
        """Read target-group messages from DB, falling back to verified UIA rows."""

        group_name = self.settings.wechat_group_name.strip()
        self._last_read_from_database = False
        if group_name:
            database_messages = self._read_database_messages(group_name, limit=limit)
            if database_messages is not None:
                self._last_read_from_database = True
                return database_messages
            if not self._ensure_visible_group(group_name):
                return []
        return self._read_visible_messages(limit=limit)

    def poll(self, account_id: str, group_name: str | None = None) -> list[MessageEvent]:
        """Read one configured group and deduplicate rows.

        When the DB listener is active, the selected Weixin tab is irrelevant.
        UIA rows are accepted only after the fallback verifies the visible chat.
        """
        group = (group_name or self.settings.wechat_group_name).strip()
        events: list[MessageEvent] = []
        for msg in self.read_messages():
            key = f"{group}\x1f{msg.fingerprint}"
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            events.append(
                MessageEvent(
                    account_id=account_id,
                    # Keep the configured group label as the storage namespace
                    # for compatibility with existing reply/memory rows. The
                    # actual listener target remains the resolved wxid in
                    # ``_db_listener_group``.
                    group_id=group,
                    group_name=group,
                    member_id=msg.sender_id or msg.sender,
                    member_name=msg.sender or msg.sender_id or "unknown",
                    text=msg.text,
                    observed_at=utc_now(),
                    source="weixin_ui_4.1",
                    external_message_id=msg.fingerprint,
                    is_self=msg.is_self,
                    reply_to=(
                        QuotedMessage(text=msg.quoted_text, sender_name=msg.quoted_sender)
                        if msg.quoted_text
                        else None
                    ),
                )
            )
        # Listener.add_listener establishes its own DB watermark. Applying the
        # UI startup baseline here would discard a real message that arrived
        # between listener start and the first poll.
        if getattr(self, "_last_read_from_database", False):
            self._primed = True
            return events
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
        self_wxid, self_name = self._self_identity()
        if normalized and normalized in {
            self_wxid.casefold(),
            self_name.casefold(),
        }:
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
    def _row_names(row) -> list[str]:
        names: list[str] = []

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
        return names

    @classmethod
    def _row_content(cls, row) -> tuple[str, QuotedMessage | None]:
        """Extract the visible message and a best-effort quoted message.

        Weixin's accessibility tree differs between builds. Some builds expose
        one combined label (``引用：张三：原文``), while others expose a marker,
        sender, quoted text, and the new message as sibling descendants. The
        parser intentionally accepts both forms and leaves the normal message
        path unchanged when no quote marker is exposed.
        """

        names = cls._row_names(row)
        candidates = [n for n in names if n not in {"消息", "消息列表"}]
        if not candidates:
            return "", None
        quoted = cls._parse_quoted(candidates)
        current = candidates[-1]
        try:
            row_label = (row.Name or "").strip()
        except Exception:
            row_label = ""
        if row_label and row_label not in {"消息", "消息列表"} and not re.match(
            r"^\s*(?:引用消息|回复消息|引用|回复|quoted|reply)(?:\s*[:：\-]\s*|\s+|\s*$)",
            row_label,
            re.IGNORECASE,
        ):
            current = row_label
        if quoted is None:
            return current, None
        # The current message is normally the last non-system descendant. If
        # the tree only exposes the quote label, keep it as the message rather
        # than silently dropping the row.
        return current, quoted

    @staticmethod
    def _parse_quoted(candidates: list[str]) -> QuotedMessage | None:
        marker = re.compile(
            r"^\s*(?:引用消息|回复消息|引用|回复|quoted|reply)(?:\s*[:：\-]\s*|\s+|\s*$)",
            re.IGNORECASE,
        )
        for index, value in enumerate(candidates):
            if not marker.search(value):
                continue
            inline = re.search(
                r"^\s*(?:引用消息|回复消息|引用|回复|quoted|reply)\s*"
                r"(?:[:：\-]\s*)?"
                r"(?:(?P<sender>[^:：\n]{1,80})\s*[:：]\s*)?"
                r"(?P<text>.+)$",
                value,
                re.IGNORECASE,
            )
            if inline and inline.group("text"):
                text = inline.group("text").strip()
                if text and text.casefold() not in {"消息", "message"}:
                    return QuotedMessage(
                        text=text,
                        sender_name=(inline.group("sender") or "").strip(),
                    )
            # Separate accessibility nodes commonly follow the marker. Use the
            # next non-empty node as quoted content and the one before it as a
            # sender only when the shape is unambiguous.
            tail = [item.strip() for item in candidates[index + 1 :] if item.strip()]
            if len(tail) < 2:
                continue
            content = tail[0]
            sender = ""
            if len(tail) >= 2 and re.search(r"[:：]", tail[0]):
                maybe_sender, maybe_text = re.split(r"[:：]", tail[0], maxsplit=1)
                if maybe_text.strip():
                    sender, content = maybe_sender.strip(), maybe_text.strip()
            if not sender and index > 0:
                previous = candidates[index - 1].strip()
                if previous and not marker.search(previous):
                    sender = previous
            if content and content.casefold() not in {"消息", "message"}:
                return QuotedMessage(text=content, sender_name=sender)
        return None

    @classmethod
    def _row_text(cls, row) -> str:
        """Compatibility helper retained for integrations importing it."""

        candidates = [n for n in cls._row_names(row) if n not in {"消息", "消息列表"}]
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
