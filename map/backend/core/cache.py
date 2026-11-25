from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class TTLCache:
    """Small in-process cache for one worker. shortcut: not shared between workers, upgrade to Redis if we scale out."""

    def __init__(self, max_entries: int, clock=time.monotonic) -> None:
        self._max, self._clock, self._lock = max_entries, clock, threading.Lock()
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            if item[0] <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return item[1]

    def set(self, key: Hashable, value: Any, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock() + ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
