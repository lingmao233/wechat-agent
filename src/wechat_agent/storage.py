from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings
from .models import MemoryFact, MessageEvent, OutboundAction, utc_now


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


class Storage:
    def __init__(self, settings: Settings):
        url = settings.database_url
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, future=True)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.settings = settings

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @staticmethod
    def hash_event(event: MessageEvent) -> str:
        raw = "\x1f".join(
            [
                event.account_id,
                event.group_id,
                event.member_id,
                event.text,
                event.observed_at.isoformat(),
            ]
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


def _tokens(text: str) -> set[str]:
    """Small dependency-free tokenizer for Chinese and whitespace text."""
    normalized = text.strip().lower()
    words = {token for token in normalized.split() if token}
    cjk = [char for char in normalized if "\u4e00" <= char <= "\u9fff"]
    words.update(cjk)
    words.update("".join(cjk[index : index + 2]) for index in range(max(0, len(cjk) - 1)))
    return words
