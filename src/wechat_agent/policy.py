from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from .config import Settings
from .models import MessageEvent, OutboundAction
from .storage import Storage


class ActionPolicy:
    """Guard every outbound action, including a reply to a group message."""

    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self._lock = Lock()
        self._recent: deque[datetime] = deque()
        self._circuit_threshold = 20  # per rolling minute; not a daily quota

    def is_stopped(self) -> bool:
        return self.settings.stop_file.exists()

    def is_allowed_group(self, group_name: str) -> bool:
        configured = self.settings.wechat_group_name.strip()
        return bool(configured) and group_name == configured

    def should_reply_to(self, event: MessageEvent) -> tuple[bool, str]:
        if not self.is_allowed_group(event.group_name):
            return False, "group_not_allowlisted"
        if self.settings.wechat_self_name and event.member_name == self.settings.wechat_self_name:
            return False, "self_message"
        if self.settings.wechat_allowed_members and event.member_name not in self.settings.wechat_allowed_members:
            return False, "member_not_allowlisted"
        aliases = [alias.casefold() for alias in self.settings.wechat_ai_aliases]
        mentioned = any(alias in event.text.casefold() for alias in aliases)
        if mentioned:
            return True, "ai_mentioned"
        if not self.settings.wechat_proactive_reply:
            return False, "proactive_disabled"
        # The first live phase uses a conservative relevance gate. The LLM can
        # later replace this with a classifier without changing the adapter.
        if "?" in event.text or "？" in event.text or any(
            token in event.text for token in ("谁", "怎么", "为什么", "求推荐", "有没有")
        ):
            return True, "proactive_relevance_gate"
        return False, "not_relevant_enough"

    def authorize(self, action: OutboundAction) -> tuple[bool, str]:
        if self.is_stopped():
            reason = "stop_file_present"
        elif not self.settings.wechat_allow_send:
            reason = "sending_disabled"
        elif not self.is_allowed_group(action.group_name):
            reason = "group_not_allowlisted"
        elif self.settings.wechat_allowed_members and action.target_member_id:
            reason = "target_member_policy_checked"
        else:
            reason = self._circuit_reason()

        allowed = reason in {"allowed", "target_member_policy_checked"}
        self.storage.audit_action(action, allowed=allowed, reason=reason)
        return allowed, reason

    def mark_sent(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            cutoff = now - timedelta(minutes=1)
            while self._recent and self._recent[0] < cutoff:
                self._recent.popleft()
            self._recent.append(now)

    def _circuit_reason(self) -> str:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        with self._lock:
            while self._recent and self._recent[0] < cutoff:
                self._recent.popleft()
            if len(self._recent) >= self._circuit_threshold:
                return "circuit_breaker_rate"
        return "allowed"
