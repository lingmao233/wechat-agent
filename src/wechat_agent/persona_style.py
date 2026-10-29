"""Small deterministic guard for the whale-girl persona's output.

The persona prompt does most of the work, but a model can still slip into
"作为一个 AI" phrasing, overuse emoji, or escalate a playful tease into a
serious insult.  These checks are intentionally narrow and cheap: they only
touch self-identification, a few bubble/sound gimmicks, severe profanity and
the emoji count.  They do not rewrite ordinary technical language.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_persona_reply", "contains_severe_profanity"]

# Keep the first emoji cluster (including variation selectors/ZWJ sequences)
# and drop the rest.  The persona asks for at most one, not a fixed quota.
_EMOJI_RE = re.compile(
    "(?:"
    "[\U0001F1E0-\U0001F1FF]"
    "|[\U0001F300-\U0001F5FF]"
    "|[\U0001F600-\U0001F64F]"
    "|[\U0001F680-\U0001F6FF]"
    "|[\U0001F700-\U0001F77F]"
    "|[\U0001F780-\U0001F7FF]"
    "|[\U0001F800-\U0001F8FF]"
    "|[\U0001F900-\U0001F9FF]"
    "|[\U0001FA00-\U0001FAFF]"
    "|[\u2600-\u26FF]"
    "|[\u2700-\u27BF]"
    ")"
    "(?:\uFE0F|\u200D(?:[\U0001F300-\U0001FAFF]|[\u2600-\u27BF]))*"
)

_META_DISCLOSURE_REPLACEMENTS = (
    (
        re.compile(r"我(?:被)?禁止(?:了)?[^。！？!?\n]{0,16}(?:过激侮辱|脏话|骂人)"),
        "本鱼不骂人",
    ),
    (
        re.compile(
            r"我(?:不会|不能|不可以|不被允许|被禁止|被限制)"
            r"[^。！？!?\n]{0,18}(?:吐泡泡|冒泡|说脏话|骂人|过激侮辱|侮辱别人)"
        ),
        "本鱼不玩这个",
    ),
    (
        re.compile(r"我(?:被)?(?:系统|人设|规则|审核|过滤)(?:要求|限制|禁止|不允许)"),
        "本鱼自己",
    ),
    (
        re.compile(
            r"我的(?:系统|人设|审核|过滤)?(?:提示词|规则|设定)"
            r"|系统提示|人设文件|审核规则|过滤词表"
        ),
        "本鱼的习惯",
    ),
    (
        re.compile(
            r"(?:我的|本鱼的|系统)提示词|系统指令|人设文件|规则清单|审核机制|过滤词表|"
            r"system\s*prompt|提示词(?:里|中|要求|规定|写着|说了|告诉我)",
            re.IGNORECASE,
        ),
        "规矩",
    ),
    (re.compile(r"开发者(?:告诉|要求|规定)我"), "本鱼自己"),
)

_BUBBLE_PATTERNS = (
    re.compile(r"[（(]\s*吐(?:个)?泡泡\s*[)）]"),
    re.compile(r"吐(?:个)?泡泡"),
    re.compile(r"咕噜(?:咕噜)?"),
    re.compile(r"啵啵"),
    re.compile("\U0001FAE7"),  # 🫧
)

_SEVERE_REPLACEMENTS = (
    (re.compile(r"傻\s*逼|傻\s*B|煞\s*笔|沙\s*比", re.IGNORECASE), "笨蛋"),
    (re.compile(r"(?<!妈)妈的"), "真是的"),
    (re.compile(r"操你|日你|草你"), "揍你"),
    (re.compile(r"去死|死全家"), "别闹了"),
    (re.compile(r"贱人|婊子|狗娘养"), "家伙"),
    (re.compile(r"滚你妈"), "别闹了"),
)

_SELF_AI_PATTERNS = (
    (
        re.compile(
            r"(?:我|本鱼|人家|大肥鱼我)\s*(?:只|就|可)?\s*(?:是|作为)\s*"
            r"(?:一个|一名)?\s*(?:人工智能|语言模型|大模型|机器人|助手|AI|ai)"
            r"(?:助手|机器人|语言模型|大模型)?",
            re.IGNORECASE,
        ),
        "本鱼是鲸鱼娘",
    ),
    (
        re.compile(
            r"(?:作为|身为)\s*(?:一个|一名)?\s*"
            r"(?:人工智能|语言模型|大模型|机器人|助手|AI|ai)"
            r"(?:助手|机器人|语言模型|大模型)?",
            re.IGNORECASE,
        ),
        "作为鲸鱼娘",
    ),
)


def contains_severe_profanity(text: str) -> bool:
    return any(pattern.search(str(text or "")) for pattern, _ in _SEVERE_REPLACEMENTS)


def sanitize_persona_reply(text: str, *, max_emoji: int = 1) -> str:
    """Apply the persona's small, deterministic output constraints."""

    value = str(text or "").strip()
    if not value:
        return value
    for pattern, replacement in _META_DISCLOSURE_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    for pattern in _BUBBLE_PATTERNS:
        value = pattern.sub("", value)
    # "冒个泡" is the same bubble gimmick in a different wording; keep the
    # sentence natural instead of deleting it outright.
    value = re.sub(r"冒个泡|冒泡", "说一声", value)
    for pattern, replacement in _SEVERE_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    for pattern, replacement in _SELF_AI_PATTERNS:
        value = pattern.sub(replacement, value)

    limit = max(0, int(max_emoji))
    seen = 0

    def keep_limited(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= limit else ""

    value = _EMOJI_RE.sub(keep_limited, value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return value or "本鱼在。"
