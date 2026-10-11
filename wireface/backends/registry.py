"""The agents a face can run behind it, by ``backend.type``. Add another agent here."""
from __future__ import annotations

from .base import Backend


def _claude() -> type[Backend]:
    from .claude import ClaudeBackend            # imports the Claude SDK: only when it is wanted
    return ClaudeBackend


def _opencode() -> type[Backend]:
    from .acp import OpenCodeBackend
    return OpenCodeBackend


def _codex() -> type[Backend]:
    from .codex import CodexBackend
    return CodexBackend


_BACKENDS = {"claude": _claude, "opencode": _opencode, "codex": _codex}


def types() -> tuple[str, ...]:
    return tuple(_BACKENDS)


def get(kind: str) -> type[Backend]:
    try:
        return _BACKENDS[kind]()
    except KeyError:
        raise ValueError(f"unknown backend {kind!r} (available: {', '.join(_BACKENDS)})") from None


def register(kind: str, factory) -> None:
    """For tests and plugins: make ``kind`` available (``factory()`` returns the Backend class)."""
    _BACKENDS[kind] = factory
