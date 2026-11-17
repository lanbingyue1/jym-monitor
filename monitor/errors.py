"""Project-wide exception types."""


class FetchError(Exception):
    """Raised when a page cannot be fetched or the anti-bot gate cannot pass."""


class NotifyError(Exception):
    """Raised when delivering a notification fails."""


class StateError(Exception):
    """Raised when persisted state cannot be read or written."""


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""
