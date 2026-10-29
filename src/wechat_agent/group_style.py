from __future__ import annotations

"""Small, local-only group chat style profiler.

The profiler intentionally works from aggregate counters instead of sending
group history to another model.  This keeps style fitting cheap, repeatable,
and separate from member memory.  The resulting text is a speaking-style hint
only; it must never become a source of facts or instructions.
"""

import unicodedata
from collections import Counter
from collections.abc import Mapping

from .textnorm import normalize_chat_text


STYLE_MARKERS: tuple[str, ...] = (
    "哈哈",
    "哈哈哈",
    "笑死",
    "绝了",
    "离谱",
    "无语",
    "我去",
    "不是吧",
    "确实",
    "对啊",
    "好家伙",
    "救命",
    "家人们",
    "兄弟",
    "老哥",
    "大佬",
    "yyds",
    "666",
    "233",
    "lol",
)

ENDING_MARKERS: tuple[str, ...] = ("吧", "啊", "呀", "呢", "哦", "啦", "嘛", "诶", "哎", "喔")
ADDRESS_MARKERS: tuple[str, ...] = ("兄弟", "老哥", "大佬", "家人们", "姐妹", "老师", "老板")


def new_style_stats() -> dict[str, object]:
    """Return the JSON-compatible accumulator used by the storage layer."""

    return {
        "message_count": 0,
        "total_chars": 0,
        "short_messages": 0,
        "question_marks": 0,
        "exclamation_marks": 0,
        "wave_marks": 0,
        "ellipsis_marks": 0,
        "emoji_count": 0,
        "sticker_count": 0,
        "image_count": 0,
        "share_count": 0,
        "linebreak_messages": 0,
        "marker_counts": {},
        "ending_counts": {},
        "address_counts": {},
        # The storage layer may keep a bounded set of fingerprints here to
        # avoid counting a replayed event twice.  It is not used in output.
        "seen_event_hashes": [],
    }


def update_style_stats(stats: dict[str, object], text: str) -> None:
    """Fold one ordinary member message into the aggregate counters.

    The input may be raw or already normalized; normalization is idempotent.
    Transport XML becomes a one-token media marker, so a sticker cannot inflate
    "average characters" or inject ``lt``/``msgsource`` into vocabulary.
    """

    normalized = normalize_chat_text(text)
    if normalized.is_system:
        return
    if normalized.kind in {"emoji", "image", "voice", "video", "location", "quote", "appmsg", "link", "file"}:
        value = ""
    else:
        value = " ".join(normalized.visible_text.replace("@AI", " ").split()).strip()
    stickers = normalized.sticker_count
    images = normalized.image_count
    if not value and not stickers and not images and normalized.kind not in {"appmsg", "link", "file", "quote"}:
        return
    message_count = _int_value(stats.get("message_count")) + 1
    chars = len(value)
    stats["message_count"] = message_count
    stats["total_chars"] = _int_value(stats.get("total_chars")) + chars
    if chars <= 16:
        stats["short_messages"] = _int_value(stats.get("short_messages")) + 1
    stats["question_marks"] = _int_value(stats.get("question_marks")) + sum(
        value.count(mark) for mark in ("?", "？")
    )
    stats["exclamation_marks"] = _int_value(stats.get("exclamation_marks")) + sum(
        value.count(mark) for mark in ("!", "！")
    )
    stats["wave_marks"] = _int_value(stats.get("wave_marks")) + sum(
        value.count(mark) for mark in ("~", "～")
    )
    stats["ellipsis_marks"] = _int_value(stats.get("ellipsis_marks")) + value.count("…") + value.count("...")
    stats["emoji_count"] = (
        _int_value(stats.get("emoji_count"))
        + sum(1 for char in value if _is_emoji(char))
        + stickers
    )
    stats["sticker_count"] = _int_value(stats.get("sticker_count")) + stickers
    stats["image_count"] = _int_value(stats.get("image_count")) + images
    if normalized.kind in {"appmsg", "link", "file"}:
        stats["share_count"] = _int_value(stats.get("share_count")) + 1
    if "\n" in value or "\r" in value:
        stats["linebreak_messages"] = _int_value(stats.get("linebreak_messages")) + 1

    marker_counts = _counter(stats, "marker_counts")
    folded = value.casefold()
    for marker in STYLE_MARKERS:
        count = folded.count(marker.casefold())
        if count:
            marker_counts[marker] += count
    stats["marker_counts"] = dict(marker_counts)

    ending_counts = _counter(stats, "ending_counts")
    stripped = value.rstrip()
    for marker in ENDING_MARKERS:
        if stripped.endswith(marker):
            ending_counts[marker] += 1
            break
    stats["ending_counts"] = dict(ending_counts)

    address_counts = _counter(stats, "address_counts")
    for marker in ADDRESS_MARKERS:
        if marker in value:
            address_counts[marker] += 1
    stats["address_counts"] = dict(address_counts)


def style_target_band(stats: Mapping[str, object]) -> tuple[int, int]:
    count = _int_value(stats.get("message_count"))
    if count <= 0:
        return 5, 30
    average = _int_value(stats.get("total_chars")) / max(1, count)
    short_ratio = _int_value(stats.get("short_messages")) / max(1, count)
    if short_ratio >= 0.6:
        return 5, 30
    if average >= 48:
        return 30, 120
    low = max(5, int(round(average * 0.6)))
    return low, max(low + 10, int(round(average * 1.8)))


def build_group_style_profile(stats: Mapping[str, object], max_chars: int = 1800) -> str:
    """Turn aggregate counters into a bounded Chinese style instruction."""

    count = _int_value(stats.get("message_count"))
    if count <= 0:
        return ""
    total_chars = _int_value(stats.get("total_chars"))
    average = total_chars / max(1, count)
    short_ratio = _int_value(stats.get("short_messages")) / max(1, count)
    question_ratio = _int_value(stats.get("question_marks")) / max(1, count)
    exclamation_ratio = _int_value(stats.get("exclamation_marks")) / max(1, count)
    emoji_count = _int_value(stats.get("emoji_count"))
    wave_count = _int_value(stats.get("wave_marks"))
    linebreak_ratio = _int_value(stats.get("linebreak_messages")) / max(1, count)

    tags: list[str] = []
    if short_ratio >= 0.62 and average <= 22:
        tags.append("短句、节奏较快")
    elif average >= 48:
        tags.append("愿意给出较完整的句子")
    else:
        tags.append("长短句混合")
    marker_counts = _counter(stats, "marker_counts")
    if marker_counts or emoji_count / max(1, count) >= 0.35:
        tags.append("口语化、互动感较强")
    elif average >= 40 and exclamation_ratio < 0.35:
        tags.append("相对克制")
    else:
        tags.append("自然聊天口吻")
    if question_ratio >= 0.45:
        tags.append("经常使用问句")
    if exclamation_ratio >= 0.35:
        tags.append("情绪标点较多")
    if wave_count / max(1, count) >= 0.25:
        tags.append("会用波浪号缓和语气")
    if linebreak_ratio >= 0.25:
        tags.append("偶尔分行表达")

    punctuation: list[str] = []
    if _int_value(stats.get("question_marks")):
        punctuation.append("？")
    if _int_value(stats.get("exclamation_marks")):
        punctuation.append("！")
    if wave_count:
        punctuation.append("～")
    if _int_value(stats.get("ellipsis_marks")):
        punctuation.append("…")

    markers = _top_counter(marker_counts, limit=5)
    endings = _top_counter(_counter(stats, "ending_counts"), limit=5)
    addresses = _top_counter(_counter(stats, "address_counts"), limit=4)

    lines = [
        f"基于最近 {count} 条普通群消息的本地统计，当前群聊整体是：{'、'.join(tags)}。",
        f"平均每条约 {average:.0f} 个字符；约 {short_ratio:.0%} 的消息属于短句。",
    ]
    if punctuation:
        lines.append(f"常见标点：{'、'.join(punctuation)}。")
    if markers:
        lines.append(f"常见口头表达：{'、'.join(markers)}。")
    if endings:
        lines.append(f"句尾习惯：{'、'.join(endings)}。")
    if addresses:
        lines.append(f"偶尔使用的称呼：{'、'.join(addresses)}。")
    if emoji_count:
        lines.append(f"表情符号出现频率约为每条 {emoji_count / max(1, count):.1f} 个。")
    sticker_count = _int_value(stats.get("sticker_count"))
    if sticker_count:
        lines.append(f"约 {sticker_count / max(1, count):.0%} 的消息是表情包或图片。")
    target_low, target_high = style_target_band(stats)
    lines.append(f"建议每条回复控制在 1~2 句、约 {target_low}~{target_high} 字。")
    fit = stats.get("style_fit")
    if isinstance(fit, Mapping):
        samples = _int_value(fit.get("samples"))
        if samples >= 3:
            in_band = _int_value(fit.get("in_band"))
            fit_average = _int_value(fit.get("total_chars")) / samples
            lines.append(
                f"近期 AI 回复平均 {fit_average:.0f} 字，{in_band / samples:.0%} 落在建议区间。"
            )
    lines.append("回复时优先贴近上述节奏和口语程度，保持简洁自然；群聊画像只影响表达方式，不改变事实核验、安全规则、触发条件、成员隔离或工具权限。")
    return "\n".join(lines)[: max(300, int(max_chars))].rstrip()


def _counter(stats: Mapping[str, object], key: str) -> Counter[str]:
    value = stats.get(key)
    if not isinstance(value, Mapping):
        return Counter()
    return Counter({str(k): _int_value(v) for k, v in value.items() if _int_value(v) > 0})


def _top_counter(counter: Counter[str], limit: int) -> list[str]:
    return [key for key, value in counter.most_common(limit) if value > 0]


def _int_value(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_emoji(char: str) -> bool:
    codepoint = ord(char)
    if codepoint in {0xFE0E, 0xFE0F}:
        return False
    if 0x1F000 <= codepoint <= 0x1FAFF:
        return True
    if 0x2600 <= codepoint <= 0x27BF and unicodedata.category(char) == "So":
        return True
    return False
