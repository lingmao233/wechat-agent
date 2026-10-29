"""Export local group chat into style/behavior datasets.

This module deliberately does not call a model or upload anything.  It turns
the SQL corpus into two local JSONL files:

* ``style_corpus.jsonl``: one row per member message, useful for style
  adaptation / continued training;
* ``behavior_pairs.jsonl``: prior context plus the member's actual reply and a
  light-weight behavior label, useful for building SFT or evaluation pairs.

The target response is a *group member's* message, not an AI reply.  Training
on it directly teaches "how this group talks", not "how the whale-girl persona
should answer".  A persona-conditioned SFT set should rewrite these targets or
use accepted live replies; the manifest records this caveat explicitly.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .storage import MessageRow, Storage

__all__ = [
    "classify_behavior",
    "export_training_data",
    "build_training_examples",
]

_COMFORT = (
    "抱抱",
    "别难过",
    "别想太多",
    "没事的",
    "会好的",
    "辛苦了",
    "心疼",
    "怎么了",
    "加油",
    "我在",
    "别哭",
    "摸摸",
)
_PRANK = (
    "骗你的",
    "骗你",
    "逗你的",
    "逗你玩",
    "上钩",
    "钓鱼",
    "不告诉你",
    "吓你的",
    "整你的",
    "假的啦",
)
_TEASE = (
    "你妹",
    "傻子",
    "笨蛋",
    "是不是傻",
    "就这",
    "你不行",
    "菜鸡",
    "狗东西",
    "滚",
    "闭嘴",
    "欠揍",
)
_JOKE = (
    "哈哈",
    "笑死",
    "绷不住",
    "离谱",
    "整活",
    "乐死",
    "绝了",
    "梗",
)
_QUESTION_PREFIXES = (
    "怎么",
    "为什么",
    "为啥",
    "什么",
    "啥",
    "哪",
    "谁",
    "多少",
    "能不能",
    "可以吗",
    "有没有",
)
_SKIP_PLACEHOLDERS = {
    "[表情]",
    "[图片]",
    "[视频]",
    "[语音]",
    "[文件]",
    "[分享]",
    "[链接]",
    "[位置]",
}
_BOT_TEXT_MARKERS = (
    "我是群里的 ai 助手",
    "我是群里的ai助手",
    "我是 ai 助手",
    "我是ai助手",
    "有什么想问的",
    "直接说就行",
)


def classify_behavior(text: str) -> str:
    """Light-weight label used to balance the exported dataset.

    The labels are intentionally conservative and transparent.  They are not
    ground truth; they are a cheap prior for sampling/eyeballing and can be
    replaced by a model/human label later without changing the file format.
    """

    value = " ".join(str(text or "").split()).strip()
    if not value:
        return "ordinary"
    if any(token in value for token in _PRANK):
        return "prank"
    if any(token in value for token in _COMFORT):
        return "comfort"
    if any(token in value for token in _TEASE):
        return "tease"
    if any(token in value for token in _JOKE):
        return "joke"
    if "?" in value or "？" in value or value.startswith(_QUESTION_PREFIXES):
        return "question"
    return "ordinary"


def _is_bot(_member_id: str, member_name: str, storage: Storage) -> bool:
    # ``unknown`` is an unresolved *member* identity in the UI, not the bot.
    # Keep those rows in the group style corpus; only configured bot names and
    # explicit self rows are excluded.
    names = {
        str(storage.settings.wechat_self_name or "").strip().casefold(),
        *(str(alias or "").strip().casefold() for alias in storage.settings.wechat_ai_aliases),
    }
    names.discard("")
    return bool(member_name) and member_name.strip().casefold() in names


def _row_text(storage: Storage, row: MessageRow) -> str:
    value = (getattr(row, "text_normalized", "") or "").strip()
    if value:
        return value
    return storage._row_text(row).strip()


def _iter_messages(storage: Storage) -> list[MessageRow]:
    with storage.Session() as session:
        return list(
            session.scalars(
                select(MessageRow).order_by(
                    MessageRow.account_id, MessageRow.group_id, MessageRow.id
                )
            )
        )


def _safe_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_training_examples(
    storage: Storage,
    *,
    window_messages: int = 6,
    anonymize: bool = True,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return ``(style_rows, pair_rows, manifest)`` without writing files."""

    window = max(1, min(int(window_messages), 30))
    since_utc = _as_utc(since)
    until_utc = _as_utc(until)
    style_rows: list[dict[str, Any]] = []
    pair_rows: list[dict[str, Any]] = []
    behavior_counts: Counter[str] = Counter()
    groups: dict[tuple[str, str], str] = {}

    contexts: dict[tuple[str, str], list[str]] = {}
    labels: dict[tuple[str, str], dict[str, str]] = {}

    for row in _iter_messages(storage):
        observed = _as_utc(row.observed_at)
        if since_utc is not None and (observed is None or observed < since_utc):
            continue
        if until_utc is not None and (observed is None or observed >= until_utc):
            continue
        key = (row.account_id, row.group_id)
        if key not in groups:
            groups[key] = f"群{len(groups) + 1}"
        group_label = groups[key]
        member_key = str(row.member_id or "").strip()
        if anonymize:
            mapping = labels.setdefault(key, {})
            if member_key not in mapping:
                index = len(mapping)
                suffix = ""
                while index >= 26:
                    suffix = chr(ord("A") + index % 26) + suffix
                    index = index // 26 - 1
                mapping[member_key] = f"群友{chr(ord('A') + index)}{suffix}"
            speaker = mapping[member_key]
        else:
            speaker = str(row.member_name or row.member_id or "群友").strip() or "群友"

        text = _row_text(storage, row)
        if (
            not text
            or text in _SKIP_PLACEHOLDERS
            or row.text_kind == "system"
            or row.is_self
            or _is_bot(row.member_id, row.member_name, storage)
            or any(marker in text.casefold() for marker in _BOT_TEXT_MARKERS)
            or len(text) > 1_000
        ):
            continue

        context = contexts.setdefault(key, [])
        behavior = classify_behavior(text)
        behavior_counts[behavior] += 1
        style_rows.append(
            {
                "group": group_label,
                "behavior": behavior,
                "text": text,
            }
        )
        pair_rows.append(
            {
                "group": group_label,
                "behavior": behavior,
                "speaker": speaker,
                "context": list(context[-window:]),
                "response": text,
                "observed_at": row.observed_at.isoformat() if row.observed_at else "",
                "source": _safe_hash(f"{row.account_id}:{row.group_id}:{row.id}"),
            }
        )
        context.append(f"{speaker}：{text}")

    manifest = {
        "format_version": 1,
        "generated_at": _utc_now_text(),
        "groups": len(groups),
        "messages": len(style_rows),
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "window_messages": window,
        "anonymized": bool(anonymize),
        "files": ["style_corpus.jsonl", "behavior_pairs.jsonl"],
        "caveat": (
            "behavior_pairs response is a group member's message, not an AI reply; "
            "member/group labels are anonymized, but message text itself is preserved "
            "and may contain names. Review before sharing. Use this export for style/"
            "behavior research, or rewrite targets into the persona before "
            "persona-conditioned SFT."
        ),
    }
    if since_utc is not None:
        manifest["since"] = since_utc.isoformat()
    if until_utc is not None:
        manifest["until"] = until_utc.isoformat()
    return style_rows, pair_rows, manifest


def export_training_data(
    storage: Storage,
    out_dir: str | Path,
    *,
    window_messages: int = 6,
    anonymize: bool = True,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Write style/behavior JSONL files and a manifest under ``out_dir``."""

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    style_rows, pair_rows, manifest = build_training_examples(
        storage,
        window_messages=window_messages,
        anonymize=anonymize,
        since=since,
        until=until,
    )
    files = {
        "style_corpus.jsonl": style_rows,
        "behavior_pairs.jsonl": pair_rows,
    }
    for name, rows in files.items():
        path = target / name
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        **manifest,
        "out_dir": str(target),
        "files": [str(target / name) for name in files],
    }


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()
