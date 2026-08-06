"""Local, non-disclosing validation for resolved secrets."""
from __future__ import annotations

import re

from .errors import InvalidSecret


def _basic(value: str) -> None:
    if not value or not value.strip():
        raise InvalidSecret("secret is empty")
    if "\n" in value or "\r" in value:
        raise InvalidSecret("secret contains a newline")


def validate_value(name: str, value: str, validator: str) -> None:
    _basic(value)
    if validator in ("", "nonempty"):
        return
    if validator == "token":
        if len(value) < 8 or any(ch.isspace() for ch in value):
            raise InvalidSecret(f"{name} does not look like a token")
        return
    if validator == "telegram_bot_token":
        if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", value):
            raise InvalidSecret("Telegram bot token format is invalid")
        return
    if validator == "tailscale_auth_key":
        if not value.startswith("tskey-"):
            raise InvalidSecret("Tailscale auth key format is invalid")
        return
    if validator == "csv_ids":
        if not re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", value):
            raise InvalidSecret("Telegram allowed users must be comma-separated numeric IDs")
        return
    raise InvalidSecret(f"unknown validator {validator!r} for {name}")
