from __future__ import annotations

import logging
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace

from .config import Settings
from .image import ImageClient
from .image_context import select_image_paths
from .llm import LLMClient
from .models import MemoryFact, MessageEvent, OutboundAction
from .policy import ActionPolicy
from .profiles import (
    extract_member_profile_patch,
    extract_memory_candidates,
    extract_task_patch,
    has_task_signal,
    infer_task_id,
)
from .prompt_profiles import build_profile_summary
from .profile_synthesis import synthesize_group_profile
from .reply_queue import ReplyQueue
from .storage import Storage
from .textnorm import normalize_chat_text
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
        # Per-group pictures that a member explicitly pointed at again.  Each
        # entry keeps the image in the prompt for a few more replies instead of
        # re-attaching every historical picture for the whole context window.
        self._image_refresh: dict[str, dict[str, int]] = {}
        self._synthesis_inflight: set[tuple[str, str]] = set()
        self._training_export_attempted: set[str] = set()
        self._training_export_retry_at: dict[str, float] = {}

    def ready(self) -> dict[str, str]:
        """Readiness check only. It never opens a conversation or sends text."""
        return self.ui.check_ready()

    def _maybe_export_daily_training(self) -> None:
        """Export one local calendar day after the configured evening time.

        It is deliberately opt-in and local-only.  If the process was down at
        the scheduled time, the next poll catches up on the previous day once;
        a marker prevents duplicate exports on restart.
        """

        if not self.settings.training_export_enabled:
            return
        now = datetime.now().astimezone()
        scheduled_today = now.replace(
            hour=int(self.settings.training_export_hour),
            minute=int(self.settings.training_export_minute),
            second=0,
            microsecond=0,
        )
        target_day = now.date() if now >= scheduled_today else (now.date() - timedelta(days=1))
        day_text = target_day.isoformat()
        if day_text in self._training_export_attempted:
            return
        retry_at = self._training_export_retry_at.get(day_text, 0.0)
        if retry_at > time.monotonic():
            return
        self._training_export_attempted.add(day_text)

        target = Path(self.settings.training_export_dir).expanduser() / day_text
        marker = target / ".exported"
        if marker.exists():
            return
        start = datetime.combine(target_day, clock_time.min, tzinfo=now.tzinfo)
        since = start.astimezone(timezone.utc)
        until = (start + timedelta(days=1)).astimezone(timezone.utc)
        try:
            from .training_data import export_training_data

            report = export_training_data(
                self.storage,
                target,
                window_messages=self.settings.training_export_context_messages,
                anonymize=self.settings.training_export_anonymize,
                since=since,
                until=until,
            )
            marker.write_text(day_text + "\n", encoding="utf-8")
            log.info(
                "daily training export day=%s messages=%s path=%s",
                day_text,
                report.get("messages"),
                target,
            )
        except Exception:
            # Allow a bounded retry instead of silently missing the day.
            self._training_export_attempted.discard(day_text)
            self._training_export_retry_at[day_text] = time.monotonic() + 1_800.0
            log.exception("daily training export failed day=%s", day_text)

    def run(self) -> None:
        reply_queue: ReplyQueue[MessageEvent] = ReplyQueue(
            maxsize=self.settings.wechat_reply_queue_max_size
        )
        try:
            self.ui.connect()
            if not getattr(self.settings, "wechat_group_names", None):
                raise RuntimeError("请在 .env 设置 WECHAT_GROUP_NAMES 后再启动真实微信监听")
            log.info(
                "Listening to configured test groups: %s",
                ", ".join(self.settings.wechat_group_names),
            )
            while not self.policy.is_stopped():
                try:
                    for event in self.ui.poll(account_id=self._account_id()):
                        self._enqueue_reply(reply_queue, event)
                    self._reply_next(reply_queue)
                    self.storage.purge_expired_messages()
                    self._maybe_export_daily_training()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.stats.failed += 1
                    log.exception("poll cycle failed")
                time.sleep(self.settings.wechat_poll_seconds)
            log.warning("STOP file detected; listener stopped")
        finally:
            pending = reply_queue.clear()
            if pending:
                log.warning("reply queue discarded pending=%s", pending)
            close = getattr(self.ui, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("failed to close Weixin listener")

    def _enqueue_reply(
        self, reply_queue: ReplyQueue[MessageEvent], event: MessageEvent
    ) -> None:
        """Buffer one inbound message so a slow reply cannot lose the next one."""

        dropped = reply_queue.push(event)
        if dropped is not None:
            log.warning(
                "message dropped from reply queue member=%s text=%s",
                dropped.member_name,
                (dropped.text or "")[:80],
            )
        depth = len(reply_queue)
        if depth > 1:
            log.info(
                "message queued member=%s depth=%s",
                event.member_name,
                depth,
            )

    def _reply_next(self, reply_queue: ReplyQueue[MessageEvent]) -> None:
        """Answer the oldest queued message, one per poll cycle."""

        event = reply_queue.pop()
        if event is None:
            return
        log.debug(
            "reply queue dequeue member=%s remaining=%s",
            event.member_name,
            len(reply_queue),
        )
        self.handle_event(event)

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
        normalized_event = self.storage.normalize_event(event)
        analysis_text = normalized_event.text
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

        # Mirror every new original row to the account/group OpenViking corpus.
        # Known members additionally receive a copy below their member scope;
        # unknown/self rows remain group-level corpus evidence only.
        if claim.is_new_event:
            mirror_raw = getattr(self.storage, "mirror_raw_message", None)
            if callable(mirror_raw):
                mirror_raw(event, event_id)

        # Only explicit, low-risk statements become durable fact candidates.
        # Ordinary chatter is accumulated separately as soft behavior signals;
        # it is never promoted to a confirmed preference or sensitive trait.
        if claim.is_new_event and not self_event:
            behavior_patch = (
                extract_member_profile_patch(
                    event.text,
                    str(event_id or event.external_message_id or "").strip() or None,
                    ai_aliases=self.settings.wechat_ai_aliases,
                )
                if self._is_known_member(event)
                else {}
            )
            self._ingest_scoped_memory(event, event_id, behavior_patch=behavior_patch)

        # Fit a group-level speaking style from ordinary member messages. The
        # profile is account/group scoped and aggregate-only; it never becomes
        # a member fact and is refreshed in batches to avoid per-message model
        # calls.  Self echoes and replayed UI rows are excluded here.
        if (
            self.settings.group_style_enabled
            and claim.is_new_event
            and not self_event
            and self._is_style_candidate(event)
        ):
            refreshed = self.storage.observe_group_style(
                event,
                min_messages=self.settings.group_style_min_messages,
                refresh_messages=self.settings.group_style_refresh_messages,
                sample_messages=self.settings.group_style_sample_messages,
                max_chars=self.settings.group_style_max_chars,
            )
            if refreshed:
                log.debug(
                    "group style profile ready account=%s group=%s messages=%s",
                    event.account_id,
                    event.group_id,
                    self.settings.group_style_min_messages,
                )
                self._schedule_synthesis(event.account_id, event.group_id)

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
        if normalized_event.is_system:
            self.storage.finish_processing(claim.event_key, "skipped", "system_message")
            self.stats.skipped += 1
            log.info("message skipped member=%s reason=system_message", event.member_name)
            return False
        proactive = reason.startswith("proactive_")
        if proactive:
            log.info("proactive candidate group=%s member=%s reason=%s", event.group_id, event.member_name, reason)

        # Detect image intent before semantic memory, OpenViking, web search,
        # and profile assembly. Image generation does not need those remote
        # calls, so an image request cannot be delayed by a slow memory host.
        is_image_request = self.image_client.should_generate(analysis_text or event.text)
        if proactive and is_image_request:
            self.storage.finish_processing(claim.event_key, "skipped", "proactive_image_skipped")
            self.stats.skipped += 1
            log.info("message skipped member=%s reason=proactive_image_skipped", event.member_name)
            return False
        memories = []
        context_messages = []
        profile_context: dict[str, object] = {}
        profile_summary = ""
        group_style_profile = ""
        task_id = event.task_id or self.storage.latest_task_id(event.account_id, event.group_id)

        if not is_image_request:
            memories = self.storage.search_facts(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                query=analysis_text,
                half_life_days=self.settings.half_life_for("chat"),
            )
            # Use the hybrid scoped path when available. The legacy lexical
            # call above remains as a safe fallback for custom Storage doubles.
            try:
                memories = self.storage.search_scoped_memories(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    member_id=event.member_id,
                    query=analysis_text,
                    half_life_days=self.settings.half_life_for("chat"),
                    task_id=task_id,
                )
            except AttributeError:
                pass
            context_query = analysis_text
            if event.reply_to and event.reply_to.text.strip():
                quote = normalize_chat_text(
                    event.reply_to.text,
                    sender_name=event.reply_to.sender_name,
                    ai_aliases=self.settings.wechat_ai_aliases,
                ).text
                context_query += " " + (quote or event.reply_to.text)
            context_messages = self.storage.recent_messages(
                account_id=event.account_id,
                group_id=event.group_id,
                limit=self.settings.wechat_context_messages,
                max_chars=self.settings.wechat_context_max_chars,
                exclude_message_hash=self.storage.hash_event(event),
                query=context_query,
                thread_id=event.thread_id,
                task_id=task_id,
            )
            try:
                related = self.storage.related_messages(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    query=analysis_text,
                    limit=max(0, self.settings.wechat_context_messages // 2),
                    max_chars=self.settings.wechat_context_max_chars // 2,
                    exclude_message_hash=self.storage.hash_event(event),
                    thread_id=event.thread_id,
                    task_id=task_id,
                )
                context_messages = self._merge_context_messages(context_messages, related)
            except AttributeError:
                pass
            profile_context = {
                "user": self.storage.user_profile(event.account_id, event.group_id, event.member_id),
                "ai": self.storage.ai_profile(event.account_id, event.group_id),
                "task": self.storage.task_profile(
                    event.account_id,
                    event.group_id,
                    task_id,
                    event.member_id if event.task_id else "",
                )
                if task_id
                else {},
            }
            profile_summary = build_profile_summary(
                profile_context.get("user"),
                profile_context.get("ai"),
                profile_context.get("task"),
                max_chars=self.settings.profile_prompt_max_chars,
            )
            group_style_profile = (
                self.storage.group_style_profile(event.account_id, event.group_id)
                if self.settings.group_style_enabled
                else ""
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
        image_reply = None
        try:
            if is_image_request:
                try:
                    image_reply = self.image_client.generate(
                        self.image_client.prompt(analysis_text)
                    )
                except Exception as exc:
                    # Do not kill the polling loop when a provider rejects a
                    # request. Send a short diagnostic reply through the same
                    # reliable UI path and keep the full detail in the ledger.
                    log.warning("image generation failed error=%s", type(exc).__name__)
                    reply_text = f"图片生成失败：{str(exc)[:240]}"
                else:
                    reply_text = image_reply.text
                    log.info(
                        "image route model=%s used_api=%s files=%s",
                        image_reply.model,
                        image_reply.used_api,
                        len(image_reply.local_paths),
                    )
            else:
                # Proactive interjections should stay cheap and local; do not
                # launch web research just because a casual cue matched.
                web_sources = [] if proactive else self.web_research.research(analysis_text)
                if web_sources:
                    log.info("online research sources=%d for %s", len(web_sources), event.member_name)
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                        web_sources=web_sources,
                        group_style_profile=group_style_profile,
                        profile_summary=profile_summary,
                        proactive=proactive,
                    )
                else:
                    reply = self._reply_with_context(
                        event,
                        memories,
                        context_messages,
                        source_context,
                        group_style_profile=group_style_profile,
                        profile_summary=profile_summary,
                        proactive=proactive,
                    )
                reply_text = reply.text
                if proactive and str(reply_text or "").strip().strip("[] ") == "不回复":
                    self.storage.finish_processing(
                        claim.event_key, "skipped", "proactive_declined"
                    )
                    self.stats.skipped += 1
                    log.info("proactive declined group=%s member=%s", event.group_id, event.member_name)
                    return False
            action = OutboundAction(
                account_id=event.account_id,
                group_id=event.group_id,
                group_name=event.group_name,
                text=reply_text or "",
                action_type="group_reply",
                target_member_id=event.member_id,
                media_paths=list(image_reply.local_paths) if image_reply else [],
            )
            allowed, auth_reason = self.policy.authorize(action)
            if not allowed:
                self.storage.finish_processing(claim.event_key, "skipped", auth_reason, reply_text)
                log.info("reply blocked (%s): %s", auth_reason, (reply_text or "")[:80])
                self.stats.skipped += 1
                return False
            # A record becomes ``sent`` only after the UI adapter returns.  A
            # model response or a dry-run audit entry alone is not an answer.
            try:
                self.ui.send(action)
            finally:
                if image_reply and image_reply.local_paths:
                    cleanup = getattr(self.image_client, "cleanup_local", None)
                    if cleanup:
                        cleanup(image_reply.local_paths)
            self.policy.mark_sent()
            self._remember_outbound(reply_text or "")
            self.storage.finish_processing(claim.event_key, "sent", "sent", reply_text)
            try:
                fit = self.storage.record_reply_style(
                    event.account_id, event.group_id, reply_text or ""
                )
                if fit:
                    log.debug("style fit group=%s fit=%s", event.group_id, fit)
            except Exception:
                log.exception("failed to record style fit group=%s", event.group_id)
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
        group_style_profile: str = "",
        profile_context: dict[str, object] | None = None,
        profile_summary: str = "",
        proactive: bool = False,
    ):
        """Call the richer prompt contract while keeping small test doubles compatible."""

        normalized = self.storage.normalize_event(event)
        question = normalized.text
        reply_to = event.reply_to
        if reply_to is not None:
            quote = normalize_chat_text(
                reply_to.text,
                sender_name=reply_to.sender_name,
                ai_aliases=self.settings.wechat_ai_aliases,
            ).text
            if quote:
                reply_to = replace(reply_to, text=quote)
        image_paths = self._images_for_prompt(event, question, reply_to, proactive=proactive)
        try:
            return self.llm.reply(
                question,
                event.member_name,
                event.group_name,
                memories,
                web_sources=web_sources,
                context_messages=context_messages,
                source_context=source_context,
                reply_to=reply_to,
                group_style_profile=group_style_profile,
                profile_summary=profile_summary,
                proactive=proactive,
                image_paths=image_paths,
                context_images=False,
            )
        except TypeError as exc:
            # Existing local integrations may provide a four-argument fake LLM.
            # Do not make those adapters change just because context is now
            # available on the production client.
            message = str(exc).casefold()
            if "unexpected keyword" not in message and "positional argument" not in message:
                raise
            # Retry progressively for small local test doubles and older
            # integrations that only implement the original four arguments.
            if web_sources:
                try:
                    return self.llm.reply(
                        question,
                        event.member_name,
                        event.group_name,
                        memories,
                        web_sources=web_sources,
                    )
                except TypeError as second_exc:
                    if "unexpected keyword" not in str(second_exc).casefold():
                        raise
            return self.llm.reply(question, event.member_name, event.group_name, memories)

    def _recent_image_window(self, event: MessageEvent):
        """Unfiltered recent rows, used only to age pictures for the prompt.

        ``recent_messages`` normally filters rows by the current question; that
        would make an old picture look "recent" whenever the new text resembles
        its row, which is how one bottle stayed in context for dozens of turns.
        Images are aged against the real conversation instead.
        """

        try:
            limit = min(
                50,
                max(
                    6,
                    int(self.settings.wechat_context_messages)
                    + int(self.settings.wechat_image_context_turns)
                    + 2,
                    int(self.settings.wechat_image_reference_window) + 1,
                ),
            )
            return self.storage.recent_messages(
                account_id=event.account_id,
                group_id=event.group_id,
                limit=limit,
                max_chars=self.settings.wechat_context_max_chars,
                exclude_message_hash=self.storage.hash_event(event),
                query=None,
            )
        except Exception:
            log.debug("image context window unavailable", exc_info=True)
            return []

    def _images_for_prompt(
        self,
        event: MessageEvent,
        question: str,
        reply_to,
        *,
        proactive: bool = False,
    ) -> list[str]:
        """Pick the pictures that may accompany this one reply.

        A picture is visible for a few messages after it arrives, comes back
        when somebody explicitly talks about it, and never comes back merely
        because the semantic context search matched its row.
        """

        current = list(event.image_paths or [])
        if proactive:
            # Proactive interjections must not resurrect an old picture.
            return current
        selected, next_state = select_image_paths(
            current,
            question,
            quoted_text=(reply_to.text if reply_to is not None else ""),
            recent_messages=self._recent_image_window(event),
            turns=int(self.settings.wechat_image_context_turns),
            reference_window=int(self.settings.wechat_image_reference_window),
            max_count=int(self.settings.wechat_image_max_count),
            refreshed=self._image_refresh.get(event.group_id, {}),
        )
        if next_state:
            self._image_refresh[event.group_id] = next_state
        else:
            self._image_refresh.pop(event.group_id, None)
        return selected

    @staticmethod
    def _merge_context_messages(primary, related):
        seen = {(item.sender_name, item.text, item.observed_at) for item in primary}
        merged = list(primary)
        for item in related:
            key = (item.sender_name, item.text, item.observed_at)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _ingest_scoped_memory(
        self,
        event: MessageEvent,
        event_id: int | None,
        *,
        behavior_patch: dict[str, object] | None = None,
    ) -> None:
        source_id = str(event_id or event.external_message_id or "").strip() or None
        # A sender that the UI could not identify is safe for the shared group
        # corpus and group-level AI style, but not for a member profile.
        known_member = self._is_known_member(event)
        candidates = (
            extract_memory_candidates(
                event,
                source_id,
                ai_aliases=self.settings.wechat_ai_aliases,
            )
            if known_member
            else []
        )
        profile_patch: dict[str, list[str]] = {}
        for candidate in candidates:
            try:
                self.storage.add_scoped_memory(candidate)
            except Exception:
                log.exception("failed to save scoped memory type=%s", candidate.memory_type)
                continue
            field = {
                "preference": "preferences",
                "interest": "interests",
                "tool": "tools",
                "constraint": "constraints",
                "correction": "corrections",
            }.get(candidate.memory_type, "confirmed_info")
            profile_patch.setdefault(field, []).append(candidate.content)
        if known_member and behavior_patch:
            profile_patch.update(behavior_patch)
        if profile_patch and known_member:
            self.storage.upsert_user_profile(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                member_name=event.member_name,
                patch=profile_patch,
                source_message_ids=[source_id] if source_id else [],
            )
        if self._is_style_candidate(event):
            self.storage.observe_ai_profile(event, source_id)
        normalized_text = self.storage.normalize_event(event).text
        task_patch = extract_task_patch(normalized_text)
        if task_patch or has_task_signal(normalized_text):
            existing_task = self.storage.latest_task_id(event.account_id, event.group_id)
            task_id = infer_task_id(event, existing_task or f"task-current-{event.group_id}")
            if task_id:
                # An explicitly supplied task id can be member-owned.  A
                # synthesized current-group task remains shared by the group.
                task_member_id = event.member_id if event.task_id else ""
                self.storage.upsert_task_profile(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    task_id=task_id,
                    patch=task_patch or {"progress": normalized_text[:220]},
                    source_message_ids=[source_id] if source_id else [],
                    member_id=task_member_id,
                )

    def _is_style_candidate(self, event: MessageEvent) -> bool:
        """Reject empty/system placeholder rows before fitting group style."""

        normalized = normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if normalized.is_system:
            return False
        text = " ".join(normalized.text.split()).strip()
        if not text:
            return False
        if text in {"查看更多消息", "以下为新消息"}:
            return False
        if text.startswith("[") and text.endswith("]"):
            return False
        return True

    def rebuild_memory(self, *, window_messages: int | None = None) -> dict[str, object]:
        """Local backfill plus optional semantic synthesis of group profiles."""

        report = self.storage.rebuild_memory(window_messages=window_messages)
        report["synthesis"] = self.synthesize_group_profiles(window_messages=window_messages)
        return report

    def synthesize_group_profiles(self, *, window_messages: int | None = None) -> list[dict[str, object]]:
        if not self.settings.profile_synthesis_enabled or not self.settings.llm_api_key:
            return []
        limit = max(
            20,
            min(
                int(window_messages or self.settings.profile_synthesis_messages),
                int(self.settings.profile_synthesis_messages),
            ),
        )
        results: list[dict[str, object]] = []
        for account_id, group_id, group_name in self.storage.group_scopes():
            items = self.storage.profile_window_messages(account_id, group_id, limit=limit)
            if len(items) < 10:
                results.append(
                    {
                        "group_id": group_id,
                        "group_name": group_name,
                        "messages": len(items),
                        "saved": False,
                        "reason": "too_few_messages",
                    }
                )
                continue
            data = synthesize_group_profile(
                self.llm,
                items,
                max_input_chars=self.settings.profile_synthesis_max_input_chars,
            )
            saved = self.storage.save_synthesized_group_profile(
                account_id,
                group_id,
                data,
                group_name=group_name,
            )
            results.append(
                {
                    "group_id": group_id,
                    "group_name": group_name,
                    "messages": len(items),
                    "saved": saved,
                    "fields": sorted(data),
                }
            )
        return results

    def _schedule_synthesis(self, account_id: str, group_id: str) -> None:
        """Refresh the semantic profile in the background after a batch refresh."""

        if not self.settings.profile_synthesis_enabled or not self.settings.llm_api_key:
            return
        key = (account_id, group_id)
        if key in self._synthesis_inflight:
            return
        self._synthesis_inflight.add(key)
        threading.Thread(
            target=self._synthesis_worker,
            args=(account_id, group_id),
            name="wechat-profile-synthesis",
            daemon=True,
        ).start()

    def _synthesis_worker(self, account_id: str, group_id: str) -> None:
        try:
            items = self.storage.profile_window_messages(
                account_id,
                group_id,
                limit=self.settings.profile_synthesis_messages,
            )
            if len(items) < 10:
                return
            data = synthesize_group_profile(
                self.llm,
                items,
                max_input_chars=self.settings.profile_synthesis_max_input_chars,
            )
            if data:
                self.storage.save_synthesized_group_profile(account_id, group_id, data)
                log.info(
                    "synthesized group profile account=%s group=%s fields=%s",
                    account_id,
                    group_id,
                    ",".join(sorted(data)),
                )
        except Exception:
            log.exception("profile synthesis failed account=%s group=%s", account_id, group_id)
        finally:
            self._synthesis_inflight.discard((account_id, group_id))

    def _is_self_event(self, event: MessageEvent) -> bool:
        if event.is_self:
            return True
        sender = event.member_name.strip().casefold()
        # The bot's own UI echo can carry its display name instead of
        # "unknown".  Treat the configured identity and aliases as self,
        # otherwise the group AI profile learns from its own replies.
        bot_names = {
            str(self.settings.wechat_self_name or "").strip().casefold(),
            *[str(alias or "").strip().casefold() for alias in self.settings.wechat_ai_aliases],
        }
        bot_names.discard("")
        if sender and sender in bot_names:
            return True
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

    @staticmethod
    def _is_known_member(event: MessageEvent) -> bool:
        value = str(event.member_id or "").strip().casefold()
        return value not in {"", "unknown", "我", "自己", "me", "you", "you (me)"}

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
