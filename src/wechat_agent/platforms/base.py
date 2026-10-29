from __future__ import annotations

from typing import Protocol

from .models import BrowseItem, PlatformAction, PlatformComment, PlatformName


class PlatformAdapter(Protocol):
    """Public platform surface shared by Douyin and Xiaohongshu adapters.

    Implementations may use Playwright, CDP, or another approved browser
    session. They must return identifiers read from the current page, never
    infer a target from a display name alone.
    """

    platform: PlatformName

    def login_status(self) -> dict[str, object]: ...

    def browse(self, query: str = "", limit: int = 20) -> list[BrowseItem]: ...

    def read_comments(self, item_id: str, limit: int = 50) -> list[PlatformComment]: ...

    def build_comment(self, item_id: str, text: str, *, dry_run: bool = True) -> PlatformAction: ...

    def build_reply(
        self,
        comment_id: str,
        text: str,
        *,
        dry_run: bool = True,
    ) -> PlatformAction: ...
