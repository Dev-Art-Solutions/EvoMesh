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
import re
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
    # Top-level function/class names, formatted for a prompt (``"find_cycles()"``,
    # ``"CycleSummary"``) -- what project_map shows so a plan can quote a real
    # name instead of a plausible-sounding one.
    exports: tuple[str, ...] = ()
    # Every function/class name defined anywhere in the file, nested or not --
    # broader than ``exports`` on purpose. This is what the fabrication check
    # trusts, so a plan mentioning a real nested helper or a class method is
    # never flagged just for not being a top-level name.
    all_names: frozenset[str] = frozenset()

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


def _exported_signatures(tree: ast.Module) -> tuple[str, ...]:
    """Top-level function and class names, for a model to quote instead of guess.

    Bare names, not full signatures: project_map is a character budget for a
    small local model's context, and the mistake seen all night wiring a dead
    module in was never a wrong parameter -- it was a name that was never there
    at all (``scc_find_cycles``, ``CycleCounter``, ``live_cycle_number``, none
    of which exist). A verbatim name list closes off guessing at the source.
    """
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(f"{node.name}()")
        elif isinstance(node, ast.ClassDef):
            names.append(node.name)
    return tuple(names)


def _all_defined_names(tree: ast.Module) -> frozenset[str]:
    """Every function/class name anywhere in the file, nested included.

    Broader than ``_exported_signatures`` on purpose -- it backs the
    fabrication check (see ``fabricated_references``), where flagging a real
    nested helper or class method just for not being top-level would be a
    false positive, not a caught hallucination.
    """
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )


def survey(root: Path) -> list[Module]:
    """Every module in the package, with who imports whom already resolved."""
    directory = package_root(root)
    if not directory.is_dir():
        return []
    raw: dict[str, tuple[Path, str, int, set[str], tuple[str, ...], frozenset[str]]] = {}
    for path in sorted(directory.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A candidate mid-repair can hold a file that does not parse. That is
            # the linter's finding to report, not this module's to crash on.
            raw[path.stem] = (
                path,
                "(does not parse)",
                len(source.splitlines()),
                set(),
                (),
                frozenset(),
            )
            continue
        raw[path.stem] = (
            path,
            _summary(tree),
            len(source.splitlines()),
            _imported_names(tree),
            _exported_signatures(tree),
            _all_defined_names(tree),
        )

    importers: dict[str, set[str]] = {name: set() for name in raw}
    for name, (_, _, _, imports, _, _) in raw.items():
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
            exports=exports,
            all_names=all_names,
        )
        for name, (path, summary, lines, imports, exports, all_names) in raw.items()
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


def stray_root_scripts(root: Path) -> list[str]:
    """Loose ``.py`` files sitting directly in the repository root.

    Every real entry point lives under ``src/``, ``tests/``, ``tools/``, or
    ``scripts/``; ``new_orphans`` only ever surveys ``src/evomesh/*.py``, so a
    generation that cannot invent a dead module there (that check is a ratchet
    now) can still invent a debugging script at the root instead -- ruff sees
    it, but pyright and pytest do not, and a small print-and-exit script reads
    as clean to all three. A dozen of exactly this kind of file accumulated
    here across earlier generations before this existed. Nothing legitimate is
    ever authored at this level, so the rule is absolute rather than a ratchet:
    any match here is new litter, not tolerated legacy.
    """
    return sorted(path.name for path in root.glob("*.py"))


# Matches a whole backtick span shaped exactly like ``module.symbol`` or
# ``module.symbol(...)`` -- a chained third segment (``evomesh.cycles.thing``)
# or anything else inside the backticks is left alone rather than guessed at.
_MODULE_SYMBOL_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)(?:\([^)]*\))?$"
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def fabricated_references(text: str, root: Path) -> list[str]:
    """Backtick-quoted ``module.symbol`` mentions naming a real project module
    but a symbol it never defines, anywhere -- top-level or nested.

    Every plan rejection the evaluator ever wrote this session named exactly
    this: a plausible-sounding function or class the model recalled instead of
    read. Catching it here costs a regex and an AST lookup already computed by
    ``survey``; catching it in ``docs/evolution/plans/plan.eval.md`` costs a
    whole harness job and the model's own step budget to reach the same
    verdict. This is deliberately narrow -- only a module name that actually
    exists in the project is checked, so ``os.path`` or ``self.thing`` never
    match at all -- a false negative here just falls through to the evaluator
    that already catches it; a false positive would reject a real plan outright.
    """
    modules = {module.name: module for module in survey(root)}
    if not modules:
        return []
    found: list[str] = []
    for span in _BACKTICK_RE.findall(text):
        match = _MODULE_SYMBOL_RE.match(span.strip())
        if match is None:
            continue
        module_name, symbol = match.group(1), match.group(2)
        if symbol == "py":
            # `cycles.py` -- naming the file itself, not a symbol in it. This
            # is how a plan almost always refers to a module by name.
            continue
        module = modules.get(module_name)
        if module is None or symbol in module.all_names:
            continue
        label = f"{module_name}.{symbol}"
        if label not in found:
            found.append(label)
    return found


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
    dead = sorted((item for item in modules if item.is_orphan), key=lambda item: item.name)
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
            "another file like them is not. What each one actually exports, "
            "verbatim -- never invent a name that is not listed here:"
        )
        lines += [
            f"- {item.name}.py: {', '.join(item.exports) if item.exports else '(nothing exported)'}"
            for item in dead
        ]
    lines.append(
        "A file placed directly in the repository root (not under src/, tests/, "
        "tools/, scripts/, or docs/) is never a real answer -- it fails validation."
    )
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n..."
