from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from threading import Lock
import unicodedata

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
        if event.is_self:
            return False, "self_message"
        if self.settings.wechat_allowed_members and not self._member_is_allowed(event):
            return False, "member_not_allowlisted"
        trigger_reason = self._trigger_reason(event.text)
        if trigger_reason:
            return True, trigger_reason
        # The operator explicitly requested addressed-only behavior.  A plain
        # question is still a group message and must not invoke the model.
        return False, "ai_not_mentioned"

    def _member_is_allowed(self, event: MessageEvent) -> bool:
        """Check the Weixin unique ID; display names are not authorization keys."""

        allowed = {
            str(value).strip()
            for value in self.settings.wechat_allowed_members
            if str(value).strip()
        }
        return bool(event.member_id.strip()) and event.member_id.strip() in allowed

    def _mentions_ai(self, text: str) -> bool:
        return self._trigger_reason(text) is not None

    def _trigger_reason(self, text: str) -> str | None:
        """Return the trigger class for a message.

        ``wechat_ai_aliases`` are addressing names.  They match an explicit
        ``@name`` anywhere in the message or a name at the beginning.  They
        deliberately do not match a name in the middle of ordinary prose, so
        the bot's own sentence such as ``我是 AI`` cannot re-enter the loop.
        ``wechat_keywords`` are the separate free-form keyword trigger and may
        occur anywhere, matching the behaviour of the previously working bot.
        """

        value = self._normalize_trigger_text(text)
        if not value:
            return None
        folded = value.casefold()
        aliases = [*self.settings.wechat_ai_aliases]
        # The logged-in nickname is also an address by default, matching the
        # previously working bot's automatic self-name registration.
        if self.settings.wechat_self_name.strip():
            aliases.append(self.settings.wechat_self_name)
        for raw_alias in aliases:
            alias = self._normalize_trigger_text(raw_alias)
            if not alias:
                continue
            escaped = re.escape(alias)
            # WeChat may insert a special-width space after the mention.  It
            # is normalized above, and punctuation/end-of-message closes the
            # name so @AI助手 does not accidentally count as @AI.
            mention_pattern = (
                rf"(?<![\w@])@\s*{escaped}"
                rf"(?=$|[\s,，。！？!?：:；;、()（）\[\]【】<>《》])"
            )
            if re.search(mention_pattern, value, flags=re.IGNORECASE):
                return "ai_mentioned"
            # A name without @ is accepted only as the message prefix.  This
            # preserves the old bot's ``AI 你好`` form while rejecting
            # ``我是 AI`` in an assistant response.
            if folded.startswith(alias.casefold()):
                return "ai_mentioned"

        for raw_keyword in self.settings.wechat_keywords:
            keyword = self._normalize_trigger_text(raw_keyword)
            if keyword and keyword.casefold() in folded:
                return "wechat_keyword"
        return None

    @staticmethod
    def _normalize_trigger_text(text: object) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        # Remove zero-width formatting characters, then collapse all Unicode
        # whitespace.  This covers the U+2005 spacing emitted by some WeChat
        # UIA builds after an @ mention.
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cf"})
        return " ".join(value.split()).strip()

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
