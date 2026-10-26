from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return self.account_id, self.group_id, self.member_id


@dataclass(slots=True)
class MemoryFact:
    account_id: str
    group_id: str
    member_id: str
    memory_type: Literal["chat", "interest", "fact"]
    content: str
    confidence: float = 0.5
    status: Literal["confirmed", "inferred", "superseded", "deleted"] = "inferred"
    source_event_id: str | None = None
    observed_at: datetime = field(default_factory=utc_now)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundAction:
    account_id: str
    group_id: str
    group_name: str
    text: str
    at_names: list[str] = field(default_factory=list)
    action_type: Literal["group_reply", "proactive_at", "comment", "dm"] = "group_reply"
    target_member_id: str | None = None

