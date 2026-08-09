"""Shared deny-by-default rules for repository file paths."""

from __future__ import annotations

from pathlib import Path


EXCLUDED_DIRECTORY_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".next",
    ".dart_tool",
    "target",
    "vendor",
})
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
SECRET_NAME_TOKENS = frozenset({
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
    "secrets",
    "token",
    "tokens",
})


def is_excluded_directory(name: str) -> bool:
    """Return whether a directory is generated, private, or dependency data."""
    return name.lower() in EXCLUDED_DIRECTORY_NAMES


def is_secret_path(path: Path) -> bool:
    """Conservatively identify paths whose names commonly contain secrets."""
    for part in path.parts:
        name = part.lower()
        if name == ".env" or name.startswith(".env."):
            return True
        if Path(name).suffix.lower() in SECRET_SUFFIXES:
            return True
        normalized = name.replace("-", "_").replace(".", "_")
        tokens = {token for token in normalized.split("_") if token}
        if tokens & SECRET_NAME_TOKENS:
            return True
        if {"private", "key"}.issubset(tokens) or {"api", "key"}.issubset(tokens):
            return True
    return False


def is_probably_binary(path: Path) -> bool:
    """Identify binary files using a small, bounded sample."""
    try:
        with path.open("rb") as file:
            sample = file.read(8_192)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
