from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Iterable

from .base import PlatformAdapter
from .models import ActionReceipt, BrowseItem, PlatformAction, PlatformComment, PlatformName
from ..storage import Storage


class PlatformService:
    """Registry, browser lifecycle, and guarded action planner."""

    def __init__(
        self,
        storage: Storage,
        adapters: Mapping[PlatformName, PlatformAdapter],
        *,
        mention_allowlists: Mapping[PlatformName, list[str] | tuple[str, ...]] | None = None,
    ):
        self.storage = storage
        self.adapters = dict(adapters)
        self._mention_allowlists = {
            platform: _parse_mention_allowlist((mention_allowlists or {}).get(platform, ()))
            for platform in self.adapters
        }

    def adapter(self, platform: PlatformName) -> PlatformAdapter:
        try:
            return self.adapters[platform]
        except KeyError as exc:
            raise ValueError(f"unsupported_platform:{platform}") from exc

    def login_status(self) -> dict[str, dict[str, object]]:
        statuses = {name: adapter.login_status() for name, adapter in self.adapters.items()}
        for name, status in statuses.items():
            targets = self._mention_allowlists.get(name, {})
            status["mention_allowlist_count"] = len(targets)
            status["mention_allowlist_ids"] = list(targets)
        return statuses

    def open(self, platform: PlatformName) -> dict[str, object]:
        adapter = self.adapter(platform)
        opener = getattr(adapter, "open", None)
        if not callable(opener):
            return adapter.login_status()
        return opener()

    def close(self, platform: PlatformName) -> dict[str, object]:
        adapter = self.adapter(platform)
        closer = getattr(adapter, "close", None)
        if callable(closer):
            closer()
        return adapter.login_status()

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
        mention_ids: Iterable[str] = (),
        dry_run: bool = True,
    ) -> ActionReceipt:
        mention_targets = self._validate_mentions(platform, text, mention_ids)
        action = self.adapter(platform).build_comment(item_id, text, dry_run=dry_run)
        if account_id and account_id != action.account_id:
            action = _with_account(action, account_id)
        action = _with_mention_metadata(action, mention_targets)
        return self._reserve(action)

    def plan_reply(
        self,
        platform: PlatformName,
        comment_id: str,
        text: str,
        *,
        account_id: str | None = None,
        mention_ids: Iterable[str] = (),
        dry_run: bool = True,
    ) -> ActionReceipt:
        mention_targets = self._validate_mentions(platform, text, mention_ids)
        action = self.adapter(platform).build_reply(comment_id, text, dry_run=dry_run)
        if account_id and account_id != action.account_id:
            action = _with_account(action, account_id)
        action = _with_mention_metadata(action, mention_targets)
        return self._reserve(action)

    def actions(self, limit: int = 50) -> list[dict[str, object]]:
        return self.storage.recent_platform_actions(limit)

    def _validate_mentions(
        self,
        platform: PlatformName,
        text: str,
        mention_ids: Iterable[str] | None,
    ) -> list[dict[str, str]]:
        """Allow only explicitly registered platform user IDs.

        Display names are deliberately not accepted as authorization input:
        they are not unique on either platform.  The browser adapter still
        has to select the platform's native mention suggestion during a real
        write.  This service-level check prevents an accidental or model-
        generated mention from reaching that adapter without an operator-
        approved stable ID.
        """
        has_mention_text = bool(_mention_names(text))
        ids = _normalize_mention_ids(mention_ids or ())
        if not has_mention_text and not ids:
            return []
        if has_mention_text and not ids:
            raise ValueError(f"{platform}_mention_target_ids_required")
        if ids and not has_mention_text:
            raise ValueError(f"{platform}_mention_text_missing_at")
        allowlist = self._mention_allowlists.get(platform, {})
        if not allowlist:
            raise ValueError(f"{platform}_mention_allowlist_empty")
        targets: list[dict[str, str]] = []
        for user_id in ids:
            if user_id not in allowlist:
                raise ValueError(f"{platform}_mention_target_not_allowlisted:{user_id}")
            targets.append({"user_id": user_id})
        return targets

    def execute(self, action_key: str, *, confirm: bool = False) -> dict[str, object]:
        """Execute one queued action after an explicit local confirmation.

        A dry-run record can never be upgraded into a real write.  Callers
        must first plan with ``dry_run=False`` (which creates a queued record),
        then call this method with ``confirm=True``.  The adapter itself still
        enforces the platform write switch, rate limit, challenge stop, and
        visible-page confirmation.
        """
        if not confirm:
            raise ValueError("platform_action_confirmation_required")
        record = self.storage.platform_action(action_key)
        if bool(record["dry_run"]):
            raise ValueError("dry_run_action_cannot_be_executed")
        if str(record["status"]) not in {"queued", "approved"}:
            raise ValueError(f"platform_action_not_queued:{record['status']}")
        adapter = self.adapter(str(record["platform"]))
        executor = getattr(adapter, "execute_action", None)
        if not callable(executor):
            raise ValueError("platform_browser_executor_unavailable")
        self.storage.update_platform_action(
            action_key,
            status="executing",
            reason="operator_confirmed",
        )
        try:
            result = executor(record)
        except Exception as exc:
            self.storage.update_platform_action(
                action_key,
                status="failed",
                reason=f"{type(exc).__name__}:{str(exc)[:400]}",
            )
            raise
        self.storage.update_platform_action(
            action_key,
            status="sent",
            reason="visible_page_confirmed",
            executed=True,
        )
        return {"action_key": action_key, "status": "sent", "result": result}

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
    mention_ids = ",".join(
        str(value)
        for value in action.metadata.get("mention_target_ids", [])
        if str(value).strip()
    )
    raw = "\x1f".join(
        [
            action.platform,
            action.account_id,
            action.action_type,
            action.target_id,
            action.parent_comment_id or "",
            normalized,
            mention_ids,
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


def _with_mention_metadata(
    action: PlatformAction,
    mention_targets: list[dict[str, str]],
) -> PlatformAction:
    if not mention_targets:
        return action
    metadata = dict(action.metadata)
    metadata["mention_target_ids"] = [target["user_id"] for target in mention_targets]
    metadata["mention_allowlist_checked"] = True
    return PlatformAction(
        platform=action.platform,
        account_id=action.account_id,
        action_type=action.action_type,
        target_id=action.target_id,
        text=action.text,
        parent_comment_id=action.parent_comment_id,
        target_url=action.target_url,
        dry_run=action.dry_run,
        metadata=metadata,
    )


def _parse_mention_allowlist(entries: list[str] | tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Parse a platform allowlist keyed only by stable platform user ID."""
    parsed: dict[str, dict[str, str]] = {}
    for raw in entries:
        value = str(raw).strip()
        if not value:
            continue
        if "|" in value or any(char.isspace() for char in value):
            raise ValueError("platform_mention_allowlist_entry_must_be_user_id_only")
        parsed[value] = {"user_id": value}
    return parsed


_MENTION_RE = re.compile(r"@([^\s@,，。！？!?；;：:#（）()\[\]{}]+)")


def _mention_names(text: str) -> list[str]:
    return [match.group(1).strip().rstrip(".,，。！？!?；;：:") for match in _MENTION_RE.finditer(text)]


def _normalize_mention_ids(values: Iterable[str]) -> list[str]:
    """Normalize caller-provided IDs while preserving their configured order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        user_id = str(value).strip()
        if user_id and user_id not in seen:
            normalized.append(user_id)
            seen.add(user_id)
    return normalized
