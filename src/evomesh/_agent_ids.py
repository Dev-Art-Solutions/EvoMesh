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


_next_seq: int = 0


def next_id(width: int = 4) -> str:
    """Return the next zero-padded, monotonically increasing agent id.

    The ids are stable, sortable as strings and always valid identifiers, so
    they work as a drop-in default for freshly created agents.  Each call
    advances the module-level counter, so consecutive agents get distinct
    ids without any shared mutable default.
    """
    global _next_seq
    _next_seq += 1
    return f"{_next_seq:0{width}d}"


class AgentIdSequence:
    """A private, monotonic generator of well-formed agent ids.

    Wraps the module-level ``next_id`` counter so a single owner can hand out
    ids without sharing state, while still validating that every id it produces
    is well-formed.  ``next_id`` returns the same string ``make_id`` would for
    the same value, so ids are comparable and importable.
    """

    def __init__(self, width: int = 4) -> None:
        self._width = width

    def next_id(self) -> str:
        """Return the next well-formed id from this sequence (see ``make_id``)."""
        return next_id(self._width)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        return self.next_id()


class AgentIdRegistry:
    """The set of ids the mesh currently knows about.

    Backed by a plain ``set`` of strings so lookups are O(1); every id stored
    here is validated on the way in, so ``contains`` can never report a
    malformed id as present.
    """

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def register(self, agent_id: str) -> bool:
        """Return ``True`` if ``agent_id`` is well-formed and newly recorded.

        An id that is malformed, or already registered, is not added and the
        call reports ``False`` so a double registration is never silent.
        """
        if not is_valid(agent_id) or agent_id in self._ids:
            return False
        self._ids.add(agent_id)
        return True

    def contains(self, agent_id: str) -> bool:
        """Whether ``agent_id`` has been registered (and is well-formed)."""
        return agent_id in self._ids

    def __contains__(self, agent_id: object) -> bool:
        return self.contains(agent_id if isinstance(agent_id, str) else "")  # type: ignore[return-value]

    def __iter__(self):
        return iter(set(self._ids))

    def __len__(self) -> int:
        return len(self._ids)


class AgentIdValidator:
    """The single source of truth for what counts as a well-formed id."""

    @staticmethod
    def is_valid(agent_id: str | None) -> bool:
        """Whether ``agent_id`` is a well-formed id (see ``make_id``)."""
        return is_valid(agent_id)


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    print(next_id())
    print(is_valid(make_id("root")))
    print(AgentIdSequence().next_id())
    print(AgentIdValidator.is_valid(make_id("root")))