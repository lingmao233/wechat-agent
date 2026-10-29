from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class QuotedMessage:
    """A message that the channel explicitly marks as being quoted/replied to."""

    text: str
    sender_name: str = ""
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One bounded message from the current conversation's recent context."""

    sender_name: str
    text: str
    observed_at: datetime | None = None
    sender_id: str = ""
    message_id: str = ""
    thread_id: str = ""
    task_id: str = ""
    # Local, already-decrypted image files associated with this message. The
    # paths are kept out of the prompt text and are converted to bounded data
    # URLs only by the vision-capable LLM client.
    image_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MessageEvent:
    account_id: str
    group_id: str
    group_name: str
    member_id: str
    member_name: str
    text: str
    observed_at: datetime = field(default_factory=utc_now)
    source: str = "weixin_ui"
    external_message_id: str | None = None
    is_self: bool = False
    reply_to: QuotedMessage | None = None
    # The UI adapter may not expose these fields yet, but keeping them on the
    # event lets API/integration adapters carry a stable thread or task scope
    # without changing the existing positional constructor contract.
    thread_id: str | None = None
    task_id: str | None = None
    # The WeChat DB adapter fills this when a message row is an image and the
    # local attachment can be decrypted. Keep it optional so API/test adapters
    # can continue constructing text-only events positionally.
    image_paths: list[str] = field(default_factory=list)

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return self.account_id, self.group_id, self.member_id


@dataclass(slots=True)
class MemoryFact:
    account_id: str
    group_id: str
    member_id: str
    memory_type: str
    content: str
    confidence: float = 0.5
    status: str = "candidate"
    source_event_id: str | None = None
    observed_at: datetime = field(default_factory=utc_now)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Stable memory identity and audit fields.  They are optional so existing
    # callers/tests that construct the original six-field dataclass continue
    # to work unchanged.
    memory_id: str | None = None
    task_id: str | None = None
    source_message_ids: list[str] = field(default_factory=list)
    importance: float = 0.5
    embedding_model: str = ""
    updated_at: datetime | None = None

    @property
    def scope(self) -> dict[str, str]:
        """Return the explicit stable scope carried by this memory."""

        scope = {
            "account_id": self.account_id,
            "group_id": self.group_id,
        }
        if self.member_id:
            scope["member_id"] = self.member_id
        if self.task_id:
            scope["task_id"] = self.task_id
        return scope


@dataclass(slots=True)
class OutboundAction:
    account_id: str
    group_id: str
    group_name: str
    text: str
    at_names: list[str] = field(default_factory=list)
    action_type: Literal["group_reply", "proactive_at", "comment", "dm"] = "group_reply"
    target_member_id: str | None = None
    # Local media paths are sent as native Weixin attachments.  Keeping them
    # separate from ``text`` prevents generated image URLs from being pasted
    # into the chat as plain text.
    media_paths: list[str] = field(default_factory=list)

