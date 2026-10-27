"""Thread-confined bounded caches; content is compared directly, never digested."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CacheStatistics:
    hits: int
    misses: int
    evictions: int
    entries: int
    estimated_bytes: int


class ContentCache[T]:
    """One entry per object, with an equality-checked content/style description.

    UUIDs index entries. Neither source bytes nor resource descriptions are used
    as dictionary keys. Callers own the cache on the Qt thread creating its values.
    The byte limit is an estimate; the entry limit also bounds opaque Qt allocations.
    Oversized values are returned without retaining them.
    """

    def __init__(self, *, byte_budget: int, entry_budget: int) -> None:
        if byte_budget <= 0 or entry_budget <= 0:
            raise ValueError("cache budgets must be positive")
        self.byte_budget = byte_budget
        self.entry_budget = entry_budget
        self._entries: OrderedDict[UUID, tuple[object, T, int]] = OrderedDict()
        self._bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(
        self,
        object_id: UUID,
        description: object,
        estimated_bytes: int,
        create: Callable[[], T],
    ) -> T:
        previous = self._entries.get(object_id)
        if previous is not None and previous[0] == description:
            self._hits += 1
            self._entries.move_to_end(object_id)
            return previous[1]
        self._misses += 1
        self.discard((object_id,))
        value = create()
        cost = max(1, estimated_bytes)
        if cost > self.byte_budget:
            return value
        while self._entries and (
            self._bytes + cost > self.byte_budget or len(self._entries) >= self.entry_budget
        ):
            _, (_, _, removed_cost) = self._entries.popitem(last=False)
            self._bytes -= removed_cost
            self._evictions += 1
        self._entries[object_id] = description, value, cost
        self._bytes += cost
        return value

    def discard(self, object_ids: Iterable[UUID]) -> None:
        for object_id in object_ids:
            previous = self._entries.pop(object_id, None)
            if previous is not None:
                self._bytes -= previous[2]

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    @property
    def statistics(self) -> CacheStatistics:
        return CacheStatistics(
            self._hits, self._misses, self._evictions, len(self._entries), self._bytes
        )
