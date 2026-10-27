from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings
from .models import ConversationMessage, MemoryFact, MessageEvent, OutboundAction, utc_now


class Base(DeclarativeBase):
    pass


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64), default="weixin_ui")


class FactRow(Base):
    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(32), default="inferred", index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class BindingRow(Base):
    __tablename__ = "identity_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    platform: Mapped[str] = mapped_column(String(32), index=True)
    platform_account_id: Mapped[str] = mapped_column(String(256), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActionRow(Base):
    __tablename__ = "action_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    target_member_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    at_names_json: Mapped[str] = mapped_column(Text, default="[]")
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ReplyRecordRow(Base):
    """Persistent idempotency and answer ledger for inbound messages."""

    __tablename__ = "reply_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    question_hash: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="weixin_ui")
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlatformActionRow(Base):
    __tablename__ = "platform_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    action_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(256), index=True)
    parent_comment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    target_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    event_key: str
    question_hash: str
    claimed: bool
    is_new_event: bool
    status: str
    reason: str


class Storage:
    def __init__(self, settings: Settings):
        url = settings.database_url
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine_kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite:///") else {}
        self.engine = create_engine(url, future=True, **engine_kwargs)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.settings = settings

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @staticmethod
    def hash_event(event: MessageEvent) -> str:
        # UI polling timestamps change on every scan.  A timestamp must never
        # be part of the idempotency key, otherwise a restart can answer the
        # same visible row again.
        external_id = (event.external_message_id or "").strip()
        stable_part = f"external:{external_id}" if external_id else f"text:{_normalize_text(event.text)}"
        quoted = event.reply_to
        quote_part = ""
        if quoted is not None and quoted.text.strip():
            quote_part = "\x1fquote:" + _normalize_text(quoted.text)
            if quoted.sender_name.strip():
                quote_part += "\x1fsender:" + _normalize_text(quoted.sender_name)
            if quoted.message_id and quoted.message_id.strip():
                quote_part += "\x1fid:" + _normalize_text(quoted.message_id)
        raw = "\x1f".join(
            [
                event.account_id,
                event.group_id,
                event.member_id,
                stable_part,
            ]
        )
        raw += quote_part
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def question_hash(event: MessageEvent, aliases: Iterable[str] = ()) -> str:
        """Hash a normalized question for durable answer de-duplication."""
        normalized = _normalize_question(event.text, aliases)
        quoted = event.reply_to
        quote_text = _normalize_text(quoted.text) if quoted is not None else ""
        quote_sender = _normalize_text(quoted.sender_name) if quoted is not None else ""
        quote_id = _normalize_text(quoted.message_id) if quoted is not None and quoted.message_id else ""
        raw = "\x1f".join(
            [event.account_id, event.group_id, event.member_id, normalized, quote_sender, quote_text, quote_id]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record_message(self, event: MessageEvent) -> int | None:
        now = event.observed_at
        expires = now + timedelta(days=self.settings.raw_retention_days)
        row = MessageRow(
            message_hash=self.hash_event(event),
            account_id=event.account_id,
            group_id=event.group_id,
            group_name=event.group_name,
            member_id=event.member_id,
            member_name=event.member_name,
            text=event.text,
            observed_at=now,
            expires_at=expires,
            source=event.source,
        )
        with self.Session.begin() as session:
            existing = session.scalar(select(MessageRow).where(MessageRow.message_hash == row.message_hash))
            if existing:
                return existing.id
            session.add(row)
            session.flush()
            return row.id

    def claim_processing(
        self,
        event: MessageEvent,
        question_hash: str,
        lease_seconds: int | None = None,
    ) -> ProcessingClaim:
        """Claim an inbound event or record why it must be skipped.

        ``event_key`` prevents the same visible UI row from being processed
        twice.  ``question_hash`` prevents a newly observed copy of a question
        from receiving another answer after a successful send.  Only ``sent``
        records block a question; skipped/failed records remain retryable.
        """
        event_key = self.hash_event(event)
        now = utc_now()
        lease = lease_seconds or getattr(self.settings, "wechat_processing_lease_seconds", 300)
        try:
            with self.Session.begin() as session:
                existing = session.scalar(
                    select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key)
                )
                if existing:
                    retryable = existing.status == "failed" or existing.reason in {
                        "sending_disabled",
                        "stop_file_present",
                        "circuit_breaker_rate",
                    }
                    stale = (
                        existing.status == "processing"
                        and (now - _aware(existing.updated_at)).total_seconds() > lease
                    )
                    if stale or retryable:
                        existing.status = "processing"
                        existing.reason = "processing_lease_expired" if stale else "retry_claimed"
                        existing.attempt_count += 1
                        existing.updated_at = now
                        return ProcessingClaim(
                            event_key,
                            question_hash,
                            True,
                            False,
                            "processing",
                            "processing_lease_expired" if stale else "retry_claimed",
                        )
                    return ProcessingClaim(
                        event_key,
                        question_hash,
                        False,
                        False,
                        existing.status,
                        "event_already_processed",
                    )

                answered = session.scalar(
                    select(ReplyRecordRow)
                    .where(
                        ReplyRecordRow.account_id == event.account_id,
                        ReplyRecordRow.group_id == event.group_id,
                        ReplyRecordRow.member_id == event.member_id,
                        ReplyRecordRow.question_hash == question_hash,
                        ReplyRecordRow.status == "sent",
                    )
                    .order_by(ReplyRecordRow.answered_at.desc())
                    .limit(1)
                )
                if answered:
                    session.add(
                        ReplyRecordRow(
                            event_key=event_key,
                            question_hash=question_hash,
                            account_id=event.account_id,
                            group_id=event.group_id,
                            group_name=event.group_name,
                            member_id=event.member_id,
                            member_name=event.member_name,
                            text=event.text,
                            status="skipped",
                            reason="question_already_answered",
                            reply_text=None,
                            source=event.source,
                            attempt_count=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    return ProcessingClaim(
                        event_key, question_hash, False, True, "skipped", "question_already_answered"
                    )

                session.add(
                    ReplyRecordRow(
                        event_key=event_key,
                        question_hash=question_hash,
                        account_id=event.account_id,
                        group_id=event.group_id,
                        group_name=event.group_name,
                        member_id=event.member_id,
                        member_name=event.member_name,
                        text=event.text,
                        status="processing",
                        reason="claimed",
                        reply_text=None,
                        source=event.source,
                        attempt_count=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return ProcessingClaim(event_key, question_hash, True, True, "processing", "claimed")
        except IntegrityError:
            # Another poller won the unique event_key race.  Treat that as a
            # normal duplicate and leave the winner's state untouched.
            with self.Session() as session:
                existing = session.scalar(
                    select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key)
                )
            if existing:
                return ProcessingClaim(
                    event_key, question_hash, False, False, existing.status, "event_already_processed"
                )
            raise

    def finish_processing(
        self,
        event_key: str,
        status: str,
        reason: str,
        reply_text: str | None = None,
    ) -> None:
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(select(ReplyRecordRow).where(ReplyRecordRow.event_key == event_key))
            if not row:
                return
            row.status = status
            row.reason = reason
            row.reply_text = reply_text
            row.updated_at = now
            if status == "sent":
                row.answered_at = now

    def processing_summary(self) -> dict[str, int]:
        with self.Session() as session:
            rows = session.execute(
                select(ReplyRecordRow.status, func.count(ReplyRecordRow.id)).group_by(ReplyRecordRow.status)
            )
            return {status: int(count) for status, count in rows}

    def recent_reply_records(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(ReplyRecordRow)
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "event_key": row.event_key,
                "question_hash": row.question_hash,
                "group_name": row.group_name,
                "member_name": row.member_name,
                "text": row.text,
                "status": row.status,
                "reason": row.reason,
                "reply_text": row.reply_text,
                "attempt_count": row.attempt_count,
                "created_at": _aware(row.created_at).isoformat(),
                "updated_at": _aware(row.updated_at).isoformat(),
                "answered_at": _aware(row.answered_at).isoformat() if row.answered_at else None,
            }
            for row in rows
        ]

    def matches_recent_sent_reply(
        self,
        *,
        account_id: str,
        group_id: str,
        text: str,
        limit: int = 200,
    ) -> bool:
        """Detect a bot echo when UIA cannot identify the local sender.

        The reply ledger survives listener restarts, unlike the in-memory
        outbound cache.  Restricting the lookup to the same account and group
        keeps a copied sentence from another conversation from becoming a
        global self-message signal.
        """

        wanted = _normalize_text(text)
        if not wanted:
            return False
        with self.Session() as session:
            rows = session.scalars(
                select(ReplyRecordRow.reply_text)
                .where(
                    ReplyRecordRow.account_id == account_id,
                    ReplyRecordRow.group_id == group_id,
                    ReplyRecordRow.status == "sent",
                    ReplyRecordRow.reply_text.is_not(None),
                )
                .order_by(ReplyRecordRow.updated_at.desc())
                .limit(max(1, min(int(limit), 500)))
            )
            return any(_normalize_text(value or "") == wanted for value in rows)

    def recent_messages(
        self,
        *,
        account_id: str,
        group_id: str,
        limit: int,
        max_chars: int,
        exclude_message_hash: str | None = None,
    ) -> list[ConversationMessage]:
        """Return a bounded, oldest-first window from one group.

        Raw message retention already supplies the durable conversation window.
        This method never crosses account or group boundaries and excludes the
        triggering row so the caller can render the current question once.
        """

        bounded_limit = max(0, min(int(limit), 50))
        bounded_chars = max(500, min(int(max_chars), 30_000))
        if bounded_limit == 0 or bounded_chars <= 0:
            return []
        with self.Session() as session:
            query = (
                select(MessageRow)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                )
                .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                .limit(bounded_limit + 1)
            )
            rows = list(session.scalars(query))

        selected: list[ConversationMessage] = []
        used = 0
        for row in reversed(rows):
            if exclude_message_hash and row.message_hash == exclude_message_hash:
                continue
            text = (row.text or "").strip()
            if not text:
                continue
            remaining = bounded_chars - used
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[:remaining].rstrip()
            if not text:
                continue
            selected.append(
                ConversationMessage(
                    sender_name=row.member_name or "unknown",
                    text=text,
                    observed_at=_aware(row.observed_at),
                )
            )
            used += len(text)
            if len(selected) >= bounded_limit:
                break
        return selected

    def reserve_platform_action(
        self,
        *,
        action_key: str,
        platform: str,
        account_id: str,
        action_type: str,
        target_id: str,
        parent_comment_id: str | None,
        text: str,
        target_url: str | None,
        dry_run: bool,
        metadata: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], bool]:
        now = utc_now()
        with self.Session.begin() as session:
            existing = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if existing:
                return self._platform_action_dict(existing), True
            row = PlatformActionRow(
                action_key=action_key,
                platform=platform,
                account_id=account_id,
                action_type=action_type,
                target_id=target_id,
                parent_comment_id=parent_comment_id,
                target_url=target_url,
                text=text,
                status="dry_run" if dry_run else "queued",
                reason="dry_run_plan" if dry_run else "queued_for_review",
                dry_run=dry_run,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._platform_action_dict(row), False

    def update_platform_action(
        self,
        action_key: str,
        *,
        status: str,
        reason: str,
        executed: bool = False,
    ) -> None:
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if not row:
                raise KeyError(f"platform_action_not_found:{action_key}")
            row.status = status
            row.reason = reason
            row.updated_at = now
            if executed:
                row.executed_at = now

    def platform_action(self, action_key: str) -> dict[str, object]:
        """Return one platform action for an explicit operator approval."""
        with self.Session() as session:
            row = session.scalar(
                select(PlatformActionRow).where(PlatformActionRow.action_key == action_key)
            )
            if not row:
                raise KeyError(f"platform_action_not_found:{action_key}")
            return self._platform_action_dict(row)

    def recent_platform_actions(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(PlatformActionRow)
                    .order_by(PlatformActionRow.updated_at.desc())
                    .limit(max(1, min(limit, 200)))
                )
            )
        return [self._platform_action_dict(row) for row in rows]

    @staticmethod
    def _platform_action_dict(row: PlatformActionRow) -> dict[str, object]:
        return {
            "id": row.id,
            "action_key": row.action_key,
            "platform": row.platform,
            "account_id": row.account_id,
            "action_type": row.action_type,
            "target_id": row.target_id,
            "parent_comment_id": row.parent_comment_id,
            "target_url": row.target_url,
            "text": row.text,
            "status": row.status,
            "reason": row.reason,
            "dry_run": row.dry_run,
            "metadata": json.loads(row.metadata_json or "{}"),
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "executed_at": _aware(row.executed_at).isoformat() if row.executed_at else None,
        }

    def add_fact(self, fact: MemoryFact) -> int:
        row = FactRow(
            account_id=fact.account_id,
            group_id=fact.group_id,
            member_id=fact.member_id,
            memory_type=fact.memory_type,
            content=fact.content,
            confidence=max(0.0, min(1.0, fact.confidence)),
            status=fact.status,
            source_event_id=fact.source_event_id,
            observed_at=fact.observed_at,
            valid_from=fact.valid_from or fact.observed_at,
            valid_to=fact.valid_to,
            metadata_json=json.dumps(fact.metadata, ensure_ascii=False),
        )
        with self.Session.begin() as session:
            session.add(row)
            session.flush()
            return row.id

    def supersede(self, fact_id: int, when: datetime | None = None) -> None:
        with self.Session.begin() as session:
            row = session.get(FactRow, fact_id)
            if row:
                row.status = "superseded"
                row.valid_to = when or utc_now()

    def search_facts(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
        query: str,
        half_life_days: float,
        limit: int = 8,
    ) -> list[MemoryFact]:
        now = utc_now()
        tokens = _tokens(query)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(FactRow)
                    .where(
                        FactRow.account_id == account_id,
                        FactRow.group_id == group_id,
                        FactRow.member_id == member_id,
                        FactRow.status.in_(["confirmed", "inferred"]),
                    )
                    .order_by(FactRow.observed_at.desc())
                    .limit(200)
                )
            )

        scored: list[tuple[float, FactRow]] = []
        for row in rows:
            words = _tokens(row.content)
            lexical = len(tokens & words) / max(1, len(tokens)) if tokens else 0.0
            age_days = max(0.0, (now - _aware(row.observed_at)).total_seconds() / 86400)
            recency = math.exp(-age_days / max(half_life_days, 0.01))
            status_bonus = 0.15 if row.status == "confirmed" else 0.0
            score = 0.55 * lexical + 0.30 * recency + 0.15 * row.confidence + status_bonus
            scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._fact_from_row(row) for _, row in scored[:limit]]

    def bind_platform(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
        member_name: str,
        platform: str,
        platform_account_id: str,
    ) -> int:
        with self.Session.begin() as session:
            row = BindingRow(
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                member_name=member_name,
                platform=platform,
                platform_account_id=platform_account_id,
                status="active",
                confirmed_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id

    def audit_action(self, action: OutboundAction, allowed: bool, reason: str) -> None:
        with self.Session.begin() as session:
            session.add(
                ActionRow(
                    account_id=action.account_id,
                    group_id=action.group_id,
                    action_type=action.action_type,
                    target_member_id=action.target_member_id,
                    text=action.text,
                    at_names_json=json.dumps(action.at_names, ensure_ascii=False),
                    allowed=allowed,
                    reason=reason,
                    created_at=utc_now(),
                )
            )

    def recent_audits(self, limit: int = 50) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(ActionRow).order_by(ActionRow.created_at.desc()).limit(max(1, min(limit, 200)))
                )
            )
        return [
            {
                "id": row.id,
                "group_id": row.group_id,
                "action_type": row.action_type,
                "target_member_id": row.target_member_id,
                "text": row.text,
                "at_names": json.loads(row.at_names_json or "[]"),
                "allowed": row.allowed,
                "reason": row.reason,
                "created_at": _aware(row.created_at).isoformat(),
            }
            for row in rows
        ]

    def purge_expired_messages(self) -> int:
        with self.Session.begin() as session:
            result = session.execute(delete(MessageRow).where(MessageRow.expires_at < utc_now()))
            return int(result.rowcount or 0)

    @staticmethod
    def _fact_from_row(row: FactRow) -> MemoryFact:
        return MemoryFact(
            account_id=row.account_id,
            group_id=row.group_id,
            member_id=row.member_id,
            memory_type=row.memory_type,  # type: ignore[arg-type]
            content=row.content,
            confidence=row.confidence,
            status=row.status,  # type: ignore[arg-type]
            source_event_id=row.source_event_id,
            observed_at=_aware(row.observed_at),
            valid_from=_aware(row.valid_from) if row.valid_from else None,
            valid_to=_aware(row.valid_to) if row.valid_to else None,
            metadata=json.loads(row.metadata_json or "{}"),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(normalized.split())


def _normalize_question(text: str, aliases: Iterable[str]) -> str:
    normalized = _normalize_text(text)
    for alias in sorted((item.strip() for item in aliases if item and item.strip()), key=len, reverse=True):
        if alias.isascii() and alias.replace("_", "").isalnum():
            pattern = rf"@?\s*\b{re.escape(alias.casefold())}\b"
        else:
            pattern = rf"@?\s*{re.escape(alias.casefold())}"
        normalized = re.sub(pattern, " ", normalized, flags=re.IGNORECASE)
    # Punctuation and spacing should not make an already answered question
    # appear new.  ``\w`` keeps Chinese characters and alphanumerics.
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized, flags=re.UNICODE)
    return compact or normalized


def _tokens(text: str) -> set[str]:
    """Small dependency-free tokenizer for Chinese and whitespace text."""
    normalized = text.strip().lower()
    words = {token for token in normalized.split() if token}
    cjk = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    words.update(cjk)
    words.update("".join(cjk[index : index + 2]) for index in range(max(0, len(cjk) - 1)))
    return words
