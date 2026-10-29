"""Choose which chat images belong in one reply prompt.

WeChat keeps every picture row in ``messages.image_paths`` forever, and the
prompt builder used to forward every image still inside the context window.
Models then re-described the same bottle/cat/screenshot for turn after turn
even when nobody was talking about it.

Rules implemented here:

* the triggering message's own images are always visible;
* an old image stays visible for at most ``turns`` messages after it was sent;
* an image older than that comes back only when someone explicitly talks about
  a picture (``图片``/``照片``/``截图``/``看图``/``上面那张`` ...) or quotes an
  image placeholder, and then stays visible for ``turns`` more messages;
* a hard cap keeps the newest qualifying images.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from .models import ConversationMessage

#: Wording that makes an image part of the current question.  Deliberately
#: specific: a bare "图" appears inside ordinary words such as 图书馆/地图.
_IMAGE_REFERENCE = re.compile(
    r"(?:"
    r"(?:上面|刚才|之前|前面|上次|原来|那张|这张|发的|拍的|截的)"
    r"[^\n]{0,6}?(?:图|照片|截图|表情包)"
    r"|(?:图片|照片|截图|表情包)(?:里|中|上|呢|吗|？|\?|是|有|看着|像)"
    r"|图里|图中|这图|那图|看图|原图|图片|照片|截图|表情包"
    r")"
)

#: Quote placeholders WeChat shows when someone replies to a picture.
_IMAGE_PLACEHOLDERS = ("[图片]", "[照片]", "[微信图片]", "[Image]")


def references_image(text: str) -> bool:
    """True when the message itself asks/comment about a picture."""

    value = str(text or "")
    if any(placeholder in value for placeholder in _IMAGE_PLACEHOLDERS):
        return True
    return bool(_IMAGE_REFERENCE.search(value))


def _ordered(messages: Sequence[ConversationMessage]) -> list[ConversationMessage]:
    ordered = list(messages)
    if ordered and all(message.observed_at is not None for message in ordered):
        ordered.sort(key=lambda message: message.observed_at)
    return ordered


def _age_map(messages: Sequence[ConversationMessage]) -> dict[int, int]:
    """Map the index of each message to how many messages came after it."""

    total = len(messages)
    return {index: total - 1 - index for index in range(total)}


def select_image_paths(
    current_paths: Iterable[str],
    incoming_text: str,
    *,
    quoted_text: str = "",
    recent_messages: Sequence[ConversationMessage] = (),
    turns: int = 4,
    reference_window: int = 30,
    max_count: int = 4,
    refreshed: Mapping[str, int] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Return ``(paths_for_this_prompt, next_refresh_state)``.

    ``refreshed`` carries paths that a previous message refreshed explicitly:
    those stay visible for their remaining turns even though they are older
    than the ordinary window.
    """

    cap = max(0, int(max_count))
    if cap == 0:
        return [], {}

    window = max(0, int(turns))
    reference_limit = max(window, int(reference_window))
    messages = _ordered(recent_messages)
    ages = _age_map(messages)

    state: dict[str, int] = {}
    for path, remaining in (refreshed or {}).items():
        left = int(remaining) - 1
        if path and left > 0:
            state[path] = left

    selected: list[str] = []

    def add(path: str) -> None:
        value = str(path or "").strip()
        if value and value not in selected:
            selected.append(value)

    # The message being answered is always allowed to carry its own picture.
    for path in current_paths or []:
        add(path)

    # A picture someone points at again is promoted back into the window.
    if references_image(incoming_text) or any(
        placeholder in str(quoted_text or "") for placeholder in _IMAGE_PLACEHOLDERS
    ):
        newest: tuple[int, str] | None = None
        for index, message in enumerate(messages):
            if not message.image_paths:
                continue
            age = ages.get(index, 0)
            if age > reference_limit:
                continue
            for path in message.image_paths:
                if newest is None or age < newest[0]:
                    newest = (age, str(path))
        if newest is not None:
            state[newest[1]] = max(1, window)
            add(newest[1])

    for path in state:
        add(path)

    # Ordinary window: newest pictures first so the cap drops the oldest ones.
    for index in range(len(messages) - 1, -1, -1):
        age = ages.get(index, 0)
        if age > window:
            continue
        for path in messages[index].image_paths:
            add(path)

    return selected[:cap], state
