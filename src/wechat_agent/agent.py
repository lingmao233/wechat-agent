from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .config import Settings
from .llm import LLMClient
from .models import MemoryFact, MessageEvent, OutboundAction
from .policy import ActionPolicy
from .storage import Storage
from .weixin_ui import WeixinUiAdapter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentStats:
    seen: int = 0
    replied: int = 0
    skipped: int = 0
    duplicate_questions: int = 0
    failed: int = 0


class WeChatAgent:
    def __init__(self, settings: Settings, *, ui=None, llm=None, storage=None, policy=None):
        self.settings = settings
        self.storage = storage or Storage(settings)
        self.storage.init_schema()
        self.ui = ui or WeixinUiAdapter(settings)
        self.llm = llm or LLMClient(settings)
        self.policy = policy or ActionPolicy(settings, self.storage)
        self.stats = AgentStats()

    def ready(self) -> dict[str, str]:
        """Readiness check only. It never opens a conversation or sends text."""
        return self.ui.check_ready()

    def run(self) -> None:
        self.ui.connect()
        if not self.settings.wechat_group_name:
            raise RuntimeError("请在 .env 设置 WECHAT_GROUP_NAME 后再启动真实微信监听")
        log.info("Listening to configured test group: %s", self.settings.wechat_group_name)
        while not self.policy.is_stopped():
            try:
                for event in self.ui.poll(account_id=self._account_id()):
                    self.handle_event(event)
                self.storage.purge_expired_messages()
            except KeyboardInterrupt:
                raise
            except Exception:
                self.stats.failed += 1
                log.exception("poll cycle failed")
            time.sleep(self.settings.wechat_poll_seconds)
        log.warning("STOP file detected; listener stopped")

    def handle_event(self, event: MessageEvent) -> bool:
        self.stats.seen += 1
        event_id = self.storage.record_message(event)
        question_hash = self.storage.question_hash(event, self.settings.wechat_ai_aliases)
        claim = self.storage.claim_processing(
            event,
            question_hash,
            lease_seconds=self.settings.wechat_processing_lease_seconds,
        )
        if not claim.claimed and not claim.is_new_event:
            self.stats.skipped += 1
            if claim.reason == "question_already_answered":
                self.stats.duplicate_questions += 1
            log.debug("skip %s: %s", event.member_name, claim.reason)
            return False

        # Keep raw messages for audit/retention.  AI-authored rows are not
        # member facts, otherwise the bot can recall its own answer as user
        # context on a later turn.
        if claim.claimed and not self._is_self_event(event):
            self.storage.add_fact(
                MemoryFact(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    member_id=event.member_id,
                    memory_type="chat",
                    content=event.text,
                    confidence=1.0,
                    status="confirmed",
                    source_event_id=str(event_id) if event_id else event.external_message_id,
                    observed_at=event.observed_at,
                    metadata={"member_name": event.member_name, "source": event.source},
                )
            )
            self._extract_interest(event, event_id)

        if not claim.claimed:
            self.stats.skipped += 1
            if claim.reason == "question_already_answered":
                self.stats.duplicate_questions += 1
            log.debug("skip %s: %s", event.member_name, claim.reason)
            return False

        should_reply, reason = self.policy.should_reply_to(event)
        if not should_reply:
            self.storage.finish_processing(claim.event_key, "skipped", reason)
            self.stats.skipped += 1
            log.debug("skip %s: %s", event.member_name, reason)
            return False

        memories = self.storage.search_facts(
            account_id=event.account_id,
            group_id=event.group_id,
            member_id=event.member_id,
            query=event.text,
            half_life_days=self.settings.half_life_for("chat"),
        )
        reply_text: str | None = None
        try:
            reply = self.llm.reply(event.text, event.member_name, event.group_name, memories)
            reply_text = reply.text
            action = OutboundAction(
                account_id=event.account_id,
                group_id=event.group_id,
                group_name=event.group_name,
                text=reply.text,
                action_type="group_reply",
                target_member_id=event.member_id,
            )
            allowed, auth_reason = self.policy.authorize(action)
            if not allowed:
                self.storage.finish_processing(claim.event_key, "skipped", auth_reason, reply_text)
                log.info("reply blocked (%s): %s", auth_reason, reply.text[:80])
                self.stats.skipped += 1
                return False
            # A record becomes ``sent`` only after the UI adapter returns.  A
            # model response or a dry-run audit entry alone is not an answer.
            self.ui.send(action)
            self.policy.mark_sent()
            self.storage.finish_processing(claim.event_key, "sent", "sent", reply_text)
            self.stats.replied += 1
            return True
        except Exception as exc:
            self.storage.finish_processing(
                claim.event_key,
                "failed",
                f"{type(exc).__name__}: {str(exc)[:240]}",
                reply_text,
            )
            self.stats.failed += 1
            raise

    def _is_self_event(self, event: MessageEvent) -> bool:
        if event.is_self:
            return True
        configured = self.settings.wechat_self_name.strip()
        return bool(configured and event.member_name.casefold() == configured.casefold())

    def _extract_interest(self, event: MessageEvent, event_id: int | None) -> None:
        patterns = (
            r"我(?:比较)?喜欢([^，。！？!?]{1,40})",
            r"我(?:最近)?在关注([^，。！？!?]{1,40})",
            r"我爱([^，。！？!?]{1,40})",
        )
        for pattern in patterns:
            match = re.search(pattern, event.text)
            if not match:
                continue
            content = match.group(1).strip()
            self.storage.add_fact(
                MemoryFact(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    member_id=event.member_id,
                    memory_type="interest",
                    content=content,
                    confidence=0.75,
                    status="inferred",
                    source_event_id=str(event_id) if event_id else event.external_message_id,
                    observed_at=event.observed_at,
                    metadata={"extractor": "pattern_v1", "member_name": event.member_name},
                )
            )

    @staticmethod
    def _account_id() -> str:
        return "wechat-test-account"
