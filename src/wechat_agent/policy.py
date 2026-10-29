from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
from threading import Lock
import unicodedata

from .config import Settings
from .models import MessageEvent, OutboundAction
from .storage import Storage
from .textnorm import normalize_chat_text


class ActionPolicy:
    """Guard every outbound action, including a reply to a group message."""

    def __init__(self, settings: Settings, storage: Storage):
        self.settings = settings
        self.storage = storage
        self._lock = Lock()
        # Keep a bounded in-process send history for the minute circuit,
        # operator-configured cooldown, and optional hourly cap.  It is reset
        # when the listener restarts; the durable reply ledger still handles
        # question idempotency across restarts.
        self._sent_times: deque[datetime] = deque()
        self._last_sent_at: datetime | None = None
        self._circuit_threshold = 20  # per rolling minute; not a daily quota

    def is_stopped(self) -> bool:
        return self.settings.stop_file.exists()

    def is_allowed_group(self, group_name: str) -> bool:
        configured = getattr(self.settings, "wechat_group_names", None) or [
            self.settings.wechat_group_name
        ]
        wanted = {str(value).strip().casefold() for value in configured if str(value).strip()}
        return bool(wanted) and str(group_name or "").strip().casefold() in wanted

    def should_reply_to(self, event: MessageEvent) -> tuple[bool, str]:
        if not self.is_allowed_group(event.group_name):
            return False, "group_not_allowlisted"
        if event.is_self:
            return False, "self_message"
        if self.settings.wechat_allowed_members and not self._member_is_allowed(event):
            return False, "member_not_allowlisted"
        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if normalized.is_system:
            return False, "system_message"
        trigger_reason = self._trigger_reason(normalized.text)
        if trigger_reason:
            frequency_reason = self._frequency_reason()
            if frequency_reason:
                return False, frequency_reason
            return True, trigger_reason
        proactive_reason = self._proactive_reason(normalized.text)
        if proactive_reason and self.settings.wechat_proactive_reply:
            frequency_reason = self._frequency_reason()
            if frequency_reason:
                return False, frequency_reason
            guard = self._proactive_guard_reason()
            if guard:
                return False, guard
            if not self._proactive_roll(event):
                return False, "proactive_probability"
            return True, f"proactive_{proactive_reason}"
        # Default addressed-only behavior: when proactive speaking is off, a
        # plain question is still a group message and must not invoke the model.
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
        elif (frequency_reason := self._frequency_reason()):
            reason = frequency_reason
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
            self._prune_sent_times(now)
            self._sent_times.append(now)
            self._last_sent_at = now

    def _frequency_reason(self) -> str | None:
        """Return a configured frequency guard reason, if one is active."""

        cooldown = max(0.0, float(self.settings.wechat_reply_cooldown_seconds))
        hourly_limit = max(0, int(self.settings.wechat_max_replies_per_hour))
        if cooldown <= 0 and hourly_limit <= 0:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_sent_times(now)
            if cooldown > 0 and self._last_sent_at is not None:
                elapsed = (now - self._last_sent_at).total_seconds()
                if elapsed < cooldown:
                    return "reply_cooldown_active"
            if hourly_limit > 0 and len(self._sent_times) >= hourly_limit:
                return "reply_hourly_limit"
        return None

    def _proactive_reason(self, text: str) -> str | None:
        """Return a bounded social cue that may justify joining unprompted."""

        value = " ".join(str(text or "").split()).strip()
        if not value or value.startswith("[") or value.endswith("]"):
            return None
        # Do not barge into a message with any explicit @ target.  If it was
        # addressed to the bot, the trigger branch above already handled it.
        if "@" in value:
            return None
        if any(
            cue in value
            for cue in (
                "好累",
                "累死",
                "难受",
                "烦死",
                "不开心",
                "崩溃",
                "焦虑",
                "压力大",
                "委屈",
                "失眠",
                "想哭",
            )
        ):
            return "comfort"
        if any(cue in value for cue in ("骗你的", "逗你的", "上钩", "钓鱼", "不告诉你", "假的吧")):
            return "prank"
        if any(cue in value for cue in ("哈哈", "笑死", "绷不住", "离谱", "整活", "梗", "乐死")):
            return "joke"
        if value.endswith(("?", "？")) or value.startswith(
            ("有没有", "谁能", "求推荐", "怎么", "为什么", "为啥")
        ):
            return "question"
        return None

    def _proactive_guard_reason(self) -> str | None:
        """Apply proactive-only cooldown and hourly cap."""

        interval = max(0.0, float(self.settings.wechat_proactive_min_interval_seconds))
        hourly_limit = max(0, int(self.settings.wechat_proactive_max_per_hour))
        if interval <= 0 and hourly_limit <= 0:
            return None
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_sent_times(now)
            if interval > 0 and self._last_sent_at is not None:
                elapsed = (now - self._last_sent_at).total_seconds()
                if elapsed < interval:
                    return "proactive_cooldown"
            if hourly_limit > 0 and len(self._sent_times) >= hourly_limit:
                return "proactive_hourly_limit"
        return None

    def _proactive_roll(self, event: MessageEvent) -> bool:
        """Stable per-message random gate; retries do not change the outcome."""

        probability = max(0.0, min(1.0, float(self.settings.wechat_proactive_probability)))
        if probability >= 1.0:
            return True
        if probability <= 0.0:
            return False
        seed = ":".join(
            (
                event.account_id,
                event.group_id,
                event.member_id,
                event.external_message_id or "",
                event.text,
            )
        )
        roll = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        return roll < probability

    def _prune_sent_times(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=1)
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()

    def _circuit_reason(self) -> str:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        with self._lock:
            self._prune_sent_times(now)
            recent_count = sum(item >= cutoff for item in self._sent_times)
            if recent_count >= self._circuit_threshold:
                return "circuit_breaker_rate"
        return "allowed"
