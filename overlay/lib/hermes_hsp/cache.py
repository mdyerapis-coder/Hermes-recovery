"""In-memory cache with explicit clearing."""
from __future__ import annotations


class SecretCache:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value

    def clear(self) -> None:
        # Python strings cannot be reliably zeroed, but overwriting references
        # minimises lifetime and avoids persistence to disk.
        for key in list(self._values):
            self._values[key] = ""
        self._values.clear()

    def __len__(self) -> int:
        return len(self._values)
