"""What the package contains and how its modules depend on each other.

The Evolver used to be asked for a change with no picture of the code it was
changing, so it did the only thing it could: it invented a plausible new module.
Those modules validated -- ruff, pyright, pytest and the smoke check are all
perfectly happy with well-written code nobody calls -- and landed as dead
weight. This module supplies the missing picture, and names the orphans so a
candidate that creates one can be failed rather than shipped.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "evomesh"

# Reachable without an importer. ``__init__`` is the package itself, ``__main__``
# is the console script's entry point, and ``smoke`` is executed as
# ``python -m evomesh.smoke`` by the candidate validator.
ENTRY_POINTS = frozenset({"__init__", "__main__", "smoke"})


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    summary: str
    lines: int
    imports: frozenset[str]
    imported_by: frozenset[str]

    @property
    def is_entry_point(self) -> bool:
        return self.name in ENTRY_POINTS

    @property
    def is_orphan(self) -> bool:
        return not self.is_entry_point and not self.imported_by


def package_root(root: Path) -> Path:
    return root / "src" / PACKAGE


def _summary(tree: ast.Module) -> str:
    """The first line of the module docstring -- what the file is for."""
    doc = ast.get_docstring(tree) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _imported_names(tree: ast.Module) -> set[str]:
    """Sibling modules this file imports, by bare name.

    Both spellings the package actually uses are recognised:
    ``from evomesh.console import ConsoleChannel`` and ``from evomesh import
    console``. Relative imports are resolved to their bare name too, so a future
    ``from .console import ...`` is not silently treated as importing nothing.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{PACKAGE}."):
                    found.add(alias.name.split(".", 1)[1].split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                found.add(node.module.split(".")[0])
            elif node.level:
                found.update(alias.name for alias in node.names)
            elif node.module == PACKAGE:
                found.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith(f"{PACKAGE}."):
                found.add(node.module.split(".", 1)[1].split(".")[0])
    return found


def survey(root: Path) -> list[Module]:
    """Every module in the package, with who imports whom already resolved."""
    directory = package_root(root)
    if not directory.is_dir():
        return []
    raw: dict[str, tuple[Path, str, int, set[str]]] = {}
    for path in sorted(directory.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A candidate mid-repair can hold a file that does not parse. That is
            # the linter's finding to report, not this module's to crash on.
            raw[path.stem] = (path, "(does not parse)", len(source.splitlines()), set())
            continue
        raw[path.stem] = (path, _summary(tree), len(source.splitlines()), _imported_names(tree))

    importers: dict[str, set[str]] = {name: set() for name in raw}
    for name, (_, _, _, imports) in raw.items():
        for target in imports:
            if target in importers and target != name:
                importers[target].add(name)

    return [
        Module(
            name=name,
            path=path,
            summary=summary,
            lines=lines,
            imports=frozenset(imports & raw.keys()) - {name},
            imported_by=frozenset(importers[name]),
        )
        for name, (path, summary, lines, imports) in raw.items()
    ]


def orphans(root: Path) -> list[Module]:
    """Modules nothing imports and nothing runs -- code that cannot execute."""
    return [module for module in survey(root) if module.is_orphan]


BASELINE_PATH = Path("docs") / "evolution" / "known-dead-modules.txt"


def known_dead(root: Path) -> frozenset[str]:
    """Orphans that already existed, recorded so only new ones fail the build.

    Eleven modules were already unreachable when this check was written. Failing
    the suite on all of them would have blocked every candidate until a human
    cleaned up 431 lines, so the check is a ratchet instead: what is here is
    tolerated, anything new is not, and wiring one up simply makes this list
    stale rather than wrong.
    """
    path = root / BASELINE_PATH
    if not path.is_file():
        return frozenset()
    return frozenset(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


def new_orphans(root: Path) -> list[Module]:
    """Modules that became unreachable after the baseline was taken."""
    tolerated = known_dead(root)
    return [module for module in orphans(root) if module.name not in tolerated]


def project_map(root: Path, limit: int = 1800) -> str:
    """The package as a prompt, so a mutation targets code that actually runs.

    Two things earn their space here. The load-bearing modules tell the model
    where a change would matter, and the dead ones are a ready-made backlog:
    wiring one of them into the running mesh is worth more than another file
    nobody calls.
    """
    modules = survey(root)
    if not modules:
        return ""
    live = sorted(
        (item for item in modules if item.imported_by),
        key=lambda item: -len(item.imported_by),
    )[:12]
    dead = sorted(item.name for item in modules if item.is_orphan)
    lines = ["THE PACKAGE AS IT STANDS (src/evomesh/)."]
    if live:
        lines.append("Load-bearing modules -- 'usedN' is how many modules import it:")
        lines += [
            f"- {item.name}.py (used{len(item.imported_by)}, {item.lines}L)"
            f" {item.summary or ''}".rstrip()
            for item in live
        ]
    if dead:
        lines.append(
            "DEAD modules -- nothing imports these, so none of their code ever runs. "
            "Wiring one into a load-bearing module above is real work; adding "
            "another file like them is not:"
        )
        lines.append("- " + ", ".join(f"{name}.py" for name in dead))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n..."
