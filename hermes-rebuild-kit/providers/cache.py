from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any

_MISSING = object()

@dataclass
class _Entry:
    value: Any
    expires_at: datetime | None

class SessionCache:
    def __init__(self) -> None:
        self._lock = RLock()
        self._items: dict[str, _Entry] = {}

    def set(self, alias: str, value: Any, ttl_seconds: float | None = None) -> None:
        expires = None
        if ttl_seconds is not None:
            expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        with self._lock:
            self._items[alias] = _Entry(value, expires)

    def get(self, alias: str, default: Any = _MISSING) -> Any:
        with self._lock:
            entry = self._items.get(alias)
            if entry is not None and entry.expires_at is not None and entry.expires_at <= datetime.now(timezone.utc):
                self._items.pop(alias, None)
                entry = None
            if entry is not None:
                return entry.value
        if default is _MISSING:
            raise KeyError(alias)
        return default

    def contains(self, alias: str) -> bool:
        try:
            self.get(alias)
            return True
        except KeyError:
            return False

    def delete(self, alias: str) -> None:
        with self._lock:
            self._items.pop(alias, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __repr__(self) -> str:
        with self._lock:
            return f"SessionCache(entries={len(self._items)})"
