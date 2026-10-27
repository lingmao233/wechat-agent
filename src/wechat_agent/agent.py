from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass

from .config import Settings
from .image import ImageClient
from .llm import LLMClient
from .models import MemoryFact, MessageEvent, OutboundAction
from .policy import ActionPolicy
from .storage import Storage
from .web_research import WebResearchClient
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
    def __init__(
        self,
        settings: Settings,
        *,
        ui=None,
        llm=None,
        storage=None,
        policy=None,
        platform_service=None,
        web_research=None,
        image_client=None,
    ):
        self.settings = settings
        self.storage = storage or Storage(settings)
        self.storage.init_schema()
        self.ui = ui or WeixinUiAdapter(settings)
        self.llm = llm or LLMClient(settings)
        self.policy = policy or ActionPolicy(settings, self.storage)
        self.web_research = web_research or WebResearchClient(
            settings,
            platform_service=platform_service,
        )
        self.image_client = image_client or ImageClient(settings)
        self.stats = AgentStats()
        self._recent_outbound: dict[str, float] = {}

    def ready(self) -> dict[str, str]:
        """Readiness check only. It never opens a conversation or sends text."""
        return self.ui.check_ready()

    def run(self) -> None:
        try:
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
        finally:
            close = getattr(self.ui, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("failed to close Weixin listener")

    def handle_event(self, event: MessageEvent) -> bool:
        self.stats.seen += 1
        log.info(
            "message received group=%s member=%s text=%s",
            event.group_name,
            event.member_name,
            (event.text or "")[:240],
        )
        self_event = self._is_self_event(event)
        if self_event:
            event.is_self = True
        event_id = self.storage.record_message(event)
        trigger_terms = [
            *self.settings.wechat_ai_aliases,
            *self.settings.wechat_keywords,
        ]
        if self.settings.wechat_self_name.strip():
            trigger_terms.append(self.settings.wechat_self_name)
        question_hash = self.storage.question_hash(event, trigger_terms)
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
        if claim.claimed and not self_event:
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
            log.info("message skipped member=%s reason=%s", event.member_name, reason)
            return False

        memories = self.storage.search_facts(
            account_id=event.account_id,
            group_id=event.group_id,
            member_id=event.member_id,
            query=event.text,
            half_life_days=self.settings.half_life_for("chat"),
        )
        context_messages = self.storage.recent_messages(
            account_id=event.account_id,
            group_id=event.group_id,
            limit=self.settings.wechat_context_messages,
            max_chars=self.settings.wechat_context_max_chars,
            exclude_message_hash=self.storage.hash_event(event),
        )
        source_context = {
            "channel": "weixin",
            "conversationType": "group",
            "senderId": event.member_id,
            "senderName": event.member_name,
            "conversationTitle": event.group_name,
            "chatId": event.group_id,
            "sentAt": event.observed_at.isoformat(),
        }
        reply_text: str | None = None
        try:
            if self.image_client.should_generate(event.text):
                image_reply = self.image_client.generate(self.image_client.prompt(event.text))
                reply_text = image_reply.text
                log.info("image route model=%s used_api=%s", image_reply.model, image_reply.used_api)
            else:
                web_sources = self.web_research.research(event.text)
                if web_sources:
                    log.info("online research sources=%d for %s", len(web_sources), event.member_name)
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                        web_sources=web_sources,
                    )
                else:
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                    )
                reply_text = reply.text
            action = OutboundAction(
                account_id=event.account_id,
                group_id=event.group_id,
                group_name=event.group_name,
                text=reply_text or "",
                action_type="group_reply",
                target_member_id=event.member_id,
            )
            allowed, auth_reason = self.policy.authorize(action)
            if not allowed:
                self.storage.finish_processing(claim.event_key, "skipped", auth_reason, reply_text)
                log.info("reply blocked (%s): %s", auth_reason, (reply_text or "")[:80])
                self.stats.skipped += 1
                return False
            # A record becomes ``sent`` only after the UI adapter returns.  A
            # model response or a dry-run audit entry alone is not an answer.
            self.ui.send(action)
            self.policy.mark_sent()
            self._remember_outbound(reply_text or "")
            self.storage.finish_processing(claim.event_key, "sent", "sent", reply_text)
            self.stats.replied += 1
            log.info(
                "reply sent and verified member=%s text=%s",
                event.member_name,
                (reply_text or "")[:240],
            )
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

    def _reply_with_context(
        self,
        event: MessageEvent,
        memories: list[MemoryFact],
        context_messages,
        source_context: dict[str, object],
        *,
        web_sources=None,
    ):
        """Call the richer prompt contract while keeping small test doubles compatible."""

        try:
            return self.llm.reply(
                event.text,
                event.member_name,
                event.group_name,
                memories,
                web_sources=web_sources,
                context_messages=context_messages,
                source_context=source_context,
                reply_to=event.reply_to,
            )
        except TypeError as exc:
            # Existing local integrations may provide a four-argument fake LLM.
            # Do not make those adapters change just because context is now
            # available on the production client.
            message = str(exc).casefold()
            if "unexpected keyword" not in message and "positional argument" not in message:
                raise
            if web_sources:
                return self.llm.reply(
                    event.text,
                    event.member_name,
                    event.group_name,
                    memories,
                    web_sources=web_sources,
                )
            return self.llm.reply(event.text, event.member_name, event.group_name, memories)

    def _is_self_event(self, event: MessageEvent) -> bool:
        if event.is_self:
            return True
        sender = event.member_name.strip().casefold()
        if sender not in {"unknown", "我", "自己", "me", "you", "you (me)"}:
            return False
        self._expire_outbound()
        if self._normalize_outbound(event.text) in self._recent_outbound:
            return True
        # The UIA sender label can be ``unknown`` after a restart.  The
        # durable reply ledger still knows which text this agent successfully
        # sent to this account/group, so use it as the final self-echo guard.
        try:
            return self.storage.matches_recent_sent_reply(
                account_id=event.account_id,
                group_id=event.group_id,
                text=event.text,
            )
        except Exception:
            # A locked or unavailable ledger must not take down the polling
            # loop; the UI adapter's database identity guard remains active.
            return False

    def _remember_outbound(self, text: str) -> None:
        self._expire_outbound()
        self._recent_outbound[self._normalize_outbound(text)] = time.monotonic()

    def _expire_outbound(self) -> None:
        cutoff = time.monotonic() - 300
        self._recent_outbound = {
            text: seen_at for text, seen_at in self._recent_outbound.items() if seen_at >= cutoff
        }

    @staticmethod
    def _normalize_outbound(text: str) -> str:
        # UIA and the database can disagree on full-width punctuation and
        # special-width spaces.  Keep the outbound echo guard on the same
        # normalized representation so an ``unknown`` sender cannot re-enter
        # the model after a successful send.
        value = unicodedata.normalize("NFKC", text or "")
        value = "".join(char for char in value if unicodedata.category(char) not in {"Cf"})
        return " ".join(value.casefold().split())

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
