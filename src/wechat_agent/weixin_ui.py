from __future__ import annotations

import hashlib
import logging
import queue
import re
import time
from dataclasses import dataclass, field
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

try:  # Coordinate/OCR sender for Weixin 4.1.x's self-rendered chat surface.
    from wechatauto.guia import WeChatGUI as _WeChatGUI
except ImportError:  # pragma: no cover - optional on non-Windows/test hosts
    _WeChatGUI = None  # type: ignore[assignment,misc]

try:  # The same bridge exposes the encrypted local message database reader.
    from wechatauto.db import Listener as _WeChatListener
    from wechatauto.db import WeChatDB as _WeChatDB
except ImportError:  # pragma: no cover - installation guard on non-Windows hosts
    _WeChatListener = None  # type: ignore[assignment,misc]
    _WeChatDB = None  # type: ignore[assignment,misc]

try:
    from wechatauto.media import MediaDownloader as _MediaDownloader
except ImportError:  # pragma: no cover - optional on non-Windows/test hosts
    _MediaDownloader = None  # type: ignore[assignment,misc]

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
    group_name: str = ""
    image_paths: list[str] = field(default_factory=list)
    # Stable WeChat conversation wxid from the message database.  Keep this
    # after existing fields so older adapters/tests using keyword or positional
    # construction remain compatible.
    group_id: str = ""


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
        self._db_listener_group = ""  # legacy: first registered wxid
        self._db_listener_groups: set[str] = set()
        self._db_group_names: dict[str, str] = {}
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
        self._media_downloader = None
        # ``WeChatGUI`` is the reliable 4.1.x coordinate/OCR sender.  Keep it
        # lazy because construction calibrates the desktop layout and should
        # not happen during read-only startup or tests.
        self._gui_sender = None
        self._gui_sender_error = ""

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
            "group_configured": ", ".join(self._configured_groups()),
        }

    def open_group(self, group_name: str | None = None) -> None:
        self._require_window()
        name = (group_name or self.settings.wechat_group_name).strip()
        if not name:
            raise ValueError("WECHAT_GROUP_NAMES 必须设置至少一个专用测试群名称")
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

        # Generated images must remain native image messages.  The legacy UIA
        # bridge can only paste text, so never fall back to sending a local
        # path or URL as chat text.
        if action.media_paths:
            self._send_with_gui(action)
            self._group_open = True
            return

        # Prefer the coordinate/OCR route.  It has its own input-state checks,
        # retry loop and optional database read-back, which fixes the common
        # 4.1.x failure where UIA reports a click but the rendered input box
        # never received the text.
        if self._get_gui_sender() is not None:
            self._send_with_gui(action)
            self._group_open = True
            return

        if _ReplicaWeChatUIA is None:
            detail = self._gui_sender_error or "缺少可用的 WeChatGUI/WeChatUIA 发送驱动"
            raise RuntimeError(detail)
        db = self._get_message_db()
        target_wxid = self._resolve_group_wxid(db, action.group_name)
        before = db.get_messages(target_wxid, limit=1)
        before_seq = int(before[0].get("sort_seq") or 0) if before else 0

        sender = _ReplicaWeChatUIA(timeout=15.0, search_timeout=2.0)
        attempts = max(1, int(getattr(self.settings, "wechat_send_retry_count", 3)))
        sent = False
        for attempt in range(attempts):
            if sender.send_text_to(action.text, action.group_name):
                sent = True
                break
            if attempt + 1 < attempts:
                log.warning(
                    "Weixin UIA text send retry=%s/%s group=%s",
                    attempt + 2,
                    attempts,
                    action.group_name,
                )
                time.sleep(0.5)
        if not sent:
            raise RuntimeError(f"Weixin UIA 未确认发送到群：{action.group_name}")

        deadline = time.monotonic() + float(
            getattr(self.settings, "wechat_send_verify_seconds", 15.0)
        )
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

    def _get_gui_sender(self):
        """Create the coordinate/OCR sender once, without affecting read-only mode."""

        if self._gui_sender is not None:
            return self._gui_sender
        if _WeChatGUI is None:
            self._gui_sender_error = "未安装 wechatauto.guia.WeChatGUI"
            return None
        try:
            self._gui_sender = _WeChatGUI()
            return self._gui_sender
        except Exception as exc:  # noqa: BLE001 - preserve fallback diagnostics
            self._gui_sender_error = f"WeChatGUI 初始化失败：{type(exc).__name__}: {exc}"
            log.warning("WeChatGUI unavailable: %s", self._gui_sender_error)
            return None

    @staticmethod
    def _response_message(response: object) -> str:
        if isinstance(response, dict):
            value = response.get("message") or response.get("error") or response
            return str(value)
        return str(response)

    def _send_with_gui(self, action: OutboundAction) -> None:
        """Send text or native image attachments through ``WeChatGUI``.

        ``send_msg``/``send_image`` return ``WxResponse`` objects whose
        truthiness means the provider confirmed success.  The adapter accepts
        simple boolean fakes as well, making this path unit-testable without a
        live Weixin process.
        """

        sender = self._get_gui_sender()
        if sender is None:
            raise RuntimeError(self._gui_sender_error or "WeChatGUI 不可用，无法发送图片附件")
        verify = True
        attempts = max(1, int(self.settings.wechat_send_retry_count))
        last_detail = "未知发送错误"
        for index, path in enumerate(action.media_paths):
            response = None
            try:
                response = sender.send_image(path, action.group_name, verify=verify)
            except Exception as exc:  # noqa: BLE001 - surface driver failure
                last_detail = f"图片发送异常：{type(exc).__name__}: {exc}"
            else:
                if bool(response):
                    continue
                last_detail = f"图片发送失败：{self._response_message(response)}"
            # A failed response can mean the image was sent but read-back was
            # delayed.  Retry only the configured bounded number of times;
            # never switch to text/UIA and never expose a local path in chat.
            for retry in range(1, attempts):
                log.warning(
                    "WeChat image send retry=%s/%s path=%s reason=%s",
                    retry + 1,
                    attempts,
                    path,
                    last_detail,
                )
                try:
                    response = sender.send_image(path, action.group_name, verify=verify)
                except Exception as exc:  # noqa: BLE001
                    last_detail = f"图片发送异常：{type(exc).__name__}: {exc}"
                    continue
                if bool(response):
                    break
                last_detail = f"图片发送失败：{self._response_message(response)}"
            else:
                raise RuntimeError(
                    f"Weixin 图片附件发送失败（第 {index + 1} 张）：{last_detail}"
                )

        if action.media_paths:
            return
        try:
            response = sender.send_msg(action.text, action.group_name, verify=verify)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Weixin 文字发送异常：{type(exc).__name__}: {exc}") from exc
        if not bool(response):
            raise RuntimeError(f"Weixin 文字发送失败：{self._response_message(response)}")

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

    def _configured_groups(self) -> list[str]:
        groups = getattr(self.settings, "wechat_group_names", None) or [
            self.settings.wechat_group_name
        ]
        return list(dict.fromkeys(str(value).strip() for value in groups if str(value).strip()))

    def _start_db_listeners(self, group_names: list[str] | None = None) -> bool:
        """Listen to all configured conversations through one DB poller."""

        if _WeChatListener is None:
            return False
        db = self._get_message_db()
        names = group_names or self._configured_groups()
        targets = {self._resolve_group_wxid(db, name): name for name in names}
        if self._db_listener is not None and set(targets) == self._db_listener_groups:
            return True
        self._stop_db_listener()
        watermark = None if self.settings.wechat_skip_existing_on_start else {
            target: 0 for target in targets
        }
        listener = _WeChatListener(
            db,
            interval=max(0.5, float(self.settings.wechat_poll_seconds)),
            watermark=watermark,
            watermark_file="",
        )
        for target_wxid in targets:
            listener.add_listener(target_wxid, self._queue_db_message)
        listener.start()
        self._db_listener = listener
        self._db_listener_groups = set(targets)
        self._db_group_names = targets
        self._db_listener_group = next(iter(targets), "")
        log.info("WeChat DB listeners active groups=%s", ", ".join(names))
        return True

    def _start_db_listener(self, group_name: str) -> bool:
        """Compatibility wrapper for callers that start one group."""

        return self._start_db_listeners([group_name])

    def _stop_db_listener(self) -> None:
        listener = self._db_listener
        self._db_listener = None
        self._db_listener_group = ""
        self._db_listener_groups = set()
        self._db_group_names = {}
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

    def _download_db_image(self, row: dict[str, object], conversation_wxid: str) -> list[str]:
        if not self.settings.wechat_image_recognition_enabled:
            return []
        if not conversation_wxid or _MediaDownloader is None:
            return []
        try:
            if self._media_downloader is None:
                self._media_downloader = _MediaDownloader(
                    self._get_message_db(), save_dir=str(self.settings.wechat_media_dir)
                )
            local_id = int(row.get("local_id") or 0)
            if not local_id:
                return []
            path = self._media_downloader.download_image(
                conversation_wxid,
                local_id,
                save_dir=str(self.settings.wechat_media_dir),
                tier="best",
                shard=str(row.get("shard") or "") or None,
            )
            return [str(path)] if path else []
        except Exception:
            log.debug("微信图片无法从本地附件解密", exc_info=True)
            return []

    def _db_row_to_ui_message(
        self,
        row: dict[str, object],
        self_texts: set[str],
        group_name: str = "",
    ) -> UiMessage | None:
        text = str(row.get("content") or "").strip()
        if not text or text in {"查看更多消息", "以下为新消息"}:
            return None
        message_type = str(row.get("type") or "").casefold()
        image_types = {"图片", "image", "picture", "pic", "3"}
        is_image = message_type in image_types or text.startswith("[图片")
        if message_type and message_type not in {"text", "text_message", "unknown", *image_types}:
            # Keep text rows with an empty/unknown type, but do not turn image,
            # sticker, or system placeholders into member questions.
            if text.startswith("[") and text.endswith("]") and not is_image:
                return None
        image_paths = self._download_db_image(row, str(row.get("username") or "")) if is_image else []
        if is_image and not text:
            text = "[图片]"
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
            group_name=group_name or self._db_group_names.get(str(row.get("username") or ""), ""),
            image_paths=image_paths,
            group_id=str(row.get("username") or "").strip(),
        )

    def _read_database_messages(self, group_names: list[str], limit: int = 30) -> list[UiMessage] | None:
        if _WeChatListener is None:
            return None
        try:
            self._start_db_listeners(group_names)
            rows = self._drain_db_messages(limit)
            allowed = {
                self._resolve_group_wxid(self._get_message_db(), name): name for name in group_names
            }
            self_texts = {name: self._db_self_content(name) for name in group_names}
            messages: list[UiMessage] = []
            for row in rows:
                username = str(row.get("username") or "")
                group_name = allowed.get(username) or self._db_group_names.get(username, "")
                if not group_name:
                    continue
                message = self._db_row_to_ui_message(row, self_texts.get(group_name, set()), group_name)
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

    def _read_visible_messages(self, limit: int = 30, group_name: str | None = None) -> list[UiMessage]:
        self._require_window()
        page = self.window.GroupControl(AutomationId="chat_message_page")
        if not page.Exists(1):
            return []
        group_name = (group_name or self.settings.wechat_group_name).strip()
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
            try:
                visible_group_id = self._resolve_group_wxid(self._get_message_db(), group_name)
            except Exception:
                visible_group_id = ""
            parsed.append(
                UiMessage(
                    sender=sender,
                    sender_id=sender,
                    text=text,
                    fingerprint=fingerprint,
                    is_self=is_self,
                    quoted_text=quoted_text,
                    quoted_sender=quoted_sender,
                    group_name=group_name,
                    group_id=visible_group_id,
                )
            )
        return parsed

    def read_messages(self, limit: int = 30) -> list[UiMessage]:
        """Read all configured-group messages from DB, with UIA fallback."""

        group_names = self._configured_groups()
        self._last_read_from_database = False
        if group_names:
            database_messages = self._read_database_messages(group_names, limit=limit)
            if database_messages is not None:
                self._last_read_from_database = True
                return database_messages
            visible: list[UiMessage] = []
            for group_name in group_names:
                if self._ensure_visible_group(group_name):
                    visible.extend(self._read_visible_messages(limit=limit, group_name=group_name))
            return visible
        return self._read_visible_messages(limit=limit)

    def poll(self, account_id: str, group_name: str | None = None) -> list[MessageEvent]:
        """Read all configured groups and deduplicate rows.

        When the DB listener is active, the selected Weixin tab is irrelevant.
        UIA rows are accepted only after the fallback verifies the visible chat.
        """
        requested_group = (group_name or "").strip()
        events: list[MessageEvent] = []
        for msg in self.read_messages():
            group_name = msg.group_name or requested_group or self.settings.wechat_group_name.strip()
            group_id = msg.group_id or group_name
            if not group_id:
                continue
            key = f"{group_id}\x1f{msg.fingerprint}"
            if key in self._seen_keys:
                continue
            self._seen_keys.add(key)
            events.append(
                MessageEvent(
                    account_id=account_id,
                    # Use the stable wxid as the memory/authorization scope;
                    # keep the display label separately for outbound UI work.
                    group_id=group_id,
                    group_name=group_name or group_id,
                    member_id=msg.sender_id or msg.sender,
                    member_name=msg.sender or msg.sender_id or "unknown",
                    text=msg.text,
                    observed_at=utc_now(),
                    source="weixin_ui_4.1",
                    external_message_id=msg.fingerprint,
                    is_self=msg.is_self,
                    image_paths=list(msg.image_paths or []),
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
