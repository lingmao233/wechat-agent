from __future__ import annotations

"""Canonical chat-text normalization.

The WeChat database adapter can expose a row as ``wxid_xxx:\\n<message>`` and
may embed transport XML (``<msg><emoji .../></msg>``, ``<appmsg>``,
``<refermsg>``).  Those wrappers are storage details, not what a member said.
Every profile, memory and prompt path must consume the same normalized view so
that "average length", "common vocabulary" and "group style" mean the same
thing everywhere.

The original text remains in ``messages.text`` for audit and idempotency.  This
module only produces a derived view; it never rewrites the stored row.
"""

import re
import unicodedata
from dataclasses import dataclass
from html import unescape

__all__ = ["NormalizedMessage", "normalize_chat_text", "normalizer_version"]

normalizer_version = 2

_SENDER_ID_PREFIX = re.compile(r"^\s*(?P<sender>wxid_[A-Za-z0-9_-]{4,64})\s*[:：]\s*\n?")
_TIME_ONLY = re.compile(r"^\s*\d{1,2}\s*[:：]\s*\d{1,2}\s*$")
_TAG = re.compile(r"<[^>]{1,2000}>", re.DOTALL)
_XML_HINT = re.compile(
    r"<(?:msg|appmsg|emoji|sysmsg|img|voicemsg|voipmsg|videomsg|location|refermsg)\b",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(
    r"\[(?P<kind>表情|图片|语音|视频|文件|分享|引用|链接|小程序|位置|系统消息|红包|转账|通话)"
    r"(?::(?P<detail>[^\]\n]{0,60}))?\]"
)
_SYSTEM_HINTS = (
    "查看更多消息",
    "以下为新消息",
    "撤回了一条消息",
    "加入了群聊",
    "邀请",
    "修改了群名",
    "拍了拍",
    "开启了朋友验证",
    "你已添加",
    "该消息已过期",
    "消息已发出，但被对方拒收",
    "提到了你",
    "对方正在输入",
)
_MEDIA_ONLY: dict[str, tuple[str, str, int, int]] = {
    "图片": ("[图片]", "image", 0, 1),
    "[图片]": ("[图片]", "image", 0, 1),
    "动画表情": ("[表情]", "emoji", 1, 0),
    "[动画表情]": ("[表情]", "emoji", 1, 0),
    "[表情]": ("[表情]", "emoji", 1, 0),
    "[视频]": ("[视频]", "video", 0, 0),
    "[语音]": ("[语音]", "voice", 0, 0),
    "[文件]": ("[文件]", "file", 0, 0),
    "[链接]": ("[链接]", "link", 0, 0),
    "[位置]": ("[位置]", "location", 0, 0),
}
_EMPTY_PLACEHOLDERS = {"表情", "图片", "语音", "视频", "文件", "链接", "小程序", "位置", "系统消息", "红包", "转账", "通话"}

# A preference value such as "喜欢你" is usually addressed to the bot, not a
# durable fact about the member.  Keep the guard local and explicit.
_SELF_REFERENTIAL_VALUES = {"你", "您", "他", "她", "它", "这个", "那个", "这些", "那些", "你们", "他们"}


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """Derived, comparable view of one chat row."""

    text: str
    visible_text: str
    kind: str = "text"
    prefix_sender: str = ""
    addressed_as: str = ""
    quoted_text: str = ""
    is_system: bool = False
    sticker_count: int = 0
    image_count: int = 0
    truncated: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.visible_text.strip())


def normalize_chat_text(
    raw: object,
    *,
    sender_id: str = "",
    sender_name: str = "",
    ai_aliases: tuple[str, ...] | list[str] = (),
    max_chars: int = 1200,
) -> NormalizedMessage:
    """Return the canonical view of one WeChat message.

    ``sender_id``/``sender_name`` are only used to remove *known* transport
    prefixes.  An arbitrary ``名称：`` prefix is deliberately preserved because
    it may be part of the message body.
    """

    value = unicodedata.normalize("NFC", str(raw or ""))
    value = unescape(value)
    value = (
        value.replace("\u2005", " ")
        .replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("＠", "@")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    value = value.strip()
    if not value:
        return NormalizedMessage(text="", visible_text="", kind="empty", is_system=True)

    prefix_sender = ""
    value, prefix_sender = _strip_known_prefix(value, sender_id, sender_name)

    if _TIME_ONLY.match(value) or any(hint in value for hint in _SYSTEM_HINTS):
        return NormalizedMessage(
            text="",
            visible_text="",
            kind="system",
            prefix_sender=prefix_sender,
            is_system=True,
        )

    media = _MEDIA_ONLY.get(value)
    if media is not None:
        placeholder, media_kind, stickers, images = media
        return NormalizedMessage(
            text=placeholder,
            visible_text="",
            kind=media_kind,
            prefix_sender=prefix_sender,
            sticker_count=stickers,
            image_count=images,
        )

    kind = "text"
    quoted_text = ""
    sticker_count = 0
    image_count = 0
    if _XML_HINT.search(value):
        mapped, kind, quoted_text, visible_hint = _map_xml(value)
        value = mapped
        if kind == "emoji":
            sticker_count = 1
        elif kind == "image":
            image_count = 1

    addressed_as = ""
    value, addressed_as = _normalize_addressing(value, ai_aliases)

    value = _collapse(value)
    if not value:
        return NormalizedMessage(
            text="",
            visible_text="",
            kind=kind if kind != "text" else "empty",
            prefix_sender=prefix_sender,
            addressed_as=addressed_as,
            quoted_text=quoted_text,
            is_system=kind in {"system", "empty"},
            sticker_count=sticker_count,
            image_count=image_count,
        )

    truncated = len(value) > max_chars
    if truncated:
        value = value[:max_chars].rstrip()

    visible = _visible_text(value)
    if kind in _EMPTY_PLACEHOLDERS and not visible:
        visible = ""
    visible = _collapse(visible)
    return NormalizedMessage(
        text=value,
        visible_text=visible,
        kind=kind,
        prefix_sender=prefix_sender,
        addressed_as=addressed_as,
        quoted_text=quoted_text,
        is_system=False,
        sticker_count=sticker_count,
        image_count=image_count,
        truncated=truncated,
    )


def _strip_known_prefix(value: str, sender_id: str, sender_name: str) -> tuple[str, str]:
    match = _SENDER_ID_PREFIX.match(value)
    if match:
        return value[match.end():].lstrip("\n"), match.group("sender")
    for candidate in (sender_id.strip(), sender_name.strip()):
        if not candidate or candidate.casefold() in {"unknown", "none", "null"}:
            continue
        pattern = re.compile(rf"^\s*{re.escape(candidate)}\s*[:：]\s*\n?")
        match = pattern.match(value)
        if match:
            return value[match.end():].lstrip("\n"), candidate
    return value, ""


def _map_xml(value: str) -> tuple[str, str, str, str]:
    """Map one WeChat XML payload to a bounded placeholder.

    Returns ``(text, kind, quoted_text, visible_hint)``.  ``visible_hint`` is
    kept for share/file payloads so a title can still inform topics without
    counting the XML wrapper as a long message.
    """

    lowered = value.lower()
    if "<sysmsg" in lowered:
        return "[系统消息]", "system", "", ""
    if "<emoji" in lowered:
        return "[表情]", "emoji", "", ""
    if "<img" in lowered:
        return "[图片]", "image", "", ""
    if "<voicemsg" in lowered or "<voipmsg" in lowered:
        return "[语音]", "voice", "", ""
    if "<videomsg" in lowered:
        return "[视频]", "video", "", ""
    if "<location" in lowered:
        return "[位置]", "location", "", ""

    title = _tag_text(value, "title")
    refer = _tag_text(value, "refermsg")
    quoted = ""
    if refer:
        quoted = _tag_text(refer, "content") or _tag_text(refer, "title") or refer
        quoted = _clean_short(quoted, 60)
    if quoted:
        # For a quoted reply WeChat puts the text the member just typed in the
        # outer <title> and the referenced message inside <refermsg>.  Keep both:
        # dropping the title loses "@Away …" and makes the bot think nobody
        # addressed it, even though the member explicitly replied to it.
        typed = _clean_short(title, 80)
        prefix = f"{typed} " if typed else ""
        return f"{prefix}[引用:{quoted}]", "quote", quoted, ""

    app_type = ""
    type_match = re.search(r"<type>\s*(\d{1,3})\s*</type>", value)
    if type_match:
        app_type = type_match.group(1)
    if app_type == "6" and title:
        return f"[文件:{_clean_short(title, 40)}]", "file", "", _clean_short(title, 40)
    if app_type in {"5", "4"} and title:
        return f"[链接:{_clean_short(title, 40)}]", "link", "", _clean_short(title, 40)
    if title:
        return f"[分享:{_clean_short(title, 40)}]", "appmsg", "", _clean_short(title, 40)
    if "<appmsg" in lowered:
        return "[分享]", "appmsg", "", ""
    # Unknown XML: keep any human-readable text so a quoted reply is not lost.
    stripped = _collapse(_TAG.sub(" ", value))
    if stripped:
        return stripped, "text", "", ""
    return "", "empty", "", ""


def _tag_text(value: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}\b[^>]*>(.*?)</{tag}>",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return unescape(match.group(1)) if match else ""


def _normalize_addressing(value: str, aliases: tuple[str, ...] | list[str]) -> tuple[str, str]:
    addressed_as = ""
    ordered = sorted({str(alias).strip() for alias in aliases if str(alias).strip()}, key=len, reverse=True)
    for alias in ordered:
        pattern = re.compile(
            rf"@\s*{re.escape(alias)}(?=[\s，。！？!?,.;:~～]|$)",
            flags=re.IGNORECASE,
        )
        if not pattern.search(value):
            continue
        if not addressed_as:
            addressed_as = alias
        value = pattern.sub("@AI", value)
    return value, addressed_as


def _visible_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = match.group("kind")
        detail = match.group("detail") or ""
        if kind in {"分享", "文件", "链接"}:
            return detail
        return " "

    return _PLACEHOLDER.sub(replace, value)


def _collapse(value: str) -> str:
    return " ".join(str(value or "").split())


def _clean_short(value: str, limit: int) -> str:
    return _collapse(_TAG.sub(" ", unescape(value)))[:limit].strip(" ，,。.!！？?；;:：")


def is_self_referential_value(value: str) -> bool:
    """True when an extracted preference is about the listener, not the member."""

    return _collapse(value).strip(" ，,。.!！？?；;:：") in _SELF_REFERENTIAL_VALUES
