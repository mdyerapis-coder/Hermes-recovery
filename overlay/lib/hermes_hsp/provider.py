"""High-level environment resolution for Hermes recovery."""
from __future__ import annotations

from dataclasses import dataclass

from .bitwarden import BitwardenProvider
from .errors import HSPError
from .manifest import ProviderConfig


@dataclass(frozen=True)
class ResolutionEvent:
    name: str
    env: str
    status: str
    detail: str


def resolve_environment(
    config: ProviderConfig,
    base_env: dict[str, str],
    provider: BitwardenProvider,
) -> tuple[dict[str, str], list[ResolutionEvent]]:
    env = dict(base_env)
    events: list[ResolutionEvent] = []
    failures: list[str] = []

    for spec in config.secrets:
        if env.get(spec.env):
            events.append(ResolutionEvent(spec.name, spec.env, "PRESERVE", "already present in environment"))
            continue
        try:
            resolution = provider.resolve(spec)
        except HSPError as exc:
            if spec.required:
                failures.append(f"{spec.name}: {exc}")
                events.append(ResolutionEvent(spec.name, spec.env, "FAIL", str(exc)))
            else:
                events.append(ResolutionEvent(spec.name, spec.env, "SKIP", str(exc)))
            continue
        env[spec.env] = resolution.value
        events.append(
            ResolutionEvent(
                spec.name,
                spec.env,
                "RESOLVE",
                f"{resolution.item_name} ({resolution.source_field})",
            )
        )

    if failures:
        raise HSPError("required secrets unresolved: " + "; ".join(failures))
    return env, events
