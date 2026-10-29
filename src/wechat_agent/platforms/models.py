from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

PlatformName = Literal["douyin", "xiaohongshu"]
PlatformActionType = Literal["comment", "reply"]


@dataclass(frozen=True, slots=True)
class BrowseItem:
    platform: PlatformName
    item_id: str
    url: str
    title: str = ""
    author_id: str | None = None
    author_name: str | None = None
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformComment:
    platform: PlatformName
    comment_id: str
    item_id: str
    author_id: str | None
    author_name: str
    text: str
    parent_comment_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlatformAction:
    platform: PlatformName
    account_id: str
    action_type: PlatformActionType
    target_id: str
    text: str
    parent_comment_id: str | None = None
    target_url: str | None = None
    dry_run: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action_key: str
    status: str
    reason: str
    duplicate: bool
    dry_run: bool
