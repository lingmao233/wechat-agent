from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from .base import PlatformAdapter
from .models import ActionReceipt, BrowseItem, PlatformAction, PlatformComment, PlatformName
from ..storage import Storage


class PlatformService:
    """Registry and dry-run action planner for public platform adapters."""

    def __init__(self, storage: Storage, adapters: Mapping[PlatformName, PlatformAdapter]):
        self.storage = storage
        self.adapters = dict(adapters)

    def adapter(self, platform: PlatformName) -> PlatformAdapter:
        try:
            return self.adapters[platform]
        except KeyError as exc:
            raise ValueError(f"unsupported_platform:{platform}") from exc

    def login_status(self) -> dict[str, dict[str, object]]:
        return {name: adapter.login_status() for name, adapter in self.adapters.items()}

    def browse(self, platform: PlatformName, query: str = "", limit: int = 20) -> list[BrowseItem]:
        return self.adapter(platform).browse(query=query, limit=limit)

    def read_comments(self, platform: PlatformName, item_id: str, limit: int = 50) -> list[PlatformComment]:
        return self.adapter(platform).read_comments(item_id=item_id, limit=limit)

    def plan_comment(
        self,
        platform: PlatformName,
        item_id: str,
        text: str,
        *,
        account_id: str | None = None,
        dry_run: bool = True,
    ) -> ActionReceipt:
        action = self.adapter(platform).build_comment(item_id, text, dry_run=dry_run)
        if account_id and account_id != action.account_id:
            action = _with_account(action, account_id)
        return self._reserve(action)

    def plan_reply(
        self,
        platform: PlatformName,
        comment_id: str,
        text: str,
        *,
        account_id: str | None = None,
        dry_run: bool = True,
    ) -> ActionReceipt:
        action = self.adapter(platform).build_reply(comment_id, text, dry_run=dry_run)
        if account_id and account_id != action.account_id:
            action = _with_account(action, account_id)
        return self._reserve(action)

    def actions(self, limit: int = 50) -> list[dict[str, object]]:
        return self.storage.recent_platform_actions(limit)

    def _reserve(self, action: PlatformAction) -> ActionReceipt:
        action_key = action_key_for(action)
        record, duplicate = self.storage.reserve_platform_action(
            action_key=action_key,
            platform=action.platform,
            account_id=action.account_id,
            action_type=action.action_type,
            target_id=action.target_id,
            parent_comment_id=action.parent_comment_id,
            text=action.text,
            target_url=action.target_url,
            dry_run=action.dry_run,
            metadata=action.metadata,
        )
        return ActionReceipt(
            action_key=action_key,
            status=str(record["status"]),
            reason=str(record["reason"]),
            duplicate=duplicate,
            dry_run=bool(record["dry_run"]),
        )


def action_key_for(action: PlatformAction) -> str:
    normalized = re.sub(r"\s+", " ", action.text.strip()).casefold()
    raw = "\x1f".join(
        [
            action.platform,
            action.account_id,
            action.action_type,
            action.target_id,
            action.parent_comment_id or "",
            normalized,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _with_account(action: PlatformAction, account_id: str) -> PlatformAction:
    return PlatformAction(
        platform=action.platform,
        account_id=account_id,
        action_type=action.action_type,
        target_id=action.target_id,
        text=action.text,
        parent_comment_id=action.parent_comment_id,
        target_url=action.target_url,
        dry_run=action.dry_run,
        metadata=action.metadata,
    )
