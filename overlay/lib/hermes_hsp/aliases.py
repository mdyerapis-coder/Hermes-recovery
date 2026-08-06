"""Deterministic Bitwarden item matching and field extraction."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .errors import AmbiguousSecret, SecretNotFound
from .models import Match, SecretSpec


def normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def field_map(item: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in item.get("fields") or []:
        name = normalise(str(field.get("name") or ""))
        value = field.get("value")
        if name and isinstance(value, str):
            result[name] = value
    return result


def score_item(item: dict[str, Any], spec: SecretSpec) -> Match:
    name = normalise(str(item.get("name") or ""))
    aliases = {normalise(spec.name), normalise(spec.env), *(normalise(a) for a in spec.aliases)}
    aliases.discard("")
    score = 0
    reasons: list[str] = []

    fields = field_map(item)
    canonical = normalise(fields.get("hermessecret", ""))
    if canonical and canonical in aliases:
        score += 200
        reasons.append("hermes_secret field")

    if name in aliases:
        score += 120
        reasons.append("exact item name")
    else:
        contained = sorted((a for a in aliases if len(a) >= 4 and a in name), key=len, reverse=True)
        if contained:
            score += 60 + min(len(contained[0]), 30)
            reasons.append("item-name alias")

    raw_name = str(item.get("name") or "").casefold()
    if any(word in raw_name for word in ("active", "current", "production", "prod")):
        score += 12
        reasons.append("active marker")
    if any(word in raw_name for word in ("old", "revoked", "expired", "archive", "disabled")):
        score -= 100
        reasons.append("retired marker")

    return Match(item=item, score=score, reasons=reasons)


def choose_unique(items: Iterable[dict[str, Any]], spec: SecretSpec) -> Match:
    matches = [score_item(item, spec) for item in items]
    matches = [match for match in matches if match.score > 0]
    if not matches:
        raise SecretNotFound(f"no Bitwarden item matched {spec.name}")
    matches.sort(key=lambda m: (-m.score, str(m.item.get("name") or "").casefold()))
    best = matches[0]
    tied = [m for m in matches if m.score == best.score]
    if len(tied) > 1:
        names = ", ".join(sorted(str(m.item.get("name") or "<unnamed>") for m in tied))
        raise AmbiguousSecret(f"multiple Bitwarden items matched {spec.name}: {names}")
    return best


def extract_secret(item: dict[str, Any], spec: SecretSpec) -> tuple[str, str]:
    fields = field_map(item)
    for requested in spec.field_names:
        key = normalise(requested)
        value = fields.get(key)
        if value:
            return value, f"custom:{requested}"

    login = item.get("login") or {}
    password = login.get("password")
    if isinstance(password, str) and password:
        return password, "login.password"

    notes = item.get("notes")
    if isinstance(notes, str) and notes.strip():
        return notes.strip(), "notes"

    raise SecretNotFound(f"Bitwarden item {item.get('name', '<unnamed>')} has no usable secret value")
