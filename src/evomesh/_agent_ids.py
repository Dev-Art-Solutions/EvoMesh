"""Hierarchical agent-identifier helpers for the EvoMesh.

Agent meshes frequently need identifiers that are both stable and hierarchical,
such as ``root.child.grandchild``.  This module adds a small, dependency-free
toolkit for building and validating such identifiers.
"""

from __future__ import annotations

_ALLOWED: str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
_SEPARATOR: str = "."
_MIN_LEN: int = 1
_MAX_LEN: int = 128


class AgentId(str):
    """A validated, hierarchical agent identifier that behaves like a ``str``.

    Constructing one raises :class:`ValueError` for anything that is not a
    dot-separated sequence of valid segments, so an ``AgentId`` is always safe
    to use as a mesh node name.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> AgentId:
        if not is_valid(value):
            raise ValueError(f"invalid agent identifier: {value!r}")
        return str.__new__(cls, value)

    def parent(self) -> str:
        """Return the parent identifier, or ``""`` for a top-level id."""
        head, _, _ = str(self).rpartition(_SEPARATOR)
        return head

    def child(self, name: str) -> AgentId:
        """Return a validated child identifier formed by appending ``name``."""
        return AgentId(f"{self}{_SEPARATOR}{name}")


def is_valid(value: object) -> bool:
    """Return ``True`` when ``value`` is a syntactically valid agent identifier."""
    if not isinstance(value, str):
        return False
    for segment in value.split(_SEPARATOR):
        if not _MIN_LEN <= len(segment) <= _MAX_LEN:
            return False
        if not all(ch in _ALLOWED for ch in segment):
            return False
    return True


def make_id(*parts: str) -> AgentId:
    """Build a validated hierarchical identifier from the given ``parts``.

    Empty parts are dropped, so ``make_id("root", "", "child")`` yields
    ``AgentId("root.child")``.
    """
    cleaned = [p for p in parts if p]
    if not cleaned:
        raise ValueError("make_id requires at least one non-empty part")
    for segment in cleaned:
        if not _MIN_LEN <= len(segment) <= _MAX_LEN:
            raise ValueError(f"identifier segment out of range: {segment!r}")
        if not all(ch in _ALLOWED for ch in segment):
            raise ValueError(f"identifier segment has illegal characters: {segment!r}")
    return AgentId(_SEPARATOR.join(cleaned))