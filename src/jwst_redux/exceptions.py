"""Custom exceptions for jwst-redux."""


class JWSTReduxError(RuntimeError):
    """Base package exception."""


class ConfigurationError(JWSTReduxError):
    """Raised when configuration cannot describe a supported operation."""


class ArchiveQueryError(JWSTReduxError):
    """Raised when an archive query returns unusable or inconsistent metadata."""


class PlanningError(JWSTReduxError):
    """Raised when archive metadata cannot produce a safe reduction plan."""


class SelectionError(JWSTReduxError):
    """Raised when a configured write selection is absent or ambiguous."""


class DownloadError(JWSTReduxError):
    """Raised when an archive product cannot be downloaded or validated."""


class PipelineExecutionError(JWSTReduxError):
    """Raised when an official pipeline run does not produce valid outputs."""
