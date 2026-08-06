from __future__ import annotations
import re
from collections import defaultdict
from .exceptions import AliasResolutionError, AmbiguousAliasError

DEFAULT_ALIASES = {
    "openai": ["openai_api_key", "openai-key"],
    "openrouter": ["openrouter_api_key", "openrouter-key"],
    "anthropic": ["anthropic_api_key", "claude_api_key"],
    "google_ai": ["google_ai_api_key", "gemini_api_key", "google-ai"],
    "github": ["github_token", "gh_token"],
    "telegram": ["telegram_bot_token", "telegram-token"],
    "tailscale": ["tailscale_auth_key", "tailscale-key"],
    "vast_ai": ["vast_api_key", "vastai", "vast.ai"],
    "runpod": ["runpod_api_key", "runpod-key"],
    "bitwarden": ["bitwarden_client_secret", "bw_secret"],
    "opencode": ["opencode_api_key", "opencode-key", "opencode_auth"],
    "opencode_zen": ["opencode_zen_api_key", "zen_api_key", "zen-key"],
    "opencode_go": ["opencode_go_api_key", "go_api_key", "go-key"],
}

_AMBIGUOUS_SHORTHANDS = {"go", "zen", "api_key", "key", "token"}

def normalize_alias(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[\s.\-/]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")

class AliasResolver:
    def __init__(self, aliases=None, project_overrides=None):
        aliases = aliases or DEFAULT_ALIASES
        self._canonical: dict[str, str] = {}
        reverse: defaultdict[str, set[str]] = defaultdict(set)
        for canonical, names in aliases.items():
            c = normalize_alias(canonical)
            if c in self._canonical:
                raise AliasResolutionError(f"duplicate canonical alias: {c}")
            self._canonical[c] = c
            reverse[c].add(c)
            for name in names:
                reverse[normalize_alias(name)].add(c)
        self._reverse = dict(reverse)
        self._overrides = {normalize_alias(k): normalize_alias(v) for k, v in (project_overrides or {}).items()}

    def resolve(self, alias: str) -> str:
        n = normalize_alias(alias)
        if n in self._overrides:
            target = self._overrides[n]
            if target not in self._canonical:
                raise AliasResolutionError(f"project alias points to unknown canonical alias: {target}")
            return target
        if n in _AMBIGUOUS_SHORTHANDS:
            raise AmbiguousAliasError(f"ambiguous alias requires project scope: {alias}")
        matches = self._reverse.get(n, set())
        if not matches:
            raise AliasResolutionError(f"unknown secret alias: {alias}")
        if len(matches) > 1:
            raise AmbiguousAliasError(f"ambiguous secret alias: {alias}")
        return next(iter(matches))
