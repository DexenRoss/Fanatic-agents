"""Domain errors for controlled sandbox execution."""


class SandboxError(RuntimeError):
    """Base class for expected sandbox failures safe to show to users."""


class SandboxPolicyError(SandboxError):
    """Raised when a command or image violates the sandbox policy."""


class SandboxWorkspaceError(SandboxError):
    """Raised when a safe bounded workspace cannot be prepared."""


class DockerUnavailableError(SandboxError):
    """Raised when the Docker CLI is not installed or executable."""


class DockerDaemonUnavailableError(SandboxError):
    """Raised when the Docker daemon does not respond."""


class SandboxImageUnavailableError(SandboxError):
    """Raised when the requested image is not available locally."""


class SandboxExecutionError(SandboxError):
    """Raised when Docker execution cannot be started or observed safely."""
