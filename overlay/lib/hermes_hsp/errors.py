"""Hermes Secret Provider exceptions."""


class HSPError(RuntimeError):
    """Base error for secret-provider failures."""


class BackendUnavailable(HSPError):
    """The configured secret backend is unavailable."""


class AuthenticationRequired(HSPError):
    """The backend requires an interactive login or unlock."""


class SecretNotFound(HSPError):
    """A required secret could not be resolved."""


class AmbiguousSecret(HSPError):
    """More than one vault item is an equally good match."""


class InvalidSecret(HSPError):
    """A resolved secret failed validation."""
