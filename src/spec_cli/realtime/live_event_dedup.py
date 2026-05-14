"""Bounded dedupe for Spec Live SSE / REST prompt event ``id`` values.

The hub or a flaky client can occasionally redeliver the same row id.
``spec team watch`` and ``spec watch`` treat duplicate ids as no-ops
for terminal rendering (cursor / buffer state may still advance
elsewhere).
"""
from __future__ import annotations

from collections import deque


class LivePromptEventDeduper:
    """Ring-buffer of recent integer event ids — :meth:`is_redelivery`
    returns ``True`` when ``event_id`` was already seen in this process.
    """

    __slots__ = ("_maxlen", "_order", "_seen")

    def __init__(self, maxlen: int = 8000) -> None:
        self._maxlen = max(256, min(int(maxlen), 500_000))
        self._order: deque[int] = deque()
        self._seen: set[int] = set()

    def is_redelivery(self, event_id: int) -> bool:
        if event_id in self._seen:
            return True
        self._seen.add(event_id)
        self._order.append(event_id)
        while len(self._order) > self._maxlen:
            self._seen.discard(self._order.popleft())
        return False


__all__ = ["LivePromptEventDeduper"]
