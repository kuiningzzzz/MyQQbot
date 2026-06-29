class FeatureBusyError(RuntimeError):
    """Raised when a maintenance operation conflicts with queued work."""
