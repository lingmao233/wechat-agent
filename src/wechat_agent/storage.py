from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings
from .embedding import EmbeddingClient, EmbeddingError
from .group_style import (
    build_group_style_profile,
    new_style_stats,
    style_target_band,
    update_style_stats,
)
from .models import ConversationMessage, MemoryFact, MessageEvent, OutboundAction, utc_now
from .openviking_memory import OpenVikingMemoryClient
from .profiles import extract_ai_profile_patch, extract_member_profile_patch
from .textnorm import normalize_chat_text, normalizer_version


log = logging.getLogger(__name__)


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
    # Derived, canonical view of ``text``.  All profile, prompt and retrieval
    # paths read this column; ``text`` remains the raw audit copy.
    text_normalized: Mapped[str] = mapped_column(Text, default="")
    text_kind: Mapped[str] = mapped_column(String(16), default="text")
    text_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    external_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    reply_to_json: Mapped[str] = mapped_column(Text, default="{}")
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64), default="weixin_ui")
    image_paths_json: Mapped[str] = mapped_column(Text, default="[]")


class GroupStyleProfileRow(Base):
    """Aggregate speaking-style profile scoped to one account and group.

    ``stats_json`` contains counters only.  It deliberately does not retain
    raw group messages; the normal ``messages`` table remains responsible for
    bounded conversation context and retention.
    """

    __tablename__ = "group_style_profiles"
    __table_args__ = (UniqueConstraint("account_id", "group_id", name="uq_group_style_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256))
    profile_text: Mapped[str] = mapped_column(Text, default="")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_refresh_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactRow(Base):
    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Additive fields used by the scoped memory contract.  They remain
    # nullable for old SQLite/PostgreSQL databases; ``init_schema`` upgrades
    # existing local tables without dropping any rows.
    memory_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    task_id: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    embedding_model: Mapped[str] = mapped_column(String(256), default="")


class UserProfileRow(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "group_id", "member_id", name="uq_user_profile_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), index=True)
    member_name: Mapped[str] = mapped_column(String(256), default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AIProfileRow(Base):
    __tablename__ = "ai_profiles"
    __table_args__ = (UniqueConstraint("account_id", "group_id", name="uq_ai_profile_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    group_name: Mapped[str] = mapped_column(String(256), default="")
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TaskProfileRow(Base):
    __tablename__ = "task_profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "group_id", "task_id", "member_id", name="uq_task_profile_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(256), index=True)
    task_id: Mapped[str] = mapped_column(String(256), index=True)
    member_id: Mapped[str] = mapped_column(String(256), default="", index=True)
    profile_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    source_message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


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
        self.embedder = EmbeddingClient(settings)
        self.openviking = OpenVikingMemoryClient(settings)
        # Remote profile mirrors are derived from SQL; a short per-scope
        # debounce avoids one TLS handshake and one full-profile upload per
        # incoming message while keeping the remote view close to current.
        self._ov_profile_written_at: dict[tuple[str, ...], float] = {}

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        self._ensure_legacy_columns()

    def _ensure_legacy_columns(self) -> None:
        """Apply additive columns to a pre-scoped local database.

        SQLAlchemy's ``create_all`` intentionally does not alter an existing
        table.  The small migration keeps old memory/message rows intact and
        is safe to run repeatedly for SQLite and PostgreSQL deployments.
        """

        additions = {
            "memory_id": "VARCHAR(128)",
            "scope_json": "TEXT DEFAULT '{}'",
            "source_message_ids_json": "TEXT DEFAULT '[]'",
            "task_id": "VARCHAR(256)",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
            "valid_until": "TIMESTAMP",
            "importance": "FLOAT DEFAULT 0.5",
            "embedding_model": "VARCHAR(256) DEFAULT ''",
            "embedding_json": "TEXT",
        }
        inspector = __import__("sqlalchemy").inspect(self.engine)
        try:
            columns = {item["name"] for item in inspector.get_columns("memory_facts")}
        except Exception:
            return
        if not self.engine.dialect.name == "sqlite":
            # PostgreSQL migrations are supplied explicitly; create_all is
            # enough for a fresh database and ALTER here would need dialect
            # quoting/locking policy owned by deployment tooling.
            return
        with self.engine.begin() as connection:
            for name, definition in additions.items():
                if name in columns:
                    continue
                connection.exec_driver_sql(
                    f'ALTER TABLE memory_facts ADD COLUMN "{name}" {definition}'
                )

        # Message metadata was added after the original local schema.  Keep
        # existing raw rows and add only nullable/defaulted columns.
        try:
            message_columns = {item["name"] for item in inspector.get_columns("messages")}
        except Exception:
            return
        message_additions = {
            "external_message_id": "VARCHAR(256)",
            "thread_id": "VARCHAR(256)",
            "task_id": "VARCHAR(256)",
            "reply_to_json": "TEXT DEFAULT '{}'",
            "is_self": "BOOLEAN DEFAULT FALSE",
            "image_paths_json": "TEXT DEFAULT '[]'",
            "text_normalized": "TEXT DEFAULT ''",
            "text_kind": "VARCHAR(16) DEFAULT 'text'",
            "text_meta_json": "TEXT DEFAULT '{}'",
        }
        with self.engine.begin() as connection:
            for name, definition in message_additions.items():
                if name in message_columns:
                    continue
                connection.exec_driver_sql(
                    f'ALTER TABLE messages ADD COLUMN "{name}" {definition}'
                )

    def normalize_event(self, event: MessageEvent):
        """Canonical view used by every write-side analysis path."""

        return normalize_chat_text(
            event.text,
            sender_id=event.member_id,
            sender_name=event.member_name,
            ai_aliases=self.settings.wechat_ai_aliases,
        )

    def _row_text(self, row: MessageRow) -> str:
        """Return the normalized text for a stored row (backfill-safe)."""

        value = (getattr(row, "text_normalized", "") or "").strip()
        if value:
            return value
        return normalize_chat_text(
            row.text or "",
            sender_id=row.member_id or "",
            sender_name=row.member_name or "",
            ai_aliases=self.settings.wechat_ai_aliases,
        ).text

    def _bot_identity_names(self) -> set[str]:
        names = {str(self.settings.wechat_self_name or "").strip().casefold()}
        names.update(str(alias or "").strip().casefold() for alias in self.settings.wechat_ai_aliases)
        names.discard("")
        return names

    def _is_bot_row(self, row: MessageRow) -> bool:
        if bool(getattr(row, "is_self", False)):
            return True
        names = self._bot_identity_names()
        return bool(names) and (row.member_name or "").strip().casefold() in names

    def _sent_reply_texts(self, account_id: str, group_id: str, limit: int = 400) -> set[str]:
        """Recent verified replies, used to recognize historical self echoes."""

        with self.Session() as session:
            values = list(
                session.scalars(
                    select(ReplyRecordRow.reply_text)
                    .where(
                        ReplyRecordRow.account_id == account_id,
                        ReplyRecordRow.group_id == group_id,
                        ReplyRecordRow.reply_text.is_not(None),
                    )
                    .order_by(ReplyRecordRow.updated_at.desc())
                    .limit(max(1, min(int(limit), 2_000)))
                )
            )
        echoes: set[str] = set()
        for value in values:
            normalized = normalize_chat_text(value or "").text
            if normalized:
                echoes.add(normalized[:200])
        return echoes

    @staticmethod
    def _looks_like_echo(text: str, echoes: set[str]) -> bool:
        if not echoes or not text:
            return False
        probe = text[:200]
        for echo in echoes:
            if probe == echo:
                return True
            if len(echo) >= 20 and echo in text:
                return True
        return False

    @staticmethod
    def _looks_like_bot_output(text: str, member_id: str) -> bool:
        """Unknown-sender rows that self-identify as the bot are UI echoes."""

        if not _is_unknown_member(member_id):
            return False
        probe = str(text or "").strip()
        return probe.startswith(("我是 AI", "对，我是 AI", "本鱼", "大肥鱼", "🐋"))

    def _profile_write_due(self, key: tuple[str, ...], *, force: bool = False) -> bool:
        if not bool(getattr(self.settings, "openviking_enabled", False)):
            return False
        interval = max(0.0, float(getattr(self.settings, "openviking_profile_min_interval_seconds", 0) or 0))
        now = time.monotonic()
        last = self._ov_profile_written_at.get(key)
        if not force and last is not None and interval > 0 and now - last < interval:
            return False
        self._ov_profile_written_at[key] = now
        return True

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
                event.thread_id or "",
                event.task_id or "",
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
            [
                event.account_id,
                event.group_id,
                event.member_id,
                event.thread_id or "",
                event.task_id or "",
                normalized,
                quote_sender,
                quote_text,
                quote_id,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record_message(self, event: MessageEvent) -> int | None:
        now = event.observed_at
        expires = now + timedelta(days=self.settings.raw_retention_days)
        normalized = self.normalize_event(event)
        row = MessageRow(
            message_hash=self.hash_event(event),
            account_id=event.account_id,
            group_id=event.group_id,
            group_name=event.group_name,
            member_id=event.member_id,
            member_name=event.member_name,
            text=event.text,
            text_normalized=normalized.text,
            text_kind=normalized.kind,
            text_meta_json=json.dumps(
                {
                    "prefix_sender": normalized.prefix_sender,
                    "addressed_as": normalized.addressed_as,
                    "quoted_text": normalized.quoted_text,
                    "sticker_count": normalized.sticker_count,
                    "image_count": normalized.image_count,
                    "truncated": normalized.truncated,
                    "normalizer_version": normalizer_version,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            external_message_id=event.external_message_id,
            thread_id=event.thread_id,
            task_id=event.task_id,
            reply_to_json=json.dumps(
                {
                    "text": event.reply_to.text,
                    "sender_name": event.reply_to.sender_name,
                    "message_id": event.reply_to.message_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if event.reply_to
            else "{}",
            is_self=bool(event.is_self),
            observed_at=now,
            expires_at=expires,
            source=event.source,
            image_paths_json=json.dumps(
                [str(path) for path in (event.image_paths or []) if str(path).strip()][:12],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        with self.Session.begin() as session:
            existing = session.scalar(select(MessageRow).where(MessageRow.message_hash == row.message_hash))
            if existing:
                return existing.id
            session.add(row)
            session.flush()
            return row.id

    def mirror_raw_message(self, event: MessageEvent, message_id: int | None) -> bool:
        """Mirror one newly inserted message into the configured OV scopes.

        SQL remains the retention and audit source of truth.  This separate
        method keeps the remote write outside the SQL transaction and makes it
        explicit that raw corpus storage is independent from curated facts.
        """

        stable_id = str(event.external_message_id or message_id or "").strip()
        if not stable_id:
            return False
        normalized = self.normalize_event(event)
        if normalized.is_system or not normalized.text:
            return False
        try:
            return self.openviking.put_raw_message(
                account_id=event.account_id,
                group_id=event.group_id,
                member_id=event.member_id,
                member_name=event.member_name,
                content=normalized.text,
                message_id=stable_id,
                observed_at=event.observed_at.isoformat(),
                source=event.source,
                thread_id=event.thread_id or "",
                task_id=event.task_id or "",
                is_self=bool(event.is_self),
                image_count=len(event.image_paths or []),
            )
        except Exception as exc:  # pragma: no cover - defensive remote boundary
            log.warning("OpenViking raw message write failed error=%s", type(exc).__name__)
            return False

    def observe_group_style(
        self,
        event: MessageEvent,
        *,
        min_messages: int,
        refresh_messages: int,
        sample_messages: int,
        max_chars: int,
    ) -> str:
        """Record one ordinary message and rebuild the windowed style profile.

        Counters are rebuilt from the last N normalized messages instead of
        accumulating for the lifetime of the group.  This makes the "最近 N 条"
        label true and lets the profile drift with the group instead of
        averaging years of old behavior.
        """

        normalized = self.normalize_event(event)
        if normalized.is_system:
            return self.group_style_profile(event.account_id, event.group_id)
        if not normalized.visible_text and not normalized.sticker_count and not normalized.image_count:
            return self.group_style_profile(event.account_id, event.group_id)
        now = utc_now()
        event_hash = self.hash_event(event)
        min_count = max(1, int(min_messages))
        refresh_count = max(1, int(refresh_messages))
        fingerprint_limit = max(1, min(int(sample_messages), 2_000))
        with self.Session.begin() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == event.account_id,
                    GroupStyleProfileRow.group_id == event.group_id,
                )
            )
            if row is None:
                row = GroupStyleProfileRow(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    group_name=event.group_name,
                    profile_text="",
                    stats_json=json.dumps(new_style_stats(), ensure_ascii=False),
                    observed_count=0,
                    last_refresh_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()

            try:
                stats = json.loads(row.stats_json or "{}")
            except (TypeError, ValueError):
                stats = {}
            if not isinstance(stats, dict):
                stats = {}
            seen_hashes = stats.get("seen_event_hashes")
            if not isinstance(seen_hashes, list):
                seen_hashes = []
            seen_hashes = [str(item) for item in seen_hashes if item]
            if event_hash in seen_hashes:
                return row.profile_text or ""

            # The replay guard is the only thing carried between rebuilds.
            # All real counters are re-derived from the window below.
            seen_hashes.append(event_hash)
            stats["seen_event_hashes"] = seen_hashes[-fingerprint_limit:]
            row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            row.observed_count = int(row.observed_count or 0) + 1
            row.group_name = event.group_name or row.group_name
            row.updated_at = now
            should_refresh = int(row.observed_count) >= min_count and (
                not row.profile_text
                or int(row.observed_count) - int(row.last_refresh_count or 0) >= refresh_count
            )
        if not should_refresh:
            return self.group_style_profile(event.account_id, event.group_id)
        self.rebuild_group_profiles(
            event.account_id,
            event.group_id,
            group_name=event.group_name,
            window_messages=self.settings.group_style_window_messages,
            max_chars=max_chars,
            force_remote=True,
        )
        return self.group_style_profile(event.account_id, event.group_id)

    def record_reply_style(self, account_id: str, group_id: str, text: str) -> dict[str, object]:
        """Track whether the bot's sent replies fit the group's length band."""

        normalized = normalize_chat_text(text or "").visible_text.replace("@AI", " ")
        length = len("".join(normalized.split()))
        if length <= 0:
            return {}
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if row is None:
                return {}
            stats = _json_dict(row.stats_json)
            low = int(stats.get("target_low") or 0)
            high = int(stats.get("target_high") or 0)
            if low <= 0 or high <= 0:
                low, high = style_target_band(stats)
            fit = stats.get("style_fit") if isinstance(stats.get("style_fit"), dict) else {}
            samples = int(fit.get("samples") or 0) + 1
            in_band = int(fit.get("in_band") or 0) + (1 if low <= length <= high else 0)
            total_chars = int(fit.get("total_chars") or 0) + length
            stats["target_low"] = low
            stats["target_high"] = high
            stats["style_fit"] = {
                "samples": samples,
                "in_band": in_band,
                "total_chars": total_chars,
                "last_length": length,
                "target_low": low,
                "target_high": high,
                "updated_at": now.isoformat(),
            }
            row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            row.profile_text = build_group_style_profile(
                stats, max_chars=self.settings.group_style_max_chars
            )
            row.updated_at = now
        return dict(stats["style_fit"])

    def rebuild_group_profiles(
        self,
        account_id: str,
        group_id: str,
        *,
        group_name: str = "",
        window_messages: int | None = None,
        max_chars: int | None = None,
        force_remote: bool = False,
    ) -> dict[str, object]:
        """Recompute group style and group AI profile from a bounded window.

        Both profiles are aggregate-only.  Rebuilding (rather than folding
        forever) is what makes them track the group's *current* habits.
        """

        window = max(20, min(int(window_messages or self.settings.group_style_window_messages), 5_000))
        char_limit = int(max_chars or self.settings.group_style_max_chars)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                        MessageRow.is_self.is_(False),
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(window)
                )
            )
        rows.reverse()
        stats = new_style_stats()
        ai_profile: dict[str, Any] = {}
        evidence: list[str] = []
        echoes = self._sent_reply_texts(account_id, group_id)
        used = 0
        resolved_group_name = group_name
        for row in rows:
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            if self._is_bot_row(row):
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            if not resolved_group_name:
                resolved_group_name = row.group_name or ""
            update_style_stats(stats, text)
            patch = extract_ai_profile_patch(
                text,
                str(row.external_message_id or row.id),
                ai_aliases=self.settings.wechat_ai_aliases,
            )
            if patch:
                ai_profile = _merge_ai_profile(ai_profile, patch)
                evidence.extend(str(item) for item in patch.get("evidence_message_ids") or [])
            used += 1

        stats["message_count"] = used
        stats["target_low"], stats["target_high"] = style_target_band(stats)
        profile_text = build_group_style_profile(stats, max_chars=char_limit)
        now = utc_now()
        with self.Session.begin() as session:
            group_row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if group_row is None:
                group_row = GroupStyleProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=resolved_group_name or group_id,
                    profile_text="",
                    stats_json=json.dumps(new_style_stats(), ensure_ascii=False),
                    observed_count=0,
                    last_refresh_count=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(group_row)
                session.flush()
            previous_stats = _json_dict(group_row.stats_json)
            stats["seen_event_hashes"] = list(previous_stats.get("seen_event_hashes") or [])[-2_000:]
            stats["style_fit"] = previous_stats.get("style_fit") or {}
            if stats["style_fit"]:
                profile_text = build_group_style_profile(stats, max_chars=char_limit)
            group_row.profile_text = profile_text
            group_row.stats_json = json.dumps(stats, ensure_ascii=False, separators=(",", ":"))
            group_row.group_name = resolved_group_name or group_row.group_name
            # A rebuild itself counts as a refresh point.  Keep the lifetime
            # trigger counter at least at the window size so a newly created
            # row does not wait for a second full window.
            group_row.observed_count = max(int(group_row.observed_count or 0), used)
            group_row.last_refresh_count = int(group_row.observed_count or 0)
            group_row.last_refreshed_at = now
            group_row.updated_at = now

            ai_row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
            if ai_row is None:
                ai_row = AIProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=resolved_group_name or group_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(ai_row)
                session.flush()
            previous_ai = _json_dict(ai_row.profile_json)
            synthesized = previous_ai.get("synthesized")
            if isinstance(synthesized, dict) and synthesized:
                ai_profile["synthesized"] = synthesized
                ai_profile["synthesized_at"] = previous_ai.get("synthesized_at")
            ai_row.profile_json = json.dumps(ai_profile, ensure_ascii=False, separators=(",", ":"))
            ai_row.source_message_ids_json = json.dumps(
                _bounded_unique(evidence, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            ai_row.group_name = resolved_group_name or ai_row.group_name
            ai_row.updated_at = now
            # Old history entries carried a full snapshot of the previous
            # profile on every message (~126 KB in the audited group).  Rebase
            # them onto the compact delta shape during the rebuild.
            ai_row.history_json = json.dumps(
                [
                    _compact_ai_history_entry(entry)
                    for entry in _json_list(ai_row.history_json)[-100:]
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            observed_count = int(group_row.observed_count or 0)
            ai_result = self._profile_dict(
                ai_row.profile_json,
                scope={"account_id": account_id, "group_id": group_id},
                source_message_ids=ai_row.source_message_ids_json,
                updated_at=ai_row.updated_at,
                history_json=ai_row.history_json,
            )

        if self._profile_write_due(("group_style", account_id, group_id), force=force_remote):
            try:
                self.openviking.put_profile(
                    profile_kind="group_style",
                    scope={"account_id": account_id, "group_id": group_id},
                    profile={
                        "profile": profile_text,
                        "group_name": resolved_group_name,
                        "observed_count": observed_count,
                        "window_messages": used,
                        "updated_at": now.isoformat(),
                    },
                    source_message_ids=[],
                )
            except Exception as exc:  # pragma: no cover - defensive remote boundary
                log.warning("OpenViking group style write failed error=%s", type(exc).__name__)
        if self._profile_write_due(("ai", account_id, group_id), force=force_remote):
            try:
                self.openviking.put_profile(
                    profile_kind="ai",
                    scope={"account_id": account_id, "group_id": group_id},
                    profile=ai_result,
                    source_message_ids=list(ai_result.get("source_message_ids") or []),
                )
            except Exception as exc:  # pragma: no cover - defensive remote boundary
                log.warning("OpenViking AI profile write failed error=%s", type(exc).__name__)
        return {"profile": profile_text, "window_messages": used, "ai_profile": ai_result}

    def group_style_profile(self, account_id: str, group_id: str) -> str:
        """Return the latest fitted style text for one account/group scope."""

        with self.Session() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            return (row.profile_text or "").strip() if row else ""

    def group_style_snapshot(self, account_id: str, group_id: str) -> dict[str, object]:
        """Return dashboard-safe metadata without exposing the aggregate JSON."""

        with self.Session() as session:
            row = session.scalar(
                select(GroupStyleProfileRow).where(
                    GroupStyleProfileRow.account_id == account_id,
                    GroupStyleProfileRow.group_id == group_id,
                )
            )
            if row is None:
                return {
                    "enabled": True,
                    "ready": False,
                    "observed_count": 0,
                    "last_refresh_count": 0,
                    "profile": "",
                    "updated_at": None,
                    "last_refreshed_at": None,
                }
            return {
                "enabled": True,
                "ready": bool((row.profile_text or "").strip()),
                "group_name": row.group_name,
                "observed_count": int(row.observed_count or 0),
                "last_refresh_count": int(row.last_refresh_count or 0),
                "profile": (row.profile_text or "").strip(),
                "updated_at": _aware(row.updated_at).isoformat(),
                "last_refreshed_at": (
                    _aware(row.last_refreshed_at).isoformat() if row.last_refreshed_at else None
                ),
            }

    def user_profile(
        self,
        account_id: str,
        group_id: str,
        member_id: str,
    ) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                    UserProfileRow.member_id == member_id,
                )
            )
        if row is None:
            return {
                "scope": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "member_id": member_id,
                },
                "preferences": [],
                "interests": [],
                "tools": [],
                "expression_habits": [],
                "work_style": [],
                "constraints": [],
                "confirmed_info": [],
                "task_requirements": [],
                "corrections": [],
                "behavior": _empty_member_behavior(),
                "source_message_ids": [],
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={"account_id": row.account_id, "group_id": row.group_id, "member_id": row.member_id},
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def upsert_user_profile(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        member_name: str = "",
        patch: dict[str, object],
        source_message_ids: Iterable[str] = (),
    ) -> dict[str, object]:
        if _is_unknown_member(member_id) or not patch:
            return self.user_profile(account_id, group_id, member_id)
        now = utc_now()
        ids = [str(value) for value in source_message_ids if str(value).strip()]
        with self.Session.begin() as session:
            row = session.scalar(
                select(UserProfileRow).where(
                    UserProfileRow.account_id == account_id,
                    UserProfileRow.group_id == group_id,
                    UserProfileRow.member_id == member_id,
                )
            )
            created = row is None
            if row is None:
                row = UserProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    member_name=member_name,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            merged = _merge_profile(previous, patch)
            # Profile lists should represent the current active facts.  The
            # fact ledger retains superseded values for audit/history, so a
            # preference update does not leave the old value in the prompt.
            fact_field_types = {
                "preferences": "preference",
                "interests": "interest",
                "tools": "tool",
                "constraints": "constraint",
            }
            for field, memory_type in fact_field_types.items():
                if field not in patch:
                    continue
                active_rows = list(
                    session.scalars(
                        select(FactRow)
                        .where(
                            FactRow.account_id == account_id,
                            FactRow.group_id == group_id,
                            FactRow.member_id == member_id,
                            FactRow.memory_type == memory_type,
                            FactRow.status.in_(
                                ["candidate", "active", "confirmed", "inferred"]
                            ),
                            FactRow.valid_until.is_(None),
                        )
                        .order_by(FactRow.updated_at.asc(), FactRow.id.asc())
                    )
                )
                merged[field] = _bounded_unique(
                    [row.content for row in active_rows],
                    100,
                )
            history = _json_list(row.history_json)
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids[:20],
                    "changes": patch,
                    "previous": previous,
                }
            )
            row.profile_json = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-100:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 200),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.member_name = member_name or row.member_name
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        if self._profile_write_due(("user", account_id, group_id, member_id), force=created):
            try:
                self.openviking.put_profile(
                    profile_kind="user",
                    scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                    profile=result,
                    source_message_ids=list(result.get("source_message_ids") or []),
                )
            except Exception as exc:  # pragma: no cover - defensive remote boundary
                log.warning("OpenViking user profile write failed error=%s", type(exc).__name__)
        return result

    def ai_profile(self, account_id: str, group_id: str) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
        if row is None:
            return {
                "scope": {"account_id": account_id, "group_id": group_id},
                "common_topics": [],
                "common_vocabulary": [],
                "reply_length": "unknown",
                "formality": "unknown",
                "humor": "unknown",
                "technicality": "unknown",
                "interaction_patterns": [],
                "accepted_phrasing": [],
                "avoid_phrasing": [],
                "rules": [],
                "evidence_message_ids": [],
                "message_count": 0,
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={"account_id": row.account_id, "group_id": row.group_id},
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def observe_ai_profile(
        self,
        event: MessageEvent,
        message_id: str | None,
    ) -> dict[str, object]:
        """Update one aggregate group profile; no member facts are written."""

        patch = extract_ai_profile_patch(
            event.text,
            message_id,
            ai_aliases=self.settings.wechat_ai_aliases,
        )
        if not patch:
            return self.ai_profile(event.account_id, event.group_id)
        now = utc_now()
        ids = [str(message_id or event.external_message_id or "").strip()]
        ids = [value for value in ids if value]
        with self.Session.begin() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == event.account_id,
                    AIProfileRow.group_id == event.group_id,
                )
            )
            created = row is None
            if row is None:
                row = AIProfileRow(
                    account_id=event.account_id,
                    group_id=event.group_id,
                    group_name=event.group_name,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            profile = _merge_ai_profile(previous, patch)
            if isinstance(previous.get("synthesized"), dict) and previous["synthesized"]:
                profile["synthesized"] = previous["synthesized"]
                profile["synthesized_at"] = previous.get("synthesized_at")
            history = _json_list(row.history_json)
            # Keep an audit trail of *changes*, but not a full copy of the
            # previous profile on every message.  The old shape grew to tens of
            # thousands of characters and leaked straight into the prompt.
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids,
                    "changes": _compact_history_changes(patch),
                }
            )
            row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-100:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.group_name = event.group_name or row.group_name
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={"account_id": event.account_id, "group_id": event.group_id},
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        if self._profile_write_due(("ai", event.account_id, event.group_id), force=created):
            try:
                self.openviking.put_profile(
                    profile_kind="ai",
                    scope={"account_id": event.account_id, "group_id": event.group_id},
                    profile=result,
                    source_message_ids=list(result.get("source_message_ids") or []),
                )
            except Exception as exc:  # pragma: no cover - defensive remote boundary
                log.warning("OpenViking AI profile write failed error=%s", type(exc).__name__)
        return result

    def task_profile(
        self,
        account_id: str,
        group_id: str,
        task_id: str,
        member_id: str = "",
    ) -> dict[str, object]:
        with self.Session() as session:
            row = session.scalar(
                select(TaskProfileRow).where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                    TaskProfileRow.task_id == task_id,
                    TaskProfileRow.member_id == member_id,
                )
            )
        if row is None:
            return {
                "scope": {
                    "account_id": account_id,
                    "group_id": group_id,
                    "task_id": task_id,
                    **({"member_id": member_id} if member_id else {}),
                },
                "goal": "",
                "scope_text": "",
                "requirements": [],
                "decisions": [],
                "progress": [],
                "completed": [],
                "blockers": [],
                "people": [],
                "related_message_ids": [],
                "next_action": "",
                "updated_at": None,
                "history": [],
            }
        return self._profile_dict(
            row.profile_json,
            scope={
                "account_id": row.account_id,
                "group_id": row.group_id,
                "task_id": row.task_id,
                **({"member_id": row.member_id} if row.member_id else {}),
            },
            source_message_ids=row.source_message_ids_json,
            updated_at=row.updated_at,
            history_json=row.history_json,
        )

    def upsert_task_profile(
        self,
        *,
        account_id: str,
        group_id: str,
        task_id: str,
        patch: dict[str, object],
        source_message_ids: Iterable[str] = (),
        member_id: str = "",
    ) -> dict[str, object]:
        if not task_id.strip() or not patch:
            return self.task_profile(account_id, group_id, task_id, member_id)
        now = utc_now()
        ids = [str(value) for value in source_message_ids if str(value).strip()]
        with self.Session.begin() as session:
            row = session.scalar(
                select(TaskProfileRow).where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                    TaskProfileRow.task_id == task_id,
                    TaskProfileRow.member_id == member_id,
                )
            )
            if row is None:
                row = TaskProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    task_id=task_id,
                    member_id=member_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            previous = _json_dict(row.profile_json)
            merged = _merge_task_profile(previous, patch)
            merged["related_message_ids"] = _bounded_unique(
                _json_list(previous.get("related_message_ids")) + ids,
                500,
            )
            history = _json_list(row.history_json)
            history.append(
                {
                    "updated_at": now.isoformat(),
                    "source_message_ids": ids[:20],
                    "changes": patch,
                    "previous": previous,
                }
            )
            row.profile_json = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            row.history_json = json.dumps(history[-200:], ensure_ascii=False, separators=(",", ":"))
            row.source_message_ids_json = json.dumps(
                _bounded_unique(_json_list(row.source_message_ids_json) + ids, 500),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            row.updated_at = now
            result = self._profile_dict(
                row.profile_json,
                scope={
                    "account_id": account_id,
                    "group_id": group_id,
                    "task_id": task_id,
                    **({"member_id": member_id} if member_id else {}),
                },
                source_message_ids=row.source_message_ids_json,
                updated_at=row.updated_at,
                history_json=row.history_json,
            )
        try:
            self.openviking.put_profile(
                profile_kind="task",
                scope={
                    "account_id": account_id,
                    "group_id": group_id,
                    "task_id": task_id,
                    **({"member_id": member_id} if member_id else {}),
                },
                profile=result,
                source_message_ids=list(result.get("source_message_ids") or []),
            )
        except Exception as exc:  # pragma: no cover - defensive remote boundary
            log.warning("OpenViking task profile write failed error=%s", type(exc).__name__)
        return result

    def latest_task_id(self, account_id: str, group_id: str) -> str | None:
        with self.Session() as session:
            row = session.scalar(
                select(TaskProfileRow)
                .where(
                    TaskProfileRow.account_id == account_id,
                    TaskProfileRow.group_id == group_id,
                )
                .order_by(TaskProfileRow.updated_at.desc())
                .limit(1)
            )
            return row.task_id if row else None

    @staticmethod
    def _profile_dict(
        profile_json: str,
        *,
        scope: dict[str, str],
        source_message_ids: str,
        updated_at: datetime | None,
        history_json: str,
    ) -> dict[str, object]:
        profile = _json_dict(profile_json)
        profile["scope"] = scope
        profile["source_message_ids"] = _json_list(source_message_ids)
        profile["updated_at"] = _aware(updated_at).isoformat() if updated_at else None
        profile["history"] = _json_list(history_json)
        return profile

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
        query: str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> list[ConversationMessage]:
        """Return a bounded, oldest-first relevant window from one group.

        Raw message retention already supplies the durable conversation window.
        When a query or thread/task scope is supplied, unrelated rows are
        filtered before they enter the model context.  The method never
        crosses account or group boundaries and excludes the triggering row.
        """

        bounded_limit = max(0, min(int(limit), 50))
        bounded_chars = max(500, min(int(max_chars), 30_000))
        if bounded_limit == 0 or bounded_chars <= 0:
            return []
        with self.Session() as session:
            statement = (
                select(MessageRow)
                .where(
                    MessageRow.account_id == account_id,
                    MessageRow.group_id == group_id,
                )
                .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                .limit(max(bounded_limit + 1, 300 if str(query or "").strip() else bounded_limit + 1))
            )
            rows = list(session.scalars(statement))

        thread_value = str(thread_id or "").strip()
        task_value = str(task_id or "").strip()
        selected: list[ConversationMessage] = []
        used = 0
        for row in reversed(rows):
            if exclude_message_hash and row.message_hash == exclude_message_hash:
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text:
                continue
            if str(query or "").strip():
                same_thread = bool(thread_value and row.thread_id and row.thread_id == thread_value)
                same_task = bool(task_value and row.task_id and row.task_id == task_value)
                lexical_score = _context_similarity(query or "", text)
                has_image = bool(_json_list(getattr(row, "image_paths_json", "[]")))
                if not (same_thread or same_task or lexical_score >= _CONTEXT_SIMILARITY_THRESHOLD or has_image):
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
                    sender_id=row.member_id or "",
                    message_id=row.external_message_id or str(row.id),
                    thread_id=row.thread_id or "",
                    task_id=row.task_id or "",
                    image_paths=_json_list(getattr(row, "image_paths_json", "[]")),
                )
            )
            used += len(text)
            if len(selected) >= bounded_limit:
                break
        return selected

    def related_messages(
        self,
        *,
        account_id: str,
        group_id: str,
        query: str,
        limit: int = 8,
        max_chars: int = 3500,
        exclude_message_hash: str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> list[ConversationMessage]:
        """Retrieve same-group messages related to the current question.

        This is intentionally lexical and bounded; the semantic memory path
        handles long-term facts while raw messages stay a short-lived source
        context.  Every result retains sender and timestamp.
        """

        bounded_limit = max(0, min(int(limit), 30))
        bounded_chars = int(max_chars)
        thread_value = str(thread_id or "").strip()
        task_value = str(task_id or "").strip()
        if bounded_limit == 0 or bounded_chars <= 0:
            return []
        if not _context_tokens(query) and not thread_value and not task_value:
            return []
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(MessageRow.account_id == account_id, MessageRow.group_id == group_id)
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(300)
                )
            )
        scored: list[tuple[float, MessageRow]] = []
        for row in rows:
            if exclude_message_hash and row.message_hash == exclude_message_hash:
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text:
                continue
            same_thread = bool(thread_value and row.thread_id and row.thread_id == thread_value)
            same_task = bool(task_value and row.task_id and row.task_id == task_value)
            if same_thread:
                scope_bonus = 0.35
            elif same_task:
                scope_bonus = 0.3
            else:
                scope_bonus = 0.0
            lexical_score = _context_similarity(query, text)
            has_image = bool(_json_list(getattr(row, "image_paths_json", "[]")))
            if not (same_thread or same_task) and lexical_score < _CONTEXT_SIMILARITY_THRESHOLD and not has_image:
                continue
            age_days = max(0.0, (utc_now() - _aware(row.observed_at)).total_seconds() / 86400)
            scored.append((lexical_score + scope_bonus + math.exp(-age_days / 14.0) * 0.15, row))
        scored.sort(key=lambda item: (item[0], _aware(item[1].observed_at)), reverse=True)
        selected: list[ConversationMessage] = []
        used = 0
        for _, row in scored[:bounded_limit]:
            text = self._row_text(row)
            remaining = max(0, bounded_chars - used)
            if not text or remaining <= 0:
                break
            text = text[:remaining].rstrip()
            if not text:
                continue
            selected.append(
                ConversationMessage(
                    sender_name=row.member_name or "unknown",
                    text=text,
                    observed_at=_aware(row.observed_at),
                    sender_id=row.member_id or "",
                    message_id=row.external_message_id or str(row.id),
                    thread_id=row.thread_id or "",
                    task_id=row.task_id or "",
                    image_paths=_json_list(getattr(row, "image_paths_json", "[]")),
                )
            )
            used += len(text)
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
        """Legacy SQL fact insert kept for existing integrations/tests.

        New message ingestion uses :meth:`add_scoped_memory`, which adds the
        full memory contract, embeddings, conflict handling and OpenViking
        synchronization.  This method intentionally performs no network call
        so old offline callers remain deterministic.
        """

        now = fact.updated_at or utc_now()
        memory_id = fact.memory_id or self._memory_id(fact)
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
            memory_id=memory_id,
            scope_json=json.dumps(fact.scope, ensure_ascii=False, separators=(",", ":")),
            source_message_ids_json=json.dumps(fact.source_message_ids, ensure_ascii=False),
            task_id=fact.task_id,
            created_at=fact.observed_at,
            updated_at=now,
            valid_until=fact.valid_to,
            importance=max(0.0, min(1.0, fact.importance)),
            embedding_model=fact.embedding_model or "",
        )
        with self.Session.begin() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
            session.add(row)
            session.flush()
            return row.id

    def add_scoped_memory(self, fact: MemoryFact) -> int:
        """Insert one idempotent, scoped memory and index it semantically.

        Explicit corrections/preferences with the same ``memory_key`` mark the
        old active record ``superseded`` while preserving its row and source.
        The OpenViking call happens after the SQL transaction and is best
        effort; local retrieval remains available during a remote outage.
        """

        now = fact.updated_at or utc_now()
        memory_id = fact.memory_id or self._memory_id(fact)
        source_ids = [str(value) for value in fact.source_message_ids if str(value).strip()]
        if not source_ids and fact.source_event_id:
            source_ids = [str(fact.source_event_id)]
        # Avoid a second provider call when a replayed event is already in the
        # durable ledger.  The transaction below repeats this check for the
        # concurrent insert race.
        with self.Session() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
        embedding: list[float] | None = None
        embedding_model = fact.embedding_model or ""
        if self.embedder.configured:
            try:
                embedding, embedding_model = self.embedder.embed_one(fact.content)
            except EmbeddingError as exc:
                log.warning("embedding unavailable memory_id=%s error=%s", memory_id, str(exc))
        row_id: int
        with self.Session.begin() as session:
            existing = session.scalar(select(FactRow).where(FactRow.memory_id == memory_id))
            if existing:
                return existing.id
            memory_key = str(fact.metadata.get("memory_key") or "").strip()
            if memory_key and fact.status in {"active", "confirmed", "inferred"}:
                candidates = list(
                    session.scalars(
                        select(FactRow).where(
                            FactRow.account_id == fact.account_id,
                            FactRow.group_id == fact.group_id,
                            FactRow.member_id == fact.member_id,
                            FactRow.memory_type == fact.memory_type,
                            FactRow.status.in_(
                                ["candidate", "active", "confirmed", "inferred"]
                            ),
                            FactRow.valid_until.is_(None),
                        )
                    )
                )
                for previous in candidates:
                    metadata = _json_dict(previous.metadata_json)
                    if str(metadata.get("memory_key") or "") != memory_key:
                        continue
                    previous.status = "superseded"
                    previous.valid_to = now
                    previous.valid_until = now
                    previous.updated_at = now
            row = FactRow(
                account_id=fact.account_id,
                group_id=fact.group_id,
                member_id=fact.member_id,
                memory_type=fact.memory_type,
                content=fact.content,
                confidence=max(0.0, min(1.0, fact.confidence)),
                status=fact.status or "candidate",
                source_event_id=fact.source_event_id,
                observed_at=fact.observed_at,
                valid_from=fact.valid_from or fact.observed_at,
                valid_to=fact.valid_to,
                metadata_json=json.dumps(fact.metadata, ensure_ascii=False, separators=(",", ":")),
                embedding_json=json.dumps(embedding, ensure_ascii=False, separators=(",", ":"))
                if embedding is not None
                else None,
                memory_id=memory_id,
                scope_json=json.dumps(fact.scope, ensure_ascii=False, separators=(",", ":")),
                source_message_ids_json=json.dumps(source_ids, ensure_ascii=False),
                task_id=fact.task_id,
                created_at=now,
                updated_at=now,
                valid_until=fact.valid_to,
                importance=max(0.0, min(1.0, fact.importance)),
                embedding_model=embedding_model,
            )
            session.add(row)
            session.flush()
            row_id = row.id

        payload = {
            "memory_id": memory_id,
            "memory_type": fact.memory_type,
            "scope": fact.scope,
            "value": fact.content,
            "source_message_ids": source_ids,
            "confidence": fact.confidence,
            "importance": fact.importance,
            "status": fact.status,
            "embedding_model": embedding_model,
        }
        try:
            self.openviking.put_memory(payload)
        except Exception as exc:  # pragma: no cover - defensive remote boundary
            log.warning("OpenViking memory write failed memory_id=%s error=%s", memory_id, type(exc).__name__)
        return row_id

    @staticmethod
    def _memory_id(fact: MemoryFact) -> str:
        metadata_key = str(fact.metadata.get("memory_key") or "")
        source_ids = ",".join(sorted(str(value) for value in fact.source_message_ids if value))
        seed = "\x1f".join(
            [
                fact.account_id,
                fact.group_id,
                fact.member_id,
                fact.task_id or "",
                fact.memory_type,
                metadata_key or _normalize_text(fact.content),
                fact.source_event_id or "",
                source_ids,
            ]
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

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
                        FactRow.memory_type != "chat",
                        FactRow.status.in_(["active", "confirmed", "inferred", "candidate"]),
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
            recency = math.exp(
                -age_days
                / max(self._half_life_for_memory(row.memory_type, half_life_days), 0.01)
            )
            status_bonus = 0.15 if row.status == "confirmed" else 0.0
            if row.valid_until and _aware(row.valid_until) <= now:
                continue
            active_bonus = 0.10 if row.status == "active" else 0.0
            score = 0.50 * lexical + 0.25 * recency + 0.15 * row.confidence + status_bonus + active_bonus
            scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._fact_from_row(row) for _, row in scored[:limit]]

    def search_scoped_memories(
        self,
        *,
        account_id: str,
        group_id: str,
        member_id: str,
        query: str,
        half_life_days: float,
        task_id: str | None = None,
        limit: int = 8,
    ) -> list[MemoryFact]:
        """Hybrid lexical/vector retrieval with exact scope filtering."""

        now = utc_now()
        tokens = _tokens(query)
        query_vector: list[float] | None = None
        if self.embedder.configured:
            try:
                query_vector, _ = self.embedder.embed_one(query, is_query=True)
            except EmbeddingError as exc:
                log.warning("embedding query unavailable error=%s", str(exc))
        with self.Session() as session:
            conditions = [
                FactRow.account_id == account_id,
                FactRow.group_id == group_id,
                FactRow.memory_type != "chat",
                FactRow.status.in_(["active", "confirmed", "inferred", "candidate"]),
                FactRow.valid_until.is_(None),
            ]
            # Member-scoped rows are always exact.  Group/task memories use an
            # empty member id and may be included for the current task.
            if task_id:
                conditions.append(
                    (FactRow.member_id == member_id)
                    | ((FactRow.member_id == "") & (FactRow.task_id == task_id))
                )
            else:
                conditions.append(FactRow.member_id == member_id)
            rows = list(
                session.scalars(select(FactRow).where(*conditions).order_by(FactRow.updated_at.desc()).limit(500))
            )

        scored: list[tuple[float, FactRow]] = []
        for row in rows:
            if row.valid_until and _aware(row.valid_until) <= now:
                continue
            lexical = 0.0
            words = _tokens(row.content)
            if tokens:
                lexical = len(tokens & words) / max(1, len(tokens))
            semantic = 0.0
            if query_vector and row.embedding_json:
                try:
                    semantic = _cosine(query_vector, json.loads(row.embedding_json))
                except (TypeError, ValueError, json.JSONDecodeError):
                    semantic = 0.0
            age_days = max(0.0, (now - _aware(row.observed_at)).total_seconds() / 86400)
            recency = math.exp(
                -age_days
                / max(self._half_life_for_memory(row.memory_type, half_life_days), 0.01)
            )
            status_bonus = 0.12 if row.status == "active" else 0.08 if row.status == "confirmed" else 0.0
            score = (
                0.42 * lexical
                + 0.36 * semantic
                + 0.12 * recency
                + 0.06 * row.confidence
                + 0.04 * row.importance
                + status_bonus
            )
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        result = [self._fact_from_row(row) for _, row in scored[: max(1, limit)]]
        existing_ids = {fact.memory_id for fact in result if fact.memory_id}
        try:
            remote_hits = self.openviking.search(
                query,
                account_id=account_id,
                group_id=group_id,
                member_id=member_id,
                task_id=task_id or "",
                limit=limit,
            )
        except Exception:  # pragma: no cover - defensive remote boundary
            remote_hits = []
        for hit in remote_hits:
            if hit.memory_id in existing_ids or not hit.value:
                continue
            content = hit.value
            # Historical raw messages were mirrored before text normalization.
            # Sanitize them on read so old prefixes/XML cannot leak into the
            # prompt even when the remote corpus has not been rewritten yet.
            if hit.memory_type == "raw_message":
                content = normalize_chat_text(
                    content,
                    sender_id=str(hit.scope.get("member_id") or ""),
                    ai_aliases=self.settings.wechat_ai_aliases,
                ).text
                if not content:
                    continue
            result.append(
                MemoryFact(
                    account_id=account_id,
                    group_id=group_id,
                    member_id=member_id,
                    task_id=task_id,
                    memory_type="openviking",
                    content=content,
                    confidence=max(0.0, min(1.0, hit.score or 0.5)),
                    importance=0.5,
                    status="active",
                    source_message_ids=list(hit.source_message_ids),
                    memory_id=hit.memory_id,
                    metadata={"source": "openviking", "uri": hit.uri, "scope": hit.scope},
                )
            )
            existing_ids.add(hit.memory_id)
            if len(result) >= max(1, limit):
                break
        return result[: max(1, limit)]

    def backfill_normalized_text(self, *, batch_size: int = 500, limit: int | None = None) -> int:
        """Compute ``text_normalized`` for rows created before this feature."""

        updated = 0
        bounded_batch = max(1, min(int(batch_size), 5_000))
        version_marker = f'%"normalizer_version":{normalizer_version}%'
        while True:
            with self.Session.begin() as session:
                rows = list(
                    session.scalars(
                        select(MessageRow)
                        .where(
                            or_(
                                MessageRow.text_meta_json.is_(None),
                                MessageRow.text_meta_json == "{}",
                                ~MessageRow.text_meta_json.like(version_marker),
                            )
                        )
                        .order_by(MessageRow.id.asc())
                        .limit(bounded_batch)
                    )
                )
                if not rows:
                    break
                for row in rows:
                    normalized = normalize_chat_text(
                        row.text or "",
                        sender_id=row.member_id or "",
                        sender_name=row.member_name or "",
                        ai_aliases=self.settings.wechat_ai_aliases,
                    )
                    row.text_normalized = normalized.text
                    row.text_kind = normalized.kind
                    row.text_meta_json = json.dumps(
                        {
                            "prefix_sender": normalized.prefix_sender,
                            "addressed_as": normalized.addressed_as,
                            "quoted_text": normalized.quoted_text,
                            "sticker_count": normalized.sticker_count,
                            "image_count": normalized.image_count,
                            "truncated": normalized.truncated,
                            "normalizer_version": normalizer_version,
                            "backfilled": True,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    updated += 1
            if limit is not None and updated >= int(limit):
                break
            if len(rows) < bounded_batch:
                break
        return updated

    def archive_legacy_chat_facts(self) -> int:
        """Retire pre-normalization ``memory_type=chat`` rows.

        New code never creates them.  Old rows contain a raw ``wxid:\\n`` or
        XML payload and would otherwise keep resurfacing as "memory".
        """

        now = utc_now()
        with self.Session.begin() as session:
            result = session.execute(
                update(FactRow)
                .where(
                    FactRow.memory_type == "chat",
                    FactRow.status.in_(["active", "confirmed", "candidate", "inferred"]),
                    FactRow.valid_until.is_(None),
                )
                .values(status="archived", valid_until=now, updated_at=now)
            )
            return int(result.rowcount or 0)

    def group_scopes(self) -> list[tuple[str, str, str]]:
        with self.Session() as session:
            rows = list(
                session.execute(
                    select(
                        MessageRow.account_id,
                        MessageRow.group_id,
                        func.max(MessageRow.group_name),
                    ).group_by(MessageRow.account_id, MessageRow.group_id)
                )
            )
        return [(str(account), str(group), str(name or "")) for account, group, name in rows]

    def profile_window_messages(
        self,
        account_id: str,
        group_id: str,
        *,
        limit: int = 120,
    ) -> list[tuple[str, str]]:
        """Normalized ``(member_name, text)`` pairs for offline synthesis."""

        bounded = max(10, min(int(limit), 1_000))
        echoes = self._sent_reply_texts(account_id, group_id)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(bounded * 2)
                )
            )
        rows.reverse()
        items: list[tuple[str, str]] = []
        for row in rows:
            if self._is_bot_row(row):
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            items.append((row.member_name or "群友", text))
            if len(items) >= bounded:
                break
        return items

    def save_synthesized_group_profile(
        self,
        account_id: str,
        group_id: str,
        payload: dict[str, object],
        *,
        group_name: str = "",
    ) -> bool:
        if not payload:
            return False
        now = utc_now()
        with self.Session.begin() as session:
            row = session.scalar(
                select(AIProfileRow).where(
                    AIProfileRow.account_id == account_id,
                    AIProfileRow.group_id == group_id,
                )
            )
            if row is None:
                row = AIProfileRow(
                    account_id=account_id,
                    group_id=group_id,
                    group_name=group_name or group_id,
                    profile_json="{}",
                    history_json="[]",
                    source_message_ids_json="[]",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            profile = _json_dict(row.profile_json)
            profile["synthesized"] = payload
            profile["synthesized_at"] = now.isoformat()
            row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
            row.group_name = group_name or row.group_name
            row.updated_at = now

            notes = payload.get("member_notes")
            if isinstance(notes, dict):
                members = list(
                    session.scalars(
                        select(UserProfileRow).where(
                            UserProfileRow.account_id == account_id,
                            UserProfileRow.group_id == group_id,
                        )
                    )
                )
                for member in members:
                    note = notes.get(member.member_name or "")
                    if not isinstance(note, str) or not note.strip():
                        continue
                    member_profile = _json_dict(member.profile_json)
                    member_profile["synthesized_note"] = note.strip()[:80]
                    member.profile_json = json.dumps(
                        member_profile, ensure_ascii=False, separators=(",", ":")
                    )
                    member.updated_at = now

        # Synthesis is infrequent (once per batch), so always mirror the
        # semantic payload instead of waiting for the next debounce window.
        if self._profile_write_due(("ai", account_id, group_id), force=True):
            try:
                self.openviking.put_profile(
                    profile_kind="ai",
                    scope={"account_id": account_id, "group_id": group_id},
                    profile=profile,
                )
            except Exception as exc:  # pragma: no cover - remote boundary
                log.warning("OpenViking synthesized profile write failed error=%s", type(exc).__name__)
        return True

    def rebuild_member_profiles(
        self,
        account_id: str,
        group_id: str,
        *,
        window_messages: int | None = None,
        force_remote: bool = False,
    ) -> int:
        """Rebuild member behavior counters from normalized window messages.

        Only the soft ``behavior`` block is recomputed; explicit facts
        (preferences/interests/tools/constraints) remain owned by the fact
        ledger and are left untouched.
        """

        window = max(50, min(int(window_messages or self.settings.group_style_window_messages), 5_000))
        echoes = self._sent_reply_texts(account_id, group_id)
        with self.Session() as session:
            rows = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.account_id == account_id,
                        MessageRow.group_id == group_id,
                    )
                    .order_by(MessageRow.observed_at.desc(), MessageRow.id.desc())
                    .limit(window)
                )
            )
        rows.reverse()
        per_member: dict[str, dict[str, object]] = {}
        for row in rows:
            if self._is_bot_row(row):
                continue
            if (getattr(row, "text_kind", "") or "") == "system":
                continue
            if _is_unknown_member(row.member_id):
                continue
            text = self._row_text(row)
            if not text or self._looks_like_echo(text, echoes) or self._looks_like_bot_output(text, row.member_id):
                continue
            patch = extract_member_profile_patch(
                text,
                str(row.external_message_id or row.id),
                ai_aliases=self.settings.wechat_ai_aliases,
            )
            if not patch:
                continue
            bucket = per_member.setdefault(
                str(row.member_id),
                {"member_name": row.member_name or "", "behavior": {}, "evidence": []},
            )
            behavior_patch = patch.get("behavior") if isinstance(patch.get("behavior"), dict) else {}
            bucket["behavior"] = _merge_member_behavior(
                bucket["behavior"] if isinstance(bucket.get("behavior"), dict) else {},
                behavior_patch,
            )
            bucket["evidence"].extend(str(item) for item in behavior_patch.get("evidence_message_ids") or [])

        now = utc_now()
        for member_id, bucket in per_member.items():
            with self.Session.begin() as session:
                row = session.scalar(
                    select(UserProfileRow).where(
                        UserProfileRow.account_id == account_id,
                        UserProfileRow.group_id == group_id,
                        UserProfileRow.member_id == member_id,
                    )
                )
                created = row is None
                if row is None:
                    row = UserProfileRow(
                        account_id=account_id,
                        group_id=group_id,
                        member_id=member_id,
                        member_name=str(bucket.get("member_name") or ""),
                        profile_json="{}",
                        history_json="[]",
                        source_message_ids_json="[]",
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                profile = _json_dict(row.profile_json)
                behavior = bucket.get("behavior") if isinstance(bucket.get("behavior"), dict) else {}
                profile["behavior"] = behavior
                profile["expression_habits"] = list(behavior.get("expression_habits") or [])
                row.profile_json = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                row.source_message_ids_json = json.dumps(
                    _bounded_unique([str(item) for item in bucket.get("evidence") or []], 500),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                row.member_name = str(bucket.get("member_name") or "") or row.member_name
                row.updated_at = now
                result = self._profile_dict(
                    row.profile_json,
                    scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                    source_message_ids=row.source_message_ids_json,
                    updated_at=row.updated_at,
                    history_json=row.history_json,
                )
            if self._profile_write_due(("user", account_id, group_id, member_id), force=force_remote):
                try:
                    self.openviking.put_profile(
                        profile_kind="user",
                        scope={"account_id": account_id, "group_id": group_id, "member_id": member_id},
                        profile=result,
                        source_message_ids=list(result.get("source_message_ids") or []),
                    )
                except Exception as exc:  # pragma: no cover - defensive remote boundary
                    log.warning("OpenViking user profile write failed error=%s", type(exc).__name__)
        return len(per_member)

    def rebuild_all_group_profiles(self, *, window_messages: int | None = None) -> list[dict[str, object]]:
        with self.Session() as session:
            rows = list(
                session.execute(
                    select(
                        MessageRow.account_id,
                        MessageRow.group_id,
                        func.max(MessageRow.group_name),
                    )
                    .where(MessageRow.is_self.is_(False))
                    .group_by(MessageRow.account_id, MessageRow.group_id)
                )
            )
        rebuilt: list[dict[str, object]] = []
        for account_id, group_id, group_name in rows:
            result = self.rebuild_group_profiles(
                str(account_id),
                str(group_id),
                group_name=str(group_name or ""),
                window_messages=window_messages,
                force_remote=False,
            )
            result["members_rebuilt"] = self.rebuild_member_profiles(
                str(account_id),
                str(group_id),
                window_messages=window_messages,
                force_remote=False,
            )
            rebuilt.append(result)
        return rebuilt

    def rebuild_memory(self, *, window_messages: int | None = None) -> dict[str, object]:
        """One-shot maintenance: normalize history, retire legacy facts, rebuild profiles."""

        normalized = self.backfill_normalized_text()
        archived = self.archive_legacy_chat_facts()
        rebuilt = self.rebuild_all_group_profiles(window_messages=window_messages)
        return {
            "messages_normalized": normalized,
            "legacy_chat_facts_archived": archived,
            "groups_rebuilt": len(rebuilt),
            "member_profiles_rebuilt": sum(int(item.get("members_rebuilt") or 0) for item in rebuilt),
            "profiles": [
                {
                    "window_messages": item.get("window_messages"),
                    "members": item.get("members_rebuilt"),
                    "profile_chars": len(str(item.get("profile") or "")),
                }
                for item in rebuilt
            ],
        }

    def _half_life_for_memory(self, memory_type: str, fallback: float) -> float:
        """Use the configured decay family while preserving custom callers."""

        if memory_type in {"interest", "tool"}:
            return self.settings.half_life_for("interest")
        if memory_type in {"preference", "constraint", "fact", "confirmed_info"}:
            return self.settings.half_life_for("fact")
        return fallback

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
        metadata = _json_dict(row.metadata_json)
        source_ids = _json_list(row.source_message_ids_json)
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
            valid_to=_aware(row.valid_until or row.valid_to) if (row.valid_until or row.valid_to) else None,
            metadata=metadata,
            memory_id=row.memory_id,
            task_id=row.task_id,
            source_message_ids=[str(value) for value in source_ids],
            importance=float(row.importance or 0.5),
            embedding_model=row.embedding_model or "",
            updated_at=_aware(row.updated_at) if row.updated_at else None,
        )


def _compact_ai_history_entry(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        return {}
    changes = entry.get("changes") if isinstance(entry.get("changes"), dict) else {}
    return {
        "updated_at": entry.get("updated_at"),
        "source_message_ids": [str(item) for item in (entry.get("source_message_ids") or [])[:5]],
        "changes": _compact_history_changes(changes),
    }


def _compact_history_changes(patch: dict[str, object]) -> dict[str, object]:
    """Keep a small audit delta instead of a full previous-profile snapshot."""

    keep: dict[str, object] = {}
    scalar_fields = (
        "message_count",
        "char_count",
        "short_message_count",
        "question_count",
        "exclamation_count",
        "technical_count",
        "humor_count",
        "formal_count",
        "sticker_count",
        "reply_length",
        "formality",
        "humor",
        "technicality",
    )
    for field in scalar_fields:
        if field in patch:
            keep[field] = patch[field]
    for field in ("common_vocabulary", "accepted_phrasing", "rules", "interaction_patterns"):
        if field in patch:
            keep[field] = list(patch[field] or [])[:8]
    topic_counts = patch.get("topic_counts")
    if isinstance(topic_counts, dict):
        keep["topic_counts"] = dict(list(topic_counts.items())[:8])
    evidence = patch.get("evidence_message_ids")
    if isinstance(evidence, list):
        keep["evidence_message_ids"] = evidence[:5]
    return keep


def _json_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _bounded_unique(values: Iterable[object], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result[-max(1, int(limit)) :]


def _is_unknown_member(value: object) -> bool:
    return str(value or "").strip().casefold() in {
        "",
        "unknown",
        "我",
        "自己",
        "me",
        "you",
        "you (me)",
    }


def _empty_member_behavior() -> dict[str, object]:
    """Return the stable shape exposed to the prompt and dashboard.

    The counters are deliberately descriptive rather than psychological.  A
    single message can contribute evidence, but inferred topics and habits are
    only surfaced after repeated observations in the same group/member scope.
    """

    return {
        "message_count": 0,
        "char_count": 0,
        "short_message_count": 0,
        "question_count": 0,
        "exclamation_count": 0,
        "emoji_count": 0,
        "sticker_count": 0,
        "image_count": 0,
        "linebreak_count": 0,
        "average_message_length": 0.0,
        "marker_counts": {},
        "ending_counts": {},
        "address_counts": {},
        "topic_counts": {},
        "vocabulary": {},
        "common_topics": [],
        "inferred_interests": [],
        "expression_habits": [],
        "evidence_message_ids": [],
    }


def _counter(value: object) -> Counter[str]:
    if not isinstance(value, dict):
        return Counter()
    result: Counter[str] = Counter()
    for key, raw in value.items():
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count > 0:
            result[str(key)] = count
    return result


def _merge_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    list_fields = {
        "preferences",
        "interests",
        "tools",
        "expression_habits",
        "work_style",
        "constraints",
        "confirmed_info",
        "task_requirements",
        "corrections",
    }
    for key, value in patch.items():
        if key == "behavior" and isinstance(value, dict):
            merged["behavior"] = _merge_member_behavior(
                previous.get("behavior") if isinstance(previous.get("behavior"), dict) else {},
                value,
            )
            # Keep the existing top-level prompt field useful for clients that
            # predate the nested behavior contract.
            merged["expression_habits"] = list(merged["behavior"].get("expression_habits") or [])
            continue
        if key in list_fields:
            old = [str(item) for item in merged.get(key, []) if str(item).strip()]
            new_values = value if isinstance(value, (list, tuple, set)) else [value]
            merged[key] = _bounded_unique(old + [str(item) for item in new_values], 100)
        else:
            merged[key] = value
    return merged


def _merge_member_behavior(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, object]:
    """Accumulate soft behavioral evidence for one group/member scope."""

    merged = _empty_member_behavior()
    numeric_fields = {
        "message_count",
        "char_count",
        "short_message_count",
        "question_count",
        "exclamation_count",
        "emoji_count",
        "sticker_count",
        "image_count",
        "linebreak_count",
    }
    for field in numeric_fields:
        try:
            merged[field] = int(previous.get(field) or 0) + int(patch.get(field) or 0)
        except (TypeError, ValueError):
            merged[field] = int(previous.get(field) or 0)

    for field in ("marker_counts", "ending_counts", "address_counts", "topic_counts", "vocabulary"):
        counts = _counter(previous.get(field))
        counts.update(_counter(patch.get(field)))
        merged[field] = dict(counts.most_common(80 if field == "vocabulary" else 30))

    evidence = _bounded_unique(
        _json_list(previous.get("evidence_message_ids"))
        + _json_list(patch.get("evidence_message_ids")),
        500,
    )
    merged["evidence_message_ids"] = evidence
    message_count = max(0, int(merged["message_count"]))
    char_count = max(0, int(merged["char_count"]))
    merged["average_message_length"] = round(char_count / max(1, message_count), 1)

    topic_counts = _counter(merged["topic_counts"])
    # Two observations are the minimum evidence for a durable-looking topic;
    # with a larger corpus, a 5% share is also required.  These are still soft
    # signals and are labelled as inferred interests in the prompt payload.
    min_topic_count = (
        1
        if message_count < 40
        else max(2, int(message_count * 0.05))
    )
    repeated_topics = [
        topic for topic, count in topic_counts.most_common(12) if count >= min_topic_count
    ]
    merged["common_topics"] = repeated_topics[:8]
    merged["inferred_interests"] = repeated_topics[:5]

    habits: list[str] = []
    short_ratio = int(merged["short_message_count"]) / max(1, message_count)
    question_ratio = int(merged["question_count"]) / max(1, message_count)
    exclamation_ratio = int(merged["exclamation_count"]) / max(1, message_count)
    emoji_ratio = int(merged["emoji_count"]) / max(1, message_count)
    if short_ratio >= 0.55:
        habits.append("short_turns")
    elif float(merged["average_message_length"]) >= 80:
        habits.append("detailed_turns")
    if question_ratio >= 0.35:
        habits.append("frequent_questions")
    if exclamation_ratio >= 0.35:
        habits.append("expressive_punctuation")
    if emoji_ratio >= 0.25:
        habits.append("emoji_frequent")
    marker_counts = _counter(merged["marker_counts"])
    ending_counts = _counter(merged["ending_counts"])
    address_counts = _counter(merged["address_counts"])
    for marker, count in marker_counts.most_common(4):
        if count >= min_topic_count:
            habits.append(f"uses_{marker}")
    for marker, count in ending_counts.most_common(3):
        if count >= min_topic_count:
            habits.append(f"ends_with_{marker}")
    for marker, count in address_counts.most_common(3):
        if count >= min_topic_count:
            habits.append(f"addresses_{marker}")
    merged["expression_habits"] = _bounded_unique(habits, 20)
    return merged


def _merge_ai_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    message_count = int(previous.get("message_count") or 0) + int(patch.get("message_count") or 0)
    char_count = int(previous.get("char_count") or 0) + int(patch.get("char_count") or 0)
    short_count = int(previous.get("short_message_count") or 0) + int(patch.get("short_message_count") or 0)
    question_count = int(previous.get("question_count") or 0) + int(patch.get("question_count") or 0)
    exclamation_count = int(previous.get("exclamation_count") or 0) + int(patch.get("exclamation_count") or 0)
    technical_count = int(previous.get("technical_count") or 0) + int(patch.get("technical_count") or 0)
    humor_count = int(previous.get("humor_count") or 0) + int(patch.get("humor_count") or 0)
    formal_count = int(previous.get("formal_count") or 0) + int(patch.get("formal_count") or 0)
    sticker_count = int(previous.get("sticker_count") or 0) + int(patch.get("sticker_count") or 0)
    vocabulary = Counter(_json_dict(previous.get("vocabulary")))
    vocabulary.update(Counter(_json_dict(patch.get("vocabulary"))))
    topic_counts = Counter(_json_dict(previous.get("topic_counts")))
    topic_counts.update(Counter(_json_dict(patch.get("topic_counts"))))
    evidence = _bounded_unique(
        _json_list(previous.get("evidence_message_ids")) + _json_list(patch.get("evidence_message_ids")),
        500,
    )
    average_length = char_count / max(1, message_count)
    short_ratio = short_count / max(1, message_count)
    if short_ratio >= 0.6 or average_length <= 24:
        reply_length = "short"
    elif average_length >= 80:
        reply_length = "detailed"
    else:
        reply_length = "medium"
    formality_ratio = formal_count / max(1, message_count)
    humor_ratio = humor_count / max(1, message_count)
    technical_ratio = technical_count / max(1, message_count)
    common_topics = list(dict(topic_counts.most_common(10)).keys())
    interaction_patterns: list[str] = []
    if question_count / max(1, message_count) >= 0.4:
        interaction_patterns.append("frequent_questions")
    if exclamation_count / max(1, message_count) >= 0.35:
        interaction_patterns.append("expressive_punctuation")
    if short_ratio >= 0.6 or average_length <= 24:
        interaction_patterns.append("short_turns")
    elif average_length >= 80:
        interaction_patterns.append("detailed_turns")
    accepted_markers = [
        value
        for value in ("哈哈", "笑死", "狗头", "谢谢", "大佬", "兄弟", "家人们")
        if value in vocabulary
    ]
    merged.update(
        {
            "message_count": message_count,
            "char_count": char_count,
            "short_message_count": short_count,
            "question_count": question_count,
            "exclamation_count": exclamation_count,
            "technical_count": technical_count,
            "humor_count": humor_count,
            "formal_count": formal_count,
            "sticker_count": sticker_count,
            "vocabulary": dict(vocabulary.most_common(30)),
            "common_vocabulary": list(dict(vocabulary.most_common(10)).keys()),
            "topic_counts": dict(topic_counts.most_common(20)),
            "common_topics": common_topics,
            "interaction_patterns": interaction_patterns,
            "accepted_phrasing": accepted_markers,
            "avoid_phrasing": list(merged.get("avoid_phrasing") or []),
            "rules": list(merged.get("rules") or []),
            "reply_length": reply_length,
            "formality": "formal" if formality_ratio >= 0.35 else "casual" if formality_ratio <= 0.1 else "mixed",
            "humor": "frequent" if humor_ratio >= 0.25 else "occasional" if humor_ratio else "low",
            "technicality": "technical" if technical_ratio >= 0.35 else "general",
            "evidence_message_ids": evidence,
        }
    )
    return merged


def _merge_task_profile(previous: dict[str, Any], patch: dict[str, object]) -> dict[str, Any]:
    merged = dict(previous)
    mapping = {
        "scope": "scope_text",
        "next_action": "next_action",
        "goal": "goal",
    }
    list_fields = {"requirements", "decisions", "progress", "completed", "blockers", "people"}
    for key, value in patch.items():
        target = mapping.get(key, key)
        if target in list_fields:
            old = [str(item) for item in merged.get(target, []) if str(item).strip()]
            new_values = value if isinstance(value, (list, tuple, set)) else [value]
            merged[target] = _bounded_unique(old + [str(item) for item in new_values], 100)
        else:
            merged[target] = value
    return merged


def _cosine(left: list[float], right: object) -> float:
    if not isinstance(right, list) or len(left) != len(right) or not left:
        return 0.0
    try:
        values = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    dot = sum(a * b for a, b in zip(left, values))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in values))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


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


_CONTEXT_SIMILARITY_THRESHOLD = 0.10


def _context_tokens(text: str) -> set[str]:
    """Extract meaningful context terms without treating single CJK chars as hits."""

    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_+#.-]+", normalized)
        if len(token) >= 2
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(run) < 2:
            continue
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _context_similarity(query: str, text: str) -> float:
    """Return a bounded lexical similarity for short raw-message context."""

    query_terms = _context_tokens(query)
    message_terms = _context_tokens(text)
    if not query_terms or not message_terms:
        return 0.0
    overlap = len(query_terms & message_terms)
    if overlap == 0:
        return 0.0
    return overlap / math.sqrt(len(query_terms) * len(message_terms))
