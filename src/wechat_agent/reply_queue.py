"""Bounded FIFO backlog for inbound chat messages.

A reply can take tens of seconds (memory lookup, web research, a 3k-token
completion, image sending), while members keep talking.  Without a backlog the
listener either answers every arrival at once (bursty, easy to trip the group)
or silently loses the extras.

The queue keeps arrivals in order and answers them one at a time.  It is
drained by the same thread that polls Weixin on purpose: the UI automation
layer (UIA/COM, clipboard, synthetic clicks) is thread-affine, so a background
consumer would be unsafe.  Draining one event per poll cycle gives the visible
"slowly, one by one" behaviour while ingestion keeps running.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(slots=True)
class ReplyQueueStats:
    pushed: int = 0
    dropped: int = 0
    peak_depth: int = 0


class ReplyQueue(Generic[T]):
    """FIFO backlog with a hard cap and observable depth.

    ``maxsize=0`` means unbounded.  When the cap is reached the *oldest*
    pending message is dropped: a stale question is the least useful one to
    answer after a long delay, and the newest arrivals keep the conversation
    coherent.
    """

    def __init__(self, maxsize: int = 50) -> None:
        self._maxsize = max(0, int(maxsize))
        self._items: deque[T] = deque()
        self.stats = ReplyQueueStats()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def push(self, item: T) -> T | None:
        """Append one message, returning the event dropped to make room."""

        dropped: T | None = None
        if self._maxsize and len(self._items) >= self._maxsize:
            dropped = self._items.popleft()
            self.stats.dropped += 1
            log.warning(
                "reply queue overflow max=%s dropped_oldest=%s depth=%s",
                self._maxsize,
                getattr(dropped, "member_name", "?"),
                len(self._items),
            )
        self._items.append(item)
        self.stats.pushed += 1
        self.stats.peak_depth = max(self.stats.peak_depth, len(self._items))
        return dropped

    def pop(self) -> T | None:
        if not self._items:
            return None
        return self._items.popleft()

    def clear(self) -> int:
        pending = len(self._items)
        self._items.clear()
        return pending
