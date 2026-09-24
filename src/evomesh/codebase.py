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
from datetime import datetime
from pathlib import Path

PACKAGE = "evomesh"

# Reachable without an importer. ``__init__`` is the package itself, ``__main__``
# is the console script's entry point, ``smoke`` is executed as
# ``python -m evomesh.smoke`` by the candidate validator, and
# ``browser_bridge`` is executed as ``python -m evomesh.browser_bridge`` --
# not by anything in this package, but by Chrome itself, spawning
# scripts/chrome-native-host.bat the moment browser-extension/background.js
# calls connectNative. A module nothing in this repository imports is
# usually genuinely dead; one whose only caller is an external process this
# repository does not control is not the same thing.
ENTRY_POINTS = frozenset({"__init__", "__main__", "smoke", "browser_bridge"})


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
    # Top-level classes whose bases name them as an interface (Protocol, ABC)
    # rather than a concrete, instantiable thing. Bare names, matching
    # ``exports`` -- used to keep an interface out of the untested-export
    # backlog (see ``untested_target``): a Protocol has no behavior of its
    # own to unit-test, only implementations of it do.
    protocols: frozenset[str] = frozenset()

    @property
    def is_entry_point(self) -> bool:
        return self.name in ENTRY_POINTS

    @property
    def is_orphan(self) -> bool:
        return not self.is_entry_point and not self.imported_by


def package_root(root: Path) -> Path:
    return root / "src" / PACKAGE


def project_root() -> Path:
    """The repository root, resolved from this package's location.

    The package lives at ``<root>/src/evomesh``, so the root is two levels up
    from here. A caller can pass this to :func:`package_root` to locate the
    package without hard-coding the layout.
    """
    return Path(__file__).resolve().parent.parent.parent


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

    A name starting with ``_`` is skipped -- Python's own convention for
    "internal, not part of what this module exports" is exactly the line
    ``untested_target``'s docstring already draws ("an export is a name whose
    test coverage another module could plausibly depend on"). Before this, a
    private helper like ``_post_with_retry`` or ``_with_recent_failure`` was
    handed to a harness job with the objective claiming "It is exported and
    load-bearing" -- true of neither: nothing outside the module is meant to
    depend on it, and the leading underscore says so.
    """
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_"):
            continue
        if isinstance(node, ast.ClassDef):
            names.append(node.name)
        else:
            names.append(f"{node.name}()")
    return tuple(names)


_INTERFACE_BASE_NAMES = frozenset({"Protocol", "ABC"})


def _protocol_class_names(tree: ast.Module) -> frozenset[str]:
    """Top-level classes declared as an interface: ``class X(Protocol):`` or
    ``class X(ABC):``, however ``Protocol``/``ABC`` was imported (bare name
    or ``typing.Protocol`` / ``abc.ABC`` attribute access both count).

    A generic base check, not a semantic one -- a class that merely mixes in
    an unrelated base also named ``Protocol`` would false-positive here, but
    that is a name collision worth being suspicious of on its own, and the
    cost of missing a real interface (handing the model an untestable
    Protocol as if it were a concrete target) is worse than the cost of
    treating a false one as untestable.
    """
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else None
            )
            if base_name in _INTERFACE_BASE_NAMES:
                names.add(node.name)
                break
    return frozenset(names)


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
    raw: dict[
        str, tuple[Path, str, int, set[str], tuple[str, ...], frozenset[str], frozenset[str]]
    ] = {}
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
            _protocol_class_names(tree),
        )

    importers: dict[str, set[str]] = {name: set() for name in raw}
    for name, (_, _, _, imports, _, _, _) in raw.items():
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
            protocols=protocols,
        )
        for name, (path, summary, lines, imports, exports, all_names, protocols) in raw.items()
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


# The literal set of files this project actually keeps at its root. Anything
# else sitting there is scratch work that escaped a subdirectory -- checked in
# once this project started existing, not something a generation ever adds to.
KNOWN_ROOT_FILES = frozenset(
    (
        ".editorconfig",
        ".gitignore",
        ".python-version",
        "AGENTS.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "LICENSE",
        "NuGet.config",
        "README.md",
        "evomesh.yaml",
        "evomesh.yaml.example",
        "evomesh.secrets.yaml",
        "evomesh.secrets.yaml.example",
        "pyproject.toml",
        "start-evomesh-console.bat",
        "start-evomesh.bat",
        "uv.lock",
        # This check also runs against a candidate generation's root, not
        # just the project's -- and a candidate is never quite the same tree.
        # `git worktree add` leaves `.git` as a plain *file* there (a pointer
        # to the real gitdir, not a directory, so `path.is_file()` catches
        # it), `CandidateWorkspace.create` writes `MUTATION_OBJECTIVE.md`, and
        # `CandidateValidator` writes `validation-result.json` after every
        # run. None of the three is model output; flagging them turned every
        # single validation from 74ea2be until this fix into an unwinnable
        # hygiene failure regardless of what the model actually did.
        ".git",
        "MUTATION_OBJECTIVE.md",
        "validation-result.json",
    )
)


def stray_root_files(root: Path) -> list[str]:
    """Any file sitting directly in the repository root that isn't supposed to.

    Every real entry point lives under ``src/``, ``tests/``, ``tools/``, or
    ``scripts/``; ``new_orphans`` only ever surveys ``src/evomesh/*.py``, so a
    generation that cannot invent a dead module there (that check is a ratchet
    now) can still invent a debugging script at the root instead -- ruff sees
    it, but pyright and pytest do not, and a small print-and-exit script reads
    as clean to all three. Restricting this to ``*.py`` closed that gap but
    opened the next one: a single evaluate job was found live writing eleven
    scratch files at once, most of them ``.txt``, none of them caught. Checking
    every file rather than one extension is what actually matches the rule
    already printed in ``project_map`` -- nothing legitimate is ever authored at
    this level -- so this is absolute rather than a ratchet: any match here is
    new litter, not tolerated legacy.
    """
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_file() and path.name not in KNOWN_ROOT_FILES
    )


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


def backlog_target(root: Path, seed: int) -> Module | None:
    """The dead module a backlog objective would target, or ``None`` if empty.

    Exposed separately from :func:`backlog_objective` so a caller can check,
    before committing to it, whether this exact module is one it has already
    thrown several generations at -- see that function's ``nudge_delete``.
    """
    dead = sorted((item for item in survey(root) if item.is_orphan), key=lambda item: item.name)
    if not dead:
        return None
    return dead[seed % len(dead)]


def backlog_objective(root: Path, seed: int, *, nudge_delete: bool = False) -> str | None:
    """A concrete objective from the dead-module backlog, or ``None`` if empty.

    The Evolver's standing goal ("improve EvoMesh by one validated candidate
    generation at a time") names no file and no change -- on a 35b model with
    a 40-step budget, most of that budget was going to figuring out what to
    even attempt, not to attempting it (see generation history: 969-980, ten
    of twelve capped with nothing written). The dead-module list below this
    docstring's own module comment already calls itself "the Evolver's
    backlog" -- this is what makes that literal: one concrete file, its real
    exported names, and where it would plug in, so the model's first step can
    be reading that file instead of guessing what to look for.

    ``seed`` rotates the pick deterministically (mod the backlog length) so a
    module a model cannot manage does not get handed to it again next
    generation -- pass something that increases on every open, such as the
    total count of generations ever opened (``GenerationSupervisor.
    total_created()``, not the open-candidate count: that resets on every
    discard, which is exactly what let a length-1 backlog hand the same
    module to five generations running).

    ``nudge_delete``: set when the caller has seen this same module fail
    several generations straight -- wiring in a real dependency (as opposed
    to a leaf utility) can be a genuinely harder edit than a 40-step budget
    allows, and "wire it in, or delete it" buried as an aside inside a longer
    objective was apparently easy to read past. When true, deletion is put
    first and made the recommended answer instead of an aside.
    """
    target = backlog_target(root, seed)
    if target is None:
        return None
    modules = survey(root)
    live = sorted(
        (item for item in modules if item.imported_by),
        key=lambda item: -len(item.imported_by),
    )
    exports = ", ".join(target.exports) if target.exports else "(nothing exported)"
    if nudge_delete:
        lines = [
            f"Delete src/evomesh/{target.name}.py. Earlier generations tried to wire "
            "it into the running mesh and none of them managed it in the steps they "
            "had -- deleting an orphan module nothing imports is a complete, valid "
            "answer on its own, and the safer one at this point.",
            "If you are confident you can wire it in properly in the steps you have "
            f"left, that is still a fine answer too. What it exports, verbatim: {exports}.",
        ]
    else:
        lines = [
            f"Wire src/evomesh/{target.name}.py into the running mesh, or delete it "
            "if it is not worth keeping -- both are a complete answer. Nothing "
            "imports this module right now, so none of its code ever executes.",
            "What it exports, verbatim -- call one of these, never a name that "
            f"sounds plausible but is not here: {exports}.",
        ]
    if target.summary:
        lines.append(f"Its own docstring says what it is for: {target.summary}")
    if live and not nudge_delete:
        lines.append(
            f"A natural place to call it from is {live[0].name}.py, the most-used "
            f"module in the package (used by {len(live[0].imported_by)} others)."
        )
    return "\n".join(lines)


def _test_source_text(root: Path) -> str:
    """Everything under tests/, concatenated, for a cheap "is this name
    mentioned anywhere in the suite" check.

    Not a coverage tool -- no test runs, nothing is measured. A name that
    appears in a test file might still not be exercised (a stale import, a
    comment), and this can never prove a name IS covered, only flag the
    stronger signal that it is not even *mentioned*. That is enough to be a
    concrete lead, the same way the dead-module backlog was never proof a
    wiring was correct, just a real place to look.
    """
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return ""
    chunks: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def untested_target(root: Path, seed: int) -> tuple[Module, str] | None:
    """A (module, exported name) pair never mentioned under tests/, or
    ``None`` if every eligible export of every live module is.

    The second-tier objective source, used once the dead-module backlog
    (:func:`backlog_target`) is empty -- found live 2026-09-23: after ~1200
    generations, ``survey()`` returned zero orphans, and every generation
    since fell through to the standing goal's bare "improve EvoMesh" text,
    with nothing concrete to anchor a small model's step budget on. This
    picks the same way: deterministic across a module's *exported* names
    only (not every function -- an export is a name whose test coverage
    another module could plausibly depend on), sorted for a stable order,
    then rotated by ``seed`` so a module the model cannot manage does not
    get handed back next generation.

    Plain functions are tried before classes, and a ``Protocol``/``ABC`` is
    never a candidate at all -- found live: the first two real attempts both
    picked ``AgentBehavior``, a bare ``Protocol`` with no behavior of its own
    to test, and burned their whole budget on it (one fabricated a mock
    class wholesale; the other spiralled into believing its own tools were
    broken). Testing a function means "call it, check the result"; testing a
    class means constructing one correctly first, and testing an interface
    means testing nothing at all. Ordering by difficulty, not just by name,
    is the whole fix.
    """
    modules = survey(root)
    live = [item for item in modules if item.imported_by and item.exports]
    if not live:
        return None
    test_text = _test_source_text(root)

    def mentioned(bare: str) -> bool:
        # The bare name as a whole word, never the prompt-formatted
        # ``"parse()"`` -- found live 2026-09-24: a test calls ``parse("* *")``,
        # so the literal ``parse()`` appeared nowhere under tests/ and every
        # exported function stayed "untested" forever. The backlog never
        # drained, and ~30 generations straight landed yet another small test
        # for a function that already had several.
        return re.search(rf"\b{re.escape(bare)}\b", test_text) is not None

    def eligible(module: Module) -> list[str]:
        return [
            name
            for name in module.exports
            if not mentioned(name.rstrip("()")) and name.rstrip("()") not in module.protocols
        ]

    functions = [
        (module, name)
        for module in sorted(live, key=lambda item: item.name)
        for name in eligible(module)
        if name.endswith("()")
    ]
    candidates = functions or [
        (module, name)
        for module in sorted(live, key=lambda item: item.name)
        for name in eligible(module)
        if not name.endswith("()")
    ]
    if not candidates:
        return None
    return candidates[seed % len(candidates)]


def untested_objective(root: Path, seed: int) -> str | None:
    """A concrete objective from the untested-export backlog, or ``None``.

    Deliberately modest about what "untested" means here: a name absent
    from every file under tests/ is a real, checkable lead, not a proof of
    a coverage gap (see :func:`_test_source_text`). The objective says so,
    so the model spends its first step reading the real definition instead
    of assuming a bug is waiting to be found.

    Deliberately modest about what the test itself has to be, too -- found
    live 2026-09-23: asking for "a focused test... including at least one
    edge case" against a 40-60 step budget produced fabricated mock classes
    and a job that talked itself into believing its tools were broken. One
    small, real, passing check beats an ambitious one that never lands.
    """
    target = untested_target(root, seed)
    if target is None:
        return None
    module, name = target
    importers = len(module.imported_by)
    is_function = name.endswith("()")
    bare_name = name[:-2] if is_function else name
    how = (
        f"Call `{bare_name}` with the simplest realistic arguments and assert "
        "the one obvious thing about its result. Do not try to cover every "
        "branch or every edge case -- one real, passing check that exercises "
        "actual behavior is a complete answer."
        if is_function
        else (
            f"Construct one `{bare_name}` the way an existing test already "
            "constructs something similar (grep tests/ for how other objects "
            "of a comparable shape are built there), call one real method on "
            "it, and assert one obvious thing about the result."
        )
    )
    lines = [
        f"Write ONE small, mechanical test for `{name}` in "
        f"`src/evomesh/{module.name}.py`. It is exported and load-bearing "
        f"(used by {importers} other module{'s' if importers != 1 else ''}), "
        "but its name does not appear anywhere under tests/, so it has no "
        "direct test coverage right now.",
        f"Read the real definition first -- do not guess its signature or "
        f"behavior from the name alone. {how}",
        "Do not invent a mock or stub class from scratch. If this needs a "
        "stand-in for a dependency, search tests/ first for one that already "
        "exists and reuse it -- a test that references something imagined is "
        "worse than no test at all.",
        "Add it to the existing test file for this module if one exists "
        f"(tests/test_{module.name}.py), or create one if it does not.",
    ]
    if module.summary:
        lines.append(f"The module's own docstring says what it is for: {module.summary}")
    return "\n".join(lines)


# The improvement backlog: real changes to how EvoMesh behaves, written as a
# plain Markdown checklist a human (or the mesh itself) can append to. Found
# 2026-09-24: every objective source before this one was maintenance -- wire a
# dead module, then write one small test for an untested export -- so the best
# a generation could ever do was add a test. ~30 generations straight landed
# exactly that and nothing else: the pipeline worked and the system never got
# any better.
IMPROVEMENTS_FILE = Path("docs") / "evolution" / "improvements.md"
_OPEN_ITEM = re.compile(r"^- \[ \] (?P<title>\S.*?)\s*$")


@dataclass(frozen=True)
class Improvement:
    title: str
    detail: str = ""


def open_improvements(root: Path) -> list[Improvement]:
    """Every unticked ``- [ ]`` item in the improvement backlog, in file order.

    An item's detail is whatever indented lines follow it, dedented -- the
    concrete where/why that turns a wish into something a small model can act
    on in one harness job.
    """
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return []
    items: list[Improvement] = []
    title: str | None = None
    detail: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _OPEN_ITEM.match(line)
        if match is not None:
            if title is not None:
                items.append(Improvement(title, "\n".join(detail).strip()))
            title, detail = match.group("title"), []
        elif title is not None and (line.startswith((" ", "\t")) or not line.strip()):
            detail.append(line.strip())
        elif title is not None:
            items.append(Improvement(title, "\n".join(detail).strip()))
            title, detail = None, []
    if title is not None:
        items.append(Improvement(title, "\n".join(detail).strip()))
    return items


def improvement_needle(item: Improvement) -> str:
    """The prefix every objective built from ``item`` starts with."""
    return f"Implement this improvement to EvoMesh: {item.title}"


def improvement_objective(item: Improvement) -> str:
    lines = [
        improvement_needle(item),
        *([item.detail] if item.detail else []),
        "It comes from the project's own improvement backlog "
        f"({IMPROVEMENTS_FILE.as_posix()}). This is a change to how EvoMesh "
        "behaves, so it must change at least one file under src/evomesh/ -- "
        "a test alone is not an answer, and a candidate that only touches "
        "tests or docs is discarded unvalidated. A test covering the new "
        "behavior is welcome alongside the source change.",
        f"Do not edit {IMPROVEMENTS_FILE.as_posix()} yourself: the pipeline "
        "ticks this item off once your change is in.",
    ]
    return "\n".join(lines)


def tick_improvement(root: Path, title: str) -> bool:
    """Mark ``title`` done in ``root``'s backlog; ``False`` if it is not open there."""
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _OPEN_ITEM.match(line.rstrip("\r\n"))
        if match is not None and match.group("title") == title:
            lines[index] = line.replace("- [ ] ", "- [x] ", 1)
            path.write_text("".join(lines), encoding="utf-8")
            return True
    return False


# Where the running mesh logs (``--log-file``, as every launcher this project
# ships sets it). A traceback in here is a failure the system actually
# suffered while running -- the most concrete objective there is, and one no
# amount of test-writing against exports would ever find.
RUNTIME_LOG = Path(".runtime") / "logs" / "mesh.log"
RUNTIME_LOG_TAIL_BYTES = 4 * 1024 * 1024
# A one-off traceback is as often the host (a dropped socket, a sleeping
# machine) as the code; twice is a pattern worth a generation.
MIN_FAULT_OCCURRENCES = 2
_LOG_ENTRY = re.compile(r'^\{"time":"(?P<time>[^"]+)","level":"(?P<level>\w+)"')
_FRAME = re.compile(
    r'File "[^"]*[\\/]src[\\/]evomesh[\\/](?P<module>\w+)\.py", '
    r"line (?P<line>\d+), in (?P<function>[\w<>]+)"
)
_EXCEPTION_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


@dataclass(frozen=True)
class RuntimeFault:
    module: str
    function: str
    line: int
    exception: str
    count: int
    last_seen: str
    traceback: str


def _log_time(text: str) -> float:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except ValueError:
        return 0.0


def runtime_faults(root: Path, log: Path = RUNTIME_LOG) -> list[RuntimeFault]:
    """Tracebacks from the mesh's own log whose innermost EvoMesh frame's
    file has not changed since, most frequent first.

    "Not changed since" is the whole notion of fixed here: a promoted
    generation rewrites the file, so its mtime moves past every earlier
    occurrence and the fault drops out until it happens again. Deliberately
    generous -- an unrelated edit to the same file retires a fault too --
    because a fault that is really still there comes straight back the next
    time the mesh hits it.
    """
    path = root / log
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - RUNTIME_LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    grouped: dict[tuple[str, str, str], list[tuple[str, int, str]]] = {}

    def close(time: str, block: list[str]) -> None:
        frames = [match for line in block if (match := _FRAME.search(line))]
        exceptions = [
            match.group("type")
            for line in block
            if (match := _EXCEPTION_LINE.match(line.strip()))
        ]
        if not frames or not exceptions:
            return
        frame = frames[-1]
        key = (frame.group("module"), frame.group("function"), exceptions[-1])
        grouped.setdefault(key, []).append(
            (time, int(frame.group("line")), "\n".join(block[-24:]))
        )

    time: str | None = None
    block: list[str] = []
    for line in text.splitlines():
        entry = _LOG_ENTRY.match(line)
        if entry is not None:
            if time is not None:
                close(time, block)
            if entry.group("level") in ("ERROR", "CRITICAL"):
                time, block = entry.group("time"), [line]
            else:
                time, block = None, []
        elif time is not None:
            block.append(line)
    if time is not None:
        close(time, block)

    faults: list[RuntimeFault] = []
    for (module, function, exception), hits in grouped.items():
        source = root / "src" / PACKAGE / f"{module}.py"
        if not source.is_file():
            continue
        modified = source.stat().st_mtime
        recent = [hit for hit in hits if _log_time(hit[0]) > modified]
        if len(recent) < MIN_FAULT_OCCURRENCES:
            continue
        last_time, last_line, last_trace = recent[-1]
        faults.append(
            RuntimeFault(
                module, function, last_line, exception, len(recent), last_time, last_trace
            )
        )
    return sorted(faults, key=lambda fault: (-fault.count, fault.module, fault.function))


def runtime_fault_needle(fault: RuntimeFault) -> str:
    """The prefix every objective built from ``fault`` starts with."""
    return (
        f"Fix a real bug the running mesh hit: `{fault.exception}` escaping "
        f"`{fault.function}` in `src/evomesh/{fault.module}.py`"
    )


def runtime_fault_objective(fault: RuntimeFault) -> str:
    return "\n".join(
        (
            f"{runtime_fault_needle(fault)} (line {fault.line}). The mesh's own "
            f"log recorded it {fault.count} times since that file last changed, "
            f"most recently at {fault.last_seen}. This is a failure the system "
            "actually suffered while running, not a hypothetical.",
            f"The last occurrence, verbatim:\n{fault.traceback}",
            f"Read `{fault.function}` first and work out why this happens. Fix the "
            "cause where it originates -- handle the condition, or guard the call "
            "-- rather than hiding it behind a bare `except Exception: pass`. The "
            "fix must change src/evomesh/; add a test that reproduces the "
            "condition if you can write one in the steps you have.",
        )
    )


# The scout: when the improvement backlog has nothing left to hand out, the
# Evolver spends one generation finding new items instead of falling back to
# test-writing. Found 2026-09-24: a hand-seeded backlog of four items was used
# up within the hour, and a backlog only a human can refill stops being the
# mesh's own the moment that human is away.
SCOUT_NEEDLE = "Refill the improvement backlog"
SCOUT_MAX_ITEMS = 5
# Below this, a "detail" is a restated title, not a where-and-why a small model
# can act on without re-deriving the whole problem itself.
SCOUT_MIN_DETAIL_CHARS = 80
_DONE_ITEM = re.compile(r"^- \[[xX]\] (?P<title>\S.*?)\s*$")
_SOURCE_PATH = re.compile(r"src/evomesh/(?P<module>\w+)\.py")
_CALLED_NAME = re.compile(r"`(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*)\(\)`")
_LEVEL_WARNING = ("WARNING", "ERROR", "CRITICAL")


def done_improvements(root: Path) -> list[str]:
    """Titles of every ticked ``- [x]`` item, in file order."""
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return []
    return [
        match.group("title")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if (match := _DONE_ITEM.match(line))
    ]


def recurring_warnings(root: Path, limit: int = 8) -> list[tuple[int, str]]:
    """The most frequent WARNING/ERROR messages in the mesh log's tail, with
    numbers masked so one message in many variants counts as one.

    Not tracebacks (that is :func:`runtime_faults`) -- the handled-but-noisy
    kind: a poll that keeps failing, a watcher that keeps timing out. Each is
    a lead for the scout, not an objective by itself.
    """
    path = root / RUNTIME_LOG
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - RUNTIME_LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    counts: dict[str, int] = {}
    for line in text.splitlines():
        entry = _LOG_ENTRY.match(line)
        if entry is None or entry.group("level") not in _LEVEL_WARNING:
            continue
        message = line.split('"message":"', 1)[-1].rstrip('"}')
        key = re.sub(r"\d+", "N", message)[:160]
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(count, message) for message, count in ranked[:limit]]


def scout_objective(root: Path) -> str:
    done = done_improvements(root)
    warnings = recurring_warnings(root)
    leads = (
        "\n".join(f"  - {count}x {message}" for count, message in warnings)
        if warnings
        else "  (none in the current log)"
    )
    lines = [
        f"{SCOUT_NEEDLE} in {IMPROVEMENTS_FILE.as_posix()}: every item there is done "
        "or has been set aside, so the next generation has nothing substantive to "
        f"work on. Your job this generation is to FIND 2 to {SCOUT_MAX_ITEMS} real "
        "problems or missing capabilities in EvoMesh and write them down as new "
        "items -- not to fix any of them.",
        "Where to look, most valuable first:",
        "1. What the running mesh keeps logging (WARNING/ERROR, numbers masked). "
        "The log reaches back further than the code: a line here may come from "
        "before a done item below fixed it, so read the code before trusting one.",
        leads,
        "2. The code itself: a function that does the wrong thing on an input it "
        "really gets, error handling that swallows the cause, a fixed number that "
        "should come from settings, a loop with no backoff, work repeated on every "
        "cycle that could be cached, something README.md or CLAUDE.md promises "
        "that the code does not actually do.",
        "Rules for every item:",
        "- Read the code before writing the item. Name the real file as "
        "src/evomesh/<module>.py and the real function or class in backticks, "
        "copied from what you read. An item naming a file or a function that does "
        "not exist is dropped automatically.",
        "- Say what is wrong today, how you know (what you read, or the log line), "
        "and what the change should be -- concretely enough that someone with "
        "about a hundred tool calls can do it in one or two files.",
        "- It must change behavior in src/evomesh/. No items that only add tests, "
        "docs, comments, type hints or renames: those are dropped too.",
        "- Append the items at the end of the file, in exactly this shape (the "
        "detail lines indented by four spaces):",
        "- [ ] <short imperative title>",
        "    <where: file and function> <what is wrong today and how you know>",
        "    <what the change should be>",
        f"- Edit only {IMPROVEMENTS_FILE.as_posix()}. Never tick an item and never "
        "remove one.",
    ]
    if done:
        lines.append(
            "Already done -- do not propose any of these again:\n"
            + "\n".join(f"  - {title}" for title in done[-30:])
        )
    return "\n".join(lines)


def vet_new_improvements(
    before: list[Improvement], root: Path
) -> tuple[list[Improvement], list[tuple[Improvement, str]]]:
    """Split the open items ``root`` gained over ``before`` into kept and
    dropped-with-a-reason.

    The scout's output becomes the next generations' objectives verbatim, so
    anything a model recalled rather than read has to stop here: an item has
    to name a source file that exists, every backticked ``name()`` in it has
    to be defined somewhere in the package, and ``module.symbol`` mentions go
    through the same :func:`fabricated_references` check plans already do.
    """
    known = {item.title.casefold() for item in before}
    known.update(title.casefold() for title in done_improvements(root))
    defined: set[str] = set()
    for module in survey(root):
        defined.update(module.all_names)
    existing = {module.name for module in survey(root)}
    kept: list[Improvement] = []
    dropped: list[tuple[Improvement, str]] = []
    for item in open_improvements(root):
        if item.title.casefold() in known:
            continue
        known.add(item.title.casefold())
        text = f"{item.title}\n{item.detail}"
        modules = {match.group("module") for match in _SOURCE_PATH.finditer(text)}
        missing = sorted(
            {
                match.group("name")
                for match in _CALLED_NAME.finditer(text)
                if match.group("name") not in defined
            }
        )
        if len(item.detail) < SCOUT_MIN_DETAIL_CHARS:
            reason = "its detail is too short to act on"
        elif not modules:
            reason = "it names no src/evomesh/<module>.py file"
        elif not modules <= existing:
            unknown = ", ".join(sorted(modules - existing))
            reason = f"it names a file that does not exist ({unknown})"
        elif missing:
            reason = f"it names functions that do not exist ({', '.join(missing)})"
        elif fabricated := fabricated_references(text, root):
            reason = f"it names symbols that do not exist ({', '.join(fabricated)})"
        else:
            kept.append(item)
            continue
        dropped.append((item, reason))
    return kept[:SCOUT_MAX_ITEMS], dropped + [
        (item, "over the per-refill limit") for item in kept[SCOUT_MAX_ITEMS:]
    ]


def drop_improvements(root: Path, titles: set[str]) -> None:
    """Remove the open items named in ``titles`` (and their detail lines)."""
    path = root / IMPROVEMENTS_FILE
    if not titles or not path.is_file():
        return
    kept: list[str] = []
    skipping = False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        match = _OPEN_ITEM.match(line.rstrip("\r\n"))
        if match is not None:
            skipping = match.group("title") in titles
        elif skipping and not line.startswith((" ", "\t")):
            skipping = False
        if not skipping:
            kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
