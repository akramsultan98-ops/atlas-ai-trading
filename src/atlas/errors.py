"""Exception hierarchy.

Errors are split by whether they indicate a *safety* condition. Safety errors must never
be caught and suppressed to allow trading to continue.
"""

from __future__ import annotations


class AtlasError(Exception):
    """Base for every ATLAS error."""


class ConfigurationError(AtlasError):
    """Configuration is missing, malformed, or internally contradictory."""


class SafetyError(AtlasError):
    """A safety control was violated or could not be verified.

    Never suppress a SafetyError to keep trading. The correct response is to stop.
    """


class KillSwitchArmedError(SafetyError):
    """An action was attempted while the kill switch is armed."""


class AuditIntegrityError(SafetyError):
    """The audit chain failed verification: history was modified or truncated."""


class ContainmentError(SafetyError):
    """The advisory plane attempted an action reserved for the control plane."""


class PersistenceError(AtlasError):
    """The datastore could not satisfy a request."""


class ValidationError(AtlasError):
    """Input failed validation."""
