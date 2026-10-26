from __future__ import annotations

from .models import BrowseItem, PlatformAction, PlatformComment, PlatformName


class DryRunPlatformAdapter:
    """Fixture-backed adapter for offline planning and UI integration tests.

    It deliberately has no browser or network dependency. A real adapter can
    replace it while keeping ``PlatformService`` and the action ledger intact.
    """

    def __init__(
        self,
        platform: PlatformName,
        *,
        items: list[BrowseItem] | None = None,
        comments: list[PlatformComment] | None = None,
        account_id: str = "unbound",
    ):
        self.platform = platform
        self.account_id = account_id
        self._items = {item.item_id: item for item in items or []}
        self._comments = {comment.comment_id: comment for comment in comments or []}

    def login_status(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "logged_in": False,
            "mode": "dry_run",
            "account_id": self.account_id,
        }

    def browse(self, query: str = "", limit: int = 20) -> list[BrowseItem]:
        needle = query.casefold().strip()
        values = list(self._items.values())
        if needle:
            values = [
                item
                for item in values
                if needle in f"{item.title} {item.text} {item.author_name or ''}".casefold()
            ]
        return values[: max(1, min(limit, 100))]

    def read_comments(self, item_id: str, limit: int = 50) -> list[PlatformComment]:
        values = [comment for comment in self._comments.values() if comment.item_id == item_id]
        return values[: max(1, min(limit, 200))]

    def build_comment(self, item_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        self._require_item(item_id)
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="comment",
            target_id=item_id,
            text=_validate_text(text),
            target_url=self._items[item_id].url,
            dry_run=dry_run,
        )

    def build_reply(self, comment_id: str, text: str, *, dry_run: bool = True) -> PlatformAction:
        comment = self._comments.get(comment_id)
        if not comment:
            raise ValueError(f"comment_not_found:{comment_id}")
        return PlatformAction(
            platform=self.platform,
            account_id=self.account_id,
            action_type="reply",
            target_id=comment_id,
            parent_comment_id=comment_id,
            text=_validate_text(text),
            target_url=self._items[comment.item_id].url if comment.item_id in self._items else None,
            dry_run=dry_run,
        )

    def _require_item(self, item_id: str) -> None:
        if item_id not in self._items:
            raise ValueError(f"item_not_found:{item_id}")


def _validate_text(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("action_text_empty")
    if len(value) > 2000:
        raise ValueError("action_text_too_long")
    return value
