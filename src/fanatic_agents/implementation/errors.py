"""Safe domain errors for controlled implementation."""


class ImplementationError(RuntimeError):
    """Base error whose message is safe to expose to users."""


class ChangeApplicationError(ImplementationError):
    """A validated ChangeSet could not be applied safely."""
