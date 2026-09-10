"""Custom exceptions for jwst-redux."""


class JWSTReduxError(RuntimeError):
    """Base package exception."""


class ConfigurationError(JWSTReduxError):
    """Raised when configuration cannot describe a supported operation."""


class ArchiveQueryError(JWSTReduxError):
    """Raised when an archive query returns unusable or inconsistent metadata."""


class PlanningError(JWSTReduxError):
    """Raised when archive metadata cannot produce a safe reduction plan."""
