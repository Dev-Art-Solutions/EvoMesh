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
import fnmatch
import re
from collections.abc import Iterable
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


# What a candidate may not change on its own (closure plan 18.4): the rules
# that admit, verify and promote work, the acceptance assertions that judge
# it, and the reviewed approvals. A change here needs a human's review, so a
# candidate cannot pass by rewriting its own oracle.
PROTECTED_PATHS: tuple[str, ...] = (
    "src/evomesh/procedures.py",
    "src/evomesh/procedure_runtime.py",
    "src/evomesh/procedure_host.py",
    "src/evomesh/procedure_traces.py",
    "src/evomesh/improvements.py",
    "procedures/*",
    "plans/*",
    "docs/architecture/closure-evidence/*",
    "tests/test_procedure*.py",
    "tests/test_w3_improvement.py",
    "tests/test_w3_live.py",
    "tests/test_delegation_contract.py",
    "tests/test_idle_evolution.py",
    "tests/test_work_executor.py",
    "tests/test_protected_surface.py",
    "tests/test_acceptance_manifest.py",
    "benchmarks/closure/*",
    "evomesh.yaml",
    "evomesh.secrets.yaml",
)


def protected_changes(changed: Iterable[str]) -> list[str]:
    """The changed paths (repository-relative, forward slashes) that fall
    on the protected surface."""
    return sorted(
        {
            path
            for path in (item.replace("\\", "/").strip() for item in changed)
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in PROTECTED_PATHS)
        }
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
# `name.<suffix>` in backticks is a file name, never a symbol.
FILE_SUFFIXES = frozenset(
    {"py", "md", "json", "jsonl", "yaml", "yml", "toml", "txt", "log", "db", "lock", "cfg", "ini"}
)


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
        if symbol in FILE_SUFFIXES:
            # `cycles.py` -- naming the file itself, not a symbol in it. This
            # is how a plan almost always refers to a module by name. Found
            # live 2026-09-25: `memory.md` (an agent's memory file) dropped a
            # scouted item as a made-up `memory` symbol.
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
        "the one obvious thing about its result -- one real, passing check is a "
        "complete answer, not every branch."
        if is_function
        else (
            f"Construct one `{bare_name}` the way an existing test constructs "
            "something similar, call one real method on it, and assert one "
            "obvious thing about the result."
        )
    )
    # The rest -- its code, the test file's edges, no invented mocks, where the
    # test goes -- is the work order's (write_test_task), not repeated here:
    # this text is also the needle, MUTATION_OBJECTIVE.md and the review's brief.
    lines = [
        f"Write ONE small, mechanical test for `{name}` in "
        f"`src/evomesh/{module.name}.py`. It is exported and load-bearing "
        f"(used by {importers} other module{'s' if importers != 1 else ''}), "
        "but its name does not appear anywhere under tests/, so it has no "
        "direct test coverage right now.",
        how,
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
# Up to three leading spaces is still a top-level list item in Markdown. Found
# live 2026-09-24: generation 1385's scout wrote a good item as " - [ ] ...",
# one space in, and it silently became detail lines of the item above it.
_OPEN_ITEM = re.compile(r"^ {0,3}- \[ \] (?P<title>\S.*?)\s*$")
# One step of an item: one change to one existing function, method or
# constant in one file, e.g.
#     1. [ ] src/evomesh/evolution.py `GenerationSupervisor.discard` -- remember it
# Found 2026-09-24 reading the transcripts of three generations that failed an
# improvements.md item: a harness job's transcript is harness.transcript_chars
# (12000) and the task alone took 6.6-7.9K of it, so what was left held about
# one 4000-char read. A job that read `GenerationSupervisor`, then console.py's
# handler, had lost the first by the time it edited, wrote an `old` from memory
# (an invented `track_success`), and spent forty steps sure its read tool was
# lying. A step is the unit that fits: the pipeline hands the job the anchored
# function's current source up front, so there is nothing left to navigate.
_STEP = re.compile(
    r"^\s+(?P<number>\d+)\.\s*\[(?P<done>[ xX])\]\s*"
    # The path may come in backticks too (generation 1386 wrote every step so).
    r"`?(?P<path>src/evomesh/\w+\.py)`?\s+"
    r"`(?P<symbol>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)`"
    r"\s*(?:--|:|-|—|–)?\s*(?P<change>.*?)\s*$"
)
# More than this and an item is a project, not a backlog entry: every step is
# a whole generation (propose, validate, review) of its own.
MAX_ITEM_STEPS = 4


@dataclass(frozen=True)
class Step:
    number: int
    path: str
    symbol: str
    change: str
    done: bool = False

    def describe(self) -> str:
        return f"{self.path} `{self.symbol}` -- {self.change}"


@dataclass(frozen=True)
class Improvement:
    title: str
    detail: str = ""
    steps: tuple[Step, ...] = ()

    @property
    def next_step(self) -> Step | None:
        """The first step not landed yet, or ``None`` (no steps, or all done)."""
        return next((step for step in self.steps if not step.done), None)

    @property
    def source_paths(self) -> list[str]:
        """Every ``src/evomesh/<module>.py`` the item names, first mention first."""
        text = "\n".join((self.title, self.detail, *(step.path for step in self.steps)))
        seen: dict[str, None] = {}
        for match in _SOURCE_PATH.finditer(text):
            seen.setdefault(match.group(0), None)
        return list(seen)


def _continues(line: str) -> bool:
    """Whether ``line`` belongs to the item above it: indented or blank, and
    not an item of its own -- which may itself be indented up to three spaces."""
    bare = line.rstrip("\r\n")
    if _OPEN_ITEM.match(bare) or _DONE_ITEM.match(bare):
        return False
    return not bare.strip() or bare.startswith((" ", "\t"))


def _parse_item(title: str, block: list[str]) -> Improvement:
    detail: list[str] = []
    steps: list[Step] = []
    for line in block:
        match = _STEP.match(line)
        if match is None:
            detail.append(line.strip())
            continue
        steps.append(
            Step(
                number=int(match.group("number")),
                path=match.group("path"),
                symbol=match.group("symbol"),
                change=match.group("change"),
                done=match.group("done") != " ",
            )
        )
    return Improvement(title, "\n".join(detail).strip(), tuple(steps))


def open_improvements(root: Path) -> list[Improvement]:
    """Every unticked ``- [ ]`` item in the improvement backlog, in file order.

    An item's detail is whatever indented lines follow it, dedented -- the
    concrete where/why that turns a wish into something a small model can act
    on in one harness job. Indented lines in the step shape (see ``_STEP``)
    become its steps instead of detail.
    """
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return []
    items: list[Improvement] = []
    title: str | None = None
    block: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _OPEN_ITEM.match(line)
        if match is not None:
            if title is not None:
                items.append(_parse_item(title, block))
            title, block = match.group("title"), []
        elif title is not None and _continues(line):
            block.append(line)
        elif title is not None:
            items.append(_parse_item(title, block))
            title, block = None, []
    if title is not None:
        items.append(_parse_item(title, block))
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


def step_needle(item: Improvement, step: Step) -> str:
    """The prefix of the objective for one step. ``[step 1]`` is never a
    prefix of ``[step 10]``, so the look-back counts each step on its own."""
    return f"{improvement_needle(item)} [step {step.number}]"


def step_objective(item: Improvement, step: Step) -> str:
    """One step of ``item``, and just enough of the rest to keep it in scope.

    Short on purpose: :func:`step_task` adds the anchored code and the rules
    when the job is built, from the candidate itself, and everything here also
    lands in MUTATION_OBJECTIVE.md and the review's prompt.
    """
    lines = [
        f"{step_needle(item, step)} -- one step of a larger item; do this step "
        "and nothing else.",
        f"THIS STEP: in {step.path}, `{step.symbol}`: {step.change}",
    ]
    if item.detail:
        # Context, not the task: a long item's whole essay does not belong in
        # every one of its steps' prompts.
        why = item.detail if len(item.detail) <= 600 else f"{item.detail[:600]} [...]"
        lines.append(f"Why the item exists (context, not your task): {why}")
    landed = [other for other in item.steps if other.done]
    later = [other for other in item.steps if not other.done and other.number != step.number]
    if landed:
        lines.append(
            "Already landed: " + "; ".join(f"{s.number}. {s.describe()}" for s in landed)
        )
    if later:
        lines.append(
            "Later steps, NOT this one: "
            + "; ".join(f"{s.number}. {s.describe()}" for s in later)
        )
    lines.append(
        f"Do not edit {IMPROVEMENTS_FILE.as_posix()}: the pipeline ticks this step "
        "off once your change lands."
    )
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


def tick_step(root: Path, title: str, number: int) -> bool:
    """Mark step ``number`` of open item ``title`` done, and the item itself
    once that was its last open step; ``False`` if there is no such open step."""
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    inside = False
    for index, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if (match := _OPEN_ITEM.match(bare)) is not None:
            inside = match.group("title") == title
            continue
        if inside and not _continues(bare):
            inside = False
        step = _STEP.match(bare) if inside else None
        if step is not None and int(step.group("number")) == number and step.group("done") == " ":
            lines[index] = line.replace("[ ]", "[x]", 1)
            path.write_text("".join(lines), encoding="utf-8")
            item = next((item for item in open_improvements(root) if item.title == title), None)
            if item is not None and item.next_step is None:
                tick_improvement(root, title)
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
#
# One module per scout, its outline handed over up front, and one item with
# steps as the answer. The first version asked for "2 to 5 real problems in
# EvoMesh" -- 17k lines, through a transcript that holds one read at a time:
# generation 1377 read 31 file windows, lost each one to the next, and wrote
# nothing at all.
SCOUT_NEEDLE = "Refill the improvement backlog"
SCOUT_MAX_ITEMS = 2
# Below this, an item is a restated title, not a where-and-why a small model can
# act on without re-deriving the whole problem itself. Its steps count: they are
# the most concrete part of it.
SCOUT_MIN_DETAIL_CHARS = 80
# `[x]` done, `[-]` rejected by a human: closed either way -- never handed out,
# never proposed again by a scout.
_DONE_ITEM = re.compile(r"^ {0,3}- \[[xX-]\] (?P<title>\S.*?)\s*$")
_SOURCE_PATH = re.compile(r"src/evomesh/(?P<module>\w+)\.py")
_CALLED_NAME = re.compile(r"`(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*)\(\)`")
_LEVEL_WARNING = ("WARNING", "ERROR", "CRITICAL")
_STEP_SHAPE = "N. [ ] src/evomesh/<module>.py `<Name or Class.method>` -- <the change>"


def done_improvements(root: Path) -> list[str]:
    """Titles of every closed item -- ``- [x]`` done or ``- [-]`` rejected --
    in file order."""
    path = root / IMPROVEMENTS_FILE
    if not path.is_file():
        return []
    return [
        match.group("title")
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if (match := _DONE_ITEM.match(line))
    ]


def _warning_entries(root: Path) -> list[tuple[float, str]]:
    """``(time, message)`` for every WARNING/ERROR line in the mesh log's
    tail, numbers masked so one message in many variants reads as one."""
    path = root / RUNTIME_LOG
    if not path.is_file():
        return []
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - RUNTIME_LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    entries: list[tuple[float, str]] = []
    for line in text.splitlines():
        entry = _LOG_ENTRY.match(line)
        if entry is None or entry.group("level") not in _LEVEL_WARNING:
            continue
        message = line.split('"message":"', 1)[-1].rstrip('"}')
        entries.append((_log_time(entry.group("time")), re.sub(r"\d+", "N", message)[:160]))
    return entries


def recurring_warnings(root: Path, limit: int = 8) -> list[tuple[int, str]]:
    """The most frequent WARNING/ERROR messages in the mesh log's tail, with
    numbers masked so one message in many variants counts as one.

    Not tracebacks (that is :func:`runtime_faults`) -- the handled-but-noisy
    kind: a poll that keeps failing, a watcher that keeps timing out. Each is
    a lead for the scout, not an objective by itself.
    """
    counts: dict[str, int] = {}
    for _, message in _warning_entries(root):
        counts[message] = counts.get(message, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(count, message) for message, count in ranked[:limit]]


def _module_logging(message: str, sources: dict[str, tuple[str, float]]) -> str | None:
    """The module whose source holds the longest leading run (three to eight
    words) of ``message`` -- where that log line is written."""
    words = message.split()
    for size in range(min(8, len(words)), 2, -1):
        prefix = " ".join(words[:size])
        for name, (text, _) in sources.items():
            if prefix in text:
                return name
    return None


def warning_leads(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Recurring WARNING/ERROR messages keyed by the module that logs them,
    counting only what was logged since that file last changed.

    The same notion of "fixed" :func:`runtime_faults` uses. Found live: an
    hour after the two items that fixed them had landed, the scout's leads
    were still those same two warnings, because the log remembers further
    back than the code does.
    """
    entries = _warning_entries(root)
    if not entries:
        return {}
    sources = {
        path.stem: (path.read_text(encoding="utf-8", errors="replace"), path.stat().st_mtime)
        for path in sorted(package_root(root).glob("*.py"))
    }
    grouped: dict[str, list[float]] = {}
    for time, message in entries:
        grouped.setdefault(message, []).append(time)
    leads: dict[str, list[tuple[int, str]]] = {}
    for message, times in grouped.items():
        module = _module_logging(message, sources)
        if module is None:
            continue
        count = sum(time > sources[module][1] for time in times)
        if count >= MIN_FAULT_OCCURRENCES:
            leads.setdefault(module, []).append((count, message))
    for found in leads.values():
        found.sort(key=lambda pair: (-pair[0], pair[1]))
    return leads


def scout_modules(root: Path, leads: dict[str, list[tuple[int, str]]]) -> list[str]:
    """The modules worth a scout, most promising first: the ones the log's
    warnings point into, most-warned first, then every other live module."""
    live = {
        module.name
        for module in survey(root)
        if not module.is_orphan and not module.name.startswith("_")
    }
    warned = sorted(
        (name for name in leads if name in live),
        key=lambda name: (-sum(count for count, _ in leads[name]), name),
    )
    return warned + sorted(live - set(warned))


def scout_needle(module: str) -> str:
    """The prefix of a scout objective aimed at ``module``."""
    return f"{SCOUT_NEEDLE} from src/evomesh/{module}.py"


def scout_objective(
    root: Path, module: str, leads: list[tuple[int, str]] | None = None
) -> str:
    done = done_improvements(root)
    summary = next((item.summary for item in survey(root) if item.name == module), "")
    lines = [
        f"{scout_needle(module)}: {IMPROVEMENTS_FILE.as_posix()} has nothing left "
        "to hand out. Find ONE real problem or missing capability in "
        f"src/evomesh/{module}.py and write it down as one new item with its "
        "steps -- do not fix it.",
    ]
    if summary:
        lines.append(f"What the module is for: {summary}")
    if leads:
        lines.append(
            "What the running mesh logged from it since the file last changed "
            "(numbers masked) -- the strongest lead there is:\n"
            + "\n".join(f"  - {count}x {message}" for count, message in leads[:3])
        )
    lines.append(
        "Otherwise look for: a function that does the wrong thing on an input it "
        "really gets, error handling that swallows the cause, a fixed number that "
        "should come from settings, a loop with no backoff, work repeated every "
        "cycle that could be cached."
    )
    if done:
        lines.append(
            "Already done -- do not propose these again:\n"
            + "\n".join(f"  - {title}" for title in done[-15:])
        )
    return "\n".join(lines)


# A scout's evidence: code it quotes, one `> ` detail line each, which has to be
# in a file the item names -- the same "copied, not recalled" rule an edit's
# `old` lives by. Found live 2026-09-24: generation 1386 scouted agent_label.py
# and wrote an item about its "[?]" fallback, its ValueError and its role sets.
# The real function is one line -- `return _AGENT_LABELS.get(role, "agent")` --
# and every anchor the item named existed, so nothing else would have caught it.
_QUOTE = re.compile(r"^>\s?`?(?P<code>.*?)`?\s*$")
# Shorter than this, a quote (`return`, `pass`) is in every file by luck.
MIN_QUOTE_CHARS = 8


def _quotes(item: Improvement) -> list[str]:
    return [
        code
        for line in item.detail.splitlines()
        if (match := _QUOTE.match(line))
        and len(code := match.group("code").strip()) >= MIN_QUOTE_CHARS
    ]


def _misquoted(root: Path, item: Improvement, quotes: list[str]) -> list[str]:
    """The quotes that appear on no line of any file ``item`` names."""
    lines: list[str] = []
    for path in item.source_paths:
        file = root / path
        if file.is_file():
            text = file.read_text(encoding="utf-8", errors="replace")
            lines.extend(line.strip() for line in text.splitlines())
    return [code for code in quotes if not any(code in line for line in lines)]


_ATTRIBUTE_SPAN = re.compile(r"^([a-z_]\w*)\.([A-Za-z_]\w*)(?:\(\))?$")
_IMPORTED_NAME = re.compile(r"^\s*(?:import|from)\s+(\w+)", re.MULTILINE)


def _unknown_attributes(root: Path, item: Improvement) -> list[str]:
    """Backticked ``thing.attribute`` spans in the item's detail (what the code
    does *today*) whose attribute is not a word anywhere in the files the item
    names. Found live 2026-09-25: a scout built its whole item on
    `param.annotation`, a field ToolParameter never had -- the module.symbol
    check misses it because `param` is not a module. Imported names (httpx,
    asyncio, ...) and project modules (checked by fabricated_references) are
    skipped."""
    words: set[str] = set()
    imported: set[str] = set()
    for path in item.source_paths:
        file = root / path
        if file.is_file():
            text = file.read_text(encoding="utf-8", errors="replace")
            words.update(re.findall(r"\w+", text))
            imported.update(_IMPORTED_NAME.findall(text))
    if not words:
        return []
    modules = {module.name for module in survey(root)}
    unknown: list[str] = []
    for span in _BACKTICK_RE.findall(item.detail):
        match = _ATTRIBUTE_SPAN.match(span.strip())
        if match is None:
            continue
        owner, attribute = match.groups()
        if owner in modules or owner in imported or attribute in FILE_SUFFIXES:
            continue
        if attribute not in words and span not in unknown:
            unknown.append(span)
    return unknown


def _unanchored(root: Path, item: Improvement) -> list[str]:
    return [
        f"{step.path} `{step.symbol}`"
        for step in item.steps
        if find_symbol(root, step.path, step.symbol) is None
    ]


def vet_new_improvements(
    before: list[Improvement], root: Path
) -> tuple[list[Improvement], list[tuple[Improvement, str]]]:
    """Split the open items ``root`` gained over ``before`` into kept and
    dropped-with-a-reason.

    The scout's output becomes the next generations' objectives verbatim, so
    anything a model recalled rather than read has to stop here: an item has
    to carry steps, every step's anchor has to exist in the file it names
    (:func:`find_symbol`), every backticked ``name()`` has to be defined
    somewhere in the package, and ``module.symbol`` mentions go through the
    same :func:`fabricated_references` check plans already do.
    """
    known = {item.title.casefold() for item in before}
    known.update(title.casefold() for title in done_improvements(root))
    modules_now = survey(root)
    defined: set[str] = set()
    for module in modules_now:
        defined.update(module.all_names)
    existing = {module.name for module in modules_now}
    kept: list[Improvement] = []
    dropped: list[tuple[Improvement, str]] = []
    for item in open_improvements(root):
        if item.title.casefold() in known:
            continue
        known.add(item.title.casefold())
        text = "\n".join((item.title, item.detail, *(step.describe() for step in item.steps)))
        modules = {match.group("module") for match in _SOURCE_PATH.finditer(text)}
        missing = sorted(
            {
                match.group("name")
                for match in _CALLED_NAME.finditer(text)
                if match.group("name") not in defined
            }
        )
        size = len(item.detail) + sum(len(step.change) for step in item.steps)
        if size < SCOUT_MIN_DETAIL_CHARS:
            reason = "its detail is too short to act on"
        elif not item.steps:
            reason = f"it has no steps in the shape `{_STEP_SHAPE}`"
        elif len(item.steps) > MAX_ITEM_STEPS:
            reason = f"it has {len(item.steps)} steps, more than the {MAX_ITEM_STEPS} one item may"
        elif not modules <= existing:
            unknown = ", ".join(sorted(modules - existing))
            reason = f"it names a file that does not exist ({unknown})"
        elif unanchored := _unanchored(root, item):
            reason = f"its steps name code that does not exist ({', '.join(unanchored)})"
        elif missing:
            reason = f"it names functions that do not exist ({', '.join(missing)})"
        elif fabricated := fabricated_references(text, root):
            reason = f"it names symbols that do not exist ({', '.join(fabricated)})"
        elif invented := _unknown_attributes(root, item):
            reason = (
                "it describes attributes its files never mention "
                f"({', '.join(invented)}): read the code, do not recall it"
            )
        elif not (quotes := _quotes(item)):
            reason = (
                "it quotes no code: the line that shows the problem, copied from the "
                "file, goes on a detail line of its own starting with `> `"
            )
        elif misquoted := _misquoted(root, item, quotes):
            reason = f"it quotes code that is in none of its files: {misquoted[0]!r}"
        else:
            kept.append(item)
            continue
        dropped.append((item, reason))
    return kept[:SCOUT_MAX_ITEMS], dropped + [
        (item, "over the per-refill limit") for item in kept[SCOUT_MAX_ITEMS:]
    ]


# Planning, for an item a human wrote without steps: one harness job that adds
# them, checked by code instead of by another model. It replaces what
# evolution.auto_plan's draft -> evaluate -> decompose asked for (three or more
# 12-step jobs of free prose per generation, off since 2026-09-19 after five
# generations in a row died inside them without ever reaching propose): the
# steps are the plan, and "does this anchor exist" is the evaluation.
def plan_needle(item: Improvement) -> str:
    """The prefix of the objective that splits ``item`` into steps."""
    return f"Plan this improvement to EvoMesh: {item.title}"


def plan_objective(item: Improvement) -> str:
    lines = [
        f"{plan_needle(item)}",
        "Split this backlog item into 1 to 3 small steps and write them under it "
        f"in {IMPROVEMENTS_FILE.as_posix()}. Do not change any code: each step "
        "becomes one later generation's whole objective.",
        f"THE ITEM: {item.title}",
    ]
    if item.detail:
        lines.append(item.detail)
    return "\n".join(lines)


def vet_plan(before: list[Improvement], root: Path, title: str) -> str | None:
    """Why the steps a plan generation wrote under ``title`` in ``root``'s
    backlog cannot be used, or ``None`` when every one of them can."""
    after = open_improvements(root)
    item = next((item for item in after if item.title == title), None)
    if item is None:
        return "the item it was planning is gone from the backlog"
    if {entry.title for entry in after} != {entry.title for entry in before}:
        return "it added, removed or renamed backlog items; a plan only adds steps"
    if not item.steps:
        return f"it wrote no step under the item in the shape `{_STEP_SHAPE}`"
    if len(item.steps) > MAX_ITEM_STEPS:
        return f"it wrote {len(item.steps)} steps, more than the {MAX_ITEM_STEPS} one item may"
    if unanchored := _unanchored(root, item):
        return f"its steps name code that does not exist ({', '.join(unanchored)})"
    return None


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
        elif skipping and not _continues(line):
            skipping = False
        if not skipping:
            kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")


# -- Work orders: code cut to fit a small context window ----------------------
# What a harness job for a step, a plan or a scout is handed up front, so it
# starts working instead of navigating. Numbered exactly like the harness's own
# `read` (`{n:>5}| line`), so an `edit` anchor copied from here is copied the
# same way as one from a read. The budgets leave most of a 12000-char
# transcript for the job's own reads and edits.
EXCERPT_CHARS = 3000
OUTLINE_CHARS = 2400
# Shared by the (at most two) files a plan's item names. A plan job reads less
# than a step job and anchors more, so its outlines get more of the room.
PLAN_OUTLINE_CHARS = 3600
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _named(node: ast.stmt, name: str) -> bool:
    if isinstance(node, _DEFS):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def _symbol_node(tree: ast.Module, symbol: str) -> ast.stmt | None:
    """A top-level function, class or constant, or ``Class.member``."""
    head, _, member = symbol.partition(".")
    owner = next((node for node in tree.body if _named(node, head)), None)
    if owner is None or not member:
        return owner
    if not isinstance(owner, ast.ClassDef):
        return None
    return next((node for node in owner.body if _named(node, member)), None)


def _parse(root: Path, path: str) -> tuple[list[str], ast.Module] | None:
    file = root / path
    if not file.is_file():
        return None
    text = file.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    return text.splitlines(), tree


def _span(node: ast.stmt) -> tuple[int, int]:
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(decorator.lineno for decorator in decorators)])
    return start, node.end_lineno or node.lineno


def find_symbol(root: Path, path: str, symbol: str) -> tuple[int, int] | None:
    """The line span of ``symbol`` in ``root / path``, or ``None`` if it is not there."""
    parsed = _parse(root, path)
    if parsed is None:
        return None
    node = _symbol_node(parsed[1], symbol)
    return _span(node) if node is not None else None


def _numbered(lines: list[str], start: int, end: int) -> list[str]:
    return [f"{number:>5}| {lines[number - 1]}" for number in range(start, end + 1)]


def _take(rows: list[str], budget: int) -> list[str]:
    """The longest run of whole rows from the top that fits ``budget``."""
    kept: list[str] = []
    size = 0
    for row in rows:
        size += len(row) + 1
        if size > budget:
            break
        kept.append(row)
    return kept


def symbol_excerpt(
    root: Path, path: str, symbol: str, budget: int = EXCERPT_CHARS
) -> str | None:
    """``symbol``'s current source, numbered, or as much of it as fits.

    A class too long to show whole is shown as its outline (header and one
    line per member); a function too long is cut at a line, and the cut says
    exactly which ``read`` returns the rest.
    """
    parsed = _parse(root, path)
    if parsed is None:
        return None
    lines, tree = parsed
    node = _symbol_node(tree, symbol)
    if node is None:
        return None
    start, end = _span(node)
    rows = _numbered(lines, start, end)
    if len("\n".join(rows)) <= budget:
        return "\n".join(rows)
    if isinstance(node, ast.ClassDef):
        members = [child for child in node.body if isinstance(child, _DEFS)]
        head_end = _span(members[0])[0] - 1 if members else end
        head = _numbered(lines, start, min(head_end, start + 11))
        outline = [f"{child.lineno:>5}| {lines[child.lineno - 1]}" for child in members]
        shown = _take([*head, "  ...", *outline], budget)
        return "\n".join(shown) + (
            f"\n[`{symbol}` is lines {start}-{end}, too long to show whole -- that is "
            "its outline. read offset=<line> limit=40 for the member you change.]"
        )
    shown = _take(rows, budget)
    resume = start + len(shown)
    return "\n".join(shown) + (
        f"\n[... lines {resume}-{end} not shown: read offset={resume} "
        f"limit={end - resume + 1} for them ...]"
    )


def outline_focus(item: Improvement) -> frozenset[str]:
    """What an outline of a file ``item`` names should keep in view: every
    word of a backticked name in it, and the title's longer words."""
    text = f"{item.title}\n{item.detail}"
    words = {
        word.lower()
        for quoted in re.findall(r"`([^`]+)`", text)
        for word in re.findall(r"[A-Za-z_][A-Za-z_]{3,}", quoted)
    }
    words.update(word.lower() for word in re.findall(r"[A-Za-z]{5,}", item.title))
    return frozenset(words)


def module_outline(
    root: Path, path: str, budget: int = OUTLINE_CHARS, focus: frozenset[str] = frozenset()
) -> str | None:
    """One numbered line per function and class in ``path``, methods included,
    so a job can pick an anchor without reading the file.

    What does not fit goes in this order: methods ``focus`` does not mention,
    then top-level definitions it does not mention. Found on the live tree:
    cut from the top alone, evolution.py's outline stopped 150 lines before the
    `GenerationSupervisor` its item was about, and console.py's showed one
    class with "44 methods" and not the `_command_evolution` the item meant.
    """
    parsed = _parse(root, path)
    if parsed is None:
        return None
    lines, tree = parsed

    def hit(name: str) -> bool:
        bare = name.lstrip("_").lower()
        return any(word in bare or (len(bare) >= 5 and bare in word) for word in focus)

    # (line, row, rank): 0 is in focus, 1 an unfocused top-level definition,
    # 2 an unfocused method -- the first thing to go.
    rows: list[tuple[int, str, int]] = []
    for node in tree.body:
        if not isinstance(node, _DEFS):
            continue
        row = f"{node.lineno:>5}| {lines[node.lineno - 1].strip()[:110]}"
        if isinstance(node, ast.ClassDef):
            members = [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            # Every member only for a class named outright; a class whose name
            # merely contains a title word ("Evolver's" in EnvironmentEvolver,
            # 65 methods) keeps just the members that match on their own.
            whole = node.name.lower() in focus
            rows.extend(
                (
                    child.lineno,
                    f"{child.lineno:>5}|     {lines[child.lineno - 1].strip()[:100]}",
                    0 if whole or hit(child.name) else 2,
                )
                for child in members
            )
            row += f"  ({len(members)} methods, lines {node.lineno}-{node.end_lineno})"
        rows.append((node.lineno, row, 0 if hit(node.name) else 1))
    if not rows:
        return "(no functions or classes)"
    rows.sort()
    for allowed in (2, 1):
        chosen = [row for _, row, rank in rows if rank <= allowed]
        if len("\n".join(chosen)) <= budget:
            return "\n".join(chosen)
    candidates = sorted((rank, line, row) for line, row, rank in rows if rank <= 1)
    kept = _take([row for _, _, row in candidates], budget)
    shown = sorted(candidates[: len(kept)], key=lambda entry: entry[1])
    return "\n".join(row for _, _, row in shown) + (
        f"\n[... {len(candidates) - len(kept)} more definitions: grep 'def ' in {path} ...]"
    )


def _step_rules(path: str) -> str:
    return "\n".join(
        (
            "Rules for this step -- your working memory is small, and what this step "
            "needs is already above:",
            "- Start by editing, not by searching. CURRENT CODE is the file as it is "
            "now; read more only for a line it does not show, with offset and limit, "
            "never the whole file.",
            "- Copy `old` character-for-character from CURRENT CODE, without the "
            "`NNNNN| ` prefix (the number, the bar and exactly ONE space), and keep "
            "it to the few lines you change.",
            f"- Change {path}. A test for the new behavior under tests/ is welcome; "
            "nothing else. Do this step only -- later steps are other generations' work.",
            "- Never create a new module under src/evomesh/: nothing would import it.",
            "- Do not run ruff, pyright or pytest: validation runs them once you stop. "
            "`shell` is a bare python with none of them installed.",
            "- Noticed a different problem on the way? Do not fix it here -- "
            "that is scope creep. Name it on its own line starting exactly with "
            "'PROPOSAL:' (what is wrong, and in which file); it goes to the backlog.",
            "- End with one line starting exactly with 'RATIONALE:' saying what you "
            "changed and why.",
        )
    )


_ANCHOR_RULE = (
    "- Each step changes ONE existing function, method or constant in ONE file, "
    "spelled exactly as in the OUTLINE (`Class.method` for a method); for a brand "
    "new function, anchor on the existing one it goes next to. A step naming "
    "anything that is not in its file is thrown away, and everything with it."
)
PLAN_RULES = "\n".join(
    (
        "Rules -- the steps are the plan, and your answer is where they go:",
        "- This job cannot edit anything and does not need to: END YOUR ANSWER with "
        "the steps, one per line, exactly:",
        "1. src/evomesh/<module>.py `<Name or Class.method>` -- <the change, in one "
        "sentence>",
        "- Write 1 to 3 steps. Order them so each works on its own once the ones "
        "before it have landed: validation runs after every step.",
        _ANCHOR_RULE,
        "- Read a function (offset and limit from the OUTLINE's line numbers) only "
        "when its name does not tell you enough.",
        "- Then one last line starting exactly with 'RATIONALE:'.",
    )
)
SCOUT_RULES = "\n".join(
    (
        "Rules -- one well-anchored item beats five vague ones:",
        "- Read two or three functions from the OUTLINE (offset and limit, never "
        "the whole file). A log line may predate a fix: read the code before "
        "trusting it.",
        "- It must change behavior in src/evomesh/: no item that only adds tests, "
        "docs, comments, type hints or renames.",
        # Found live 2026-09-25: a scout quoted a real line and reported weekday
        # 7 never matching Sunday; the line right above already did `% 7` and a
        # test covered it. The "fix" landed as five lines doing the same thing.
        "- Before you write it down, read the lines around your quote and grep "
        "tests/ for the function: if the code or a test already handles the "
        "case, it is not a problem -- look for another.",
        "- This job cannot edit anything and does not need to: END YOUR ANSWER with "
        "the item, exactly in this shape, with 1 to 3 steps:",
        "[ ] <short imperative title>",
        "<what is wrong today and how you know: the code you read, or the log line>",
        "> <a line of code that shows it, copied exactly from the file>",
        "1. src/evomesh/<module>.py `<Name or Class.method>` -- <the change, in one "
        "sentence>",
        "- Say only what the code you READ does, and prove it: at least one `> ` line "
        "copied character-for-character from the file (without the `NNNNN| ` "
        "prefix). An item whose quote is in none of its files is thrown away.",
        _ANCHOR_RULE,
        "- Then one last line starting exactly with 'RATIONALE:'.",
    )
)


def step_task(root: Path, objective: str, path: str, symbol: str) -> str:
    """The whole harness task for one step: the step, the anchored code as it
    is in ``root`` right now, and rules short enough to leave room to work."""
    excerpt = symbol_excerpt(root, path, symbol) or (
        f"(`{symbol}` is not in {path} any more -- grep for it first)"
    )
    return "\n\n".join(
        (objective, f"CURRENT CODE -- {path}, `{symbol}`:\n{excerpt}", _step_rules(path))
    )


def plan_task(root: Path, objective: str, title: str) -> str:
    """The whole harness task that splits item ``title`` into steps: the
    outlines of the (at most two) files it names. The item itself is already
    in ``objective``, and the answer, not an edit, is where the steps go."""
    item = next((item for item in open_improvements(root) if item.title == title), None)
    paths = item.source_paths[:2] if item is not None else []
    focus = outline_focus(item) if item is not None else frozenset[str]()
    parts = [objective]
    for path in paths:
        outline = module_outline(root, path, PLAN_OUTLINE_CHARS // len(paths), focus)
        if outline is not None:
            parts.append(f"OUTLINE -- {path} (line| definition):\n{outline}")
    if not paths:
        parts.append(
            "OUTLINE: the item names no src/evomesh/ file -- grep for the function "
            "it is about, and anchor on what you find."
        )
    parts.append(PLAN_RULES)
    return "\n\n".join(parts)


def scout_task(root: Path, objective: str, module: str) -> str:
    """The whole harness task for a scout of ``module``: its outline, and rules
    that put the item in the answer rather than in an edit."""
    path = f"src/evomesh/{module}.py"
    outline = module_outline(root, path) or "(the file is missing)"
    return "\n\n".join(
        (objective, f"OUTLINE -- {path} (line| definition):\n{outline}", SCOUT_RULES)
    )


# -- Work orders for the maintenance and repair jobs ---------------------------
# Found 2026-09-24 across the last 250 harness sessions: test-writing jobs were
# 107 of them (53% changed nothing, 40 capped) and repairs 55 -- both still on
# the full prompt (package map, skills catalog, HARNESS_RULES: 6.5-6.9K of a
# 12000-char transcript) and both left to find the code they were about.
def _file_tail(root: Path, path: str, count: int = 3) -> str:
    """The last ``count`` non-empty lines of ``path``, numbered: an append anchor."""
    lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    last = max((number for number, line in enumerate(lines, 1) if line.strip()), default=0)
    return "\n".join(_numbered(lines, max(1, last - count + 1), last)) if last else "(empty)"


def _file_head(root: Path, path: str, budget: int = 900) -> str:
    """``path`` from its top down to its first definition: its imports."""
    parsed = _parse(root, path)
    if parsed is None:
        return "(unreadable)"
    lines, tree = parsed
    first = next((node.lineno for node in tree.body if isinstance(node, _DEFS)), len(lines) + 1)
    return "\n".join(_take(_numbered(lines, 1, min(first - 1, len(lines))), budget))


def _test_rules(tests: str) -> str:
    return "\n".join(
        (
            "Rules for this test -- your working memory is small, and the code under "
            "test is already above:",
            f"- Add ONE small test to {tests}: call it with the simplest real "
            "arguments and assert the one obvious thing CODE UNDER TEST shows it "
            "does -- not what its name suggests it might do.",
            "- If the test fails, the test is what is wrong: fix the test. Never "
            "change anything under src/evomesh/ -- a candidate that does is discarded.",
            "- Do not invent a mock or stub class. If it needs a stand-in, reuse one "
            "an existing test already has (grep tests/ for it).",
            "- To append: edit, with `old` = the file's last line as shown, without "
            "the `NNNNN| ` prefix (number, bar, ONE space). Put any import you need "
            "with the others at the top. A file that does not exist yet: write it.",
            "- Do not run pytest: validation runs it once you stop. `shell` is a bare "
            "python with none of the project installed.",
            "- End with one line starting exactly with 'RATIONALE:'.",
        )
    )


def write_test_task(root: Path, objective: str, path: str, symbol: str, tests: str) -> str:
    """The whole harness task for one test of ``symbol``: its code, and where
    in the test file the new test goes."""
    excerpt = symbol_excerpt(root, path, symbol) or f"(`{symbol}` is not in {path} -- grep for it)"
    parts = [objective, f"CODE UNDER TEST -- {path}, `{symbol}`:\n{excerpt}"]
    if (root / tests).is_file():
        outline = module_outline(root, tests, budget=900) or ""
        parts.append(
            f"THE TEST FILE -- {tests}. Its imports:\n{_file_head(root, tests)}\n"
            f"Its tests (line| definition):\n{outline}\n"
            f"It ENDS WITH:\n{_file_tail(root, tests)}"
        )
    else:
        parts.append(f"{tests} does not exist yet: create it with write.")
    parts.append(_test_rules(tests))
    return "\n\n".join(parts)


# `tests/test_x.py:19`, `src/evomesh/x.py:107:5` -- anywhere in a ruff, pyright
# or pytest output, after backslashes are made forward (pyright and pytest
# print absolute Windows paths, with spaces in them).
_LOCATION = re.compile(r"((?:src|tests)/[\w./-]+?\.py):(\d+)")
FAILURE_WINDOW = 12


def failure_locations(root: Path, output: str) -> list[tuple[str, int]]:
    """``(path, line)`` for every location in ``output`` that is a real file
    under ``root``, first mention first."""
    found: list[tuple[str, int]] = []
    for match in _LOCATION.finditer(output.replace("\\", "/")):
        parts = match.group(1).split("/")
        relative = next(
            (
                "/".join(parts[index:])
                for index in range(len(parts))
                if parts[index] in ("src", "tests") and (root.joinpath(*parts[index:])).is_file()
            ),
            None,
        )
        location = (relative, int(match.group(2))) if relative else None
        if location is not None and location not in found:
            found.append(location)
    return found


def failure_excerpts(root: Path, output: str, budget: int = EXCERPT_CHARS) -> str:
    """The code around the (at most two) files a failing command points at,
    numbered like a read: what a repair would otherwise go looking for."""
    blocks: list[str] = []
    seen: set[str] = set()
    room = budget
    for path, line in failure_locations(root, output):
        if path in seen or len(seen) == 2:
            continue
        seen.add(path)
        lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(1, line - FAILURE_WINDOW), min(len(lines), line + FAILURE_WINDOW)
        header = f"CODE AT THE FAILURE -- {path}, around line {line}:"
        rows = _take(_numbered(lines, start, end), room - len(header))
        if not rows:
            break
        block = "\n".join((header, *rows))
        blocks.append(block)
        room -= len(block) + 2
    return "\n\n".join(blocks)


def named_code(root: Path, text: str, budget: int = EXCERPT_CHARS) -> str:
    """The first ``src/evomesh/<module>.py`` + backticked name pair in ``text``
    that is real code, as an excerpt -- where a reviewer said the work is."""
    names = [
        part
        for quoted in re.findall(r"`([^`]+)`", text)
        if re.fullmatch(r"[A-Za-z_][\w.]*", part := quoted.removesuffix("()"))
    ]
    for match in _SOURCE_PATH.finditer(text):
        for name in names:
            excerpt = symbol_excerpt(root, match.group(0), name, budget)
            if excerpt is not None:
                return f"CODE THE REVIEW NAMES -- {match.group(0)}, `{name}`:\n{excerpt}"
    return ""


REPAIR_RULES = "\n".join(
    (
        "Rules for this repair -- your working memory is small, so start from the "
        "code shown above:",
        "- Fix what the OUTPUT reports and nothing else. Read more only for a line "
        "you were not shown, with offset and limit, never a whole file.",
        "- Copy `old` character-for-character from what you were shown, without the "
        "`NNNNN| ` prefix (number, bar, ONE space).",
        "- A module nothing imports is fixed by importing and using it from a module "
        "that already runs, or by deleting the new file -- never by rewriting it.",
        "- Do not run ruff, pyright or pytest: validation runs them again once you "
        "stop. `shell` is a bare python with none of them installed.",
        "- End with one line starting exactly with 'RATIONALE:'.",
    )
)


# -- Plans and scouts answer; the pipeline writes ------------------------------
# A plan or a scout job is read-only and ends its answer with the steps (or the
# item); the pipeline writes them into the candidate's backlog and vets them
# there. Found 2026-09-24: when those jobs wrote improvements.md themselves,
# every one of the first four lost edits to copying an anchor out of a numbered
# read (a space too many, the wrong last line), and each was handed eight tools
# -- four of them for writing -- to produce what is, in the end, a few lines of
# text. Read-only, it is three tools and no anchor to get wrong.
_ANSWER_STEP = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\d+[.)]\s*)?(?:\[[ xX]\]\s*)?"
    r"`?(?P<path>src/evomesh/\w+\.py)`?\s+"
    r"`(?P<symbol>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)`"
    r"\s*(?:--|:|-|—|–)?\s*(?P<change>\S.*?)\s*$"
)
_ANSWER_ITEM = re.compile(r"^\s*(?:[-*]\s*)?\[ \]\s*(?P<title>\S.*?)\s*$")


def _answer_lines(answer: str) -> list[str]:
    return [
        line.rstrip()
        for line in answer.splitlines()
        if not line.strip().startswith("```")
        and not line.strip().upper().startswith("RATIONALE:")
    ]


def _answer_step(number: int, line: str) -> Step | None:
    match = _ANSWER_STEP.match(line)
    if match is None:
        return None
    return Step(number, match.group("path"), match.group("symbol"), match.group("change"))


def steps_from_answer(answer: str) -> list[Step]:
    """Every step line in a plan job's answer, numbered in the order given."""
    steps: list[Step] = []
    for line in _answer_lines(answer):
        if (step := _answer_step(len(steps) + 1, line)) is not None:
            steps.append(step)
    return steps


def item_from_answer(answer: str) -> Improvement | None:
    """The first ``[ ] title`` in a scout's answer, with the detail and steps
    under it, or ``None`` when it wrote none."""
    lines = _answer_lines(answer)
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if _ANSWER_STEP.match(line) is None and _ANSWER_ITEM.match(line)
        ),
        None,
    )
    if start is None:
        return None
    header = _ANSWER_ITEM.match(lines[start])
    assert header is not None
    detail: list[str] = []
    steps: list[Step] = []
    for line in lines[start + 1 :]:
        if (step := _answer_step(len(steps) + 1, line)) is not None:
            steps.append(step)
        elif _ANSWER_ITEM.match(line) or (steps and line.strip()):
            break  # the next item, or prose after the steps: not this item's
        elif line.strip():
            detail.append(line.strip())
    title = header.group("title").strip("*_ ")
    return Improvement(title, "\n".join(detail), tuple(steps))


def _step_line(step: Step) -> str:
    return f"    {step.number}. [ ] {step.path} `{step.symbol}` -- {step.change}"


def write_planned_steps(root: Path, title: str, steps: list[Step]) -> str | None:
    """Put ``steps`` under open item ``title`` in ``root``'s backlog; the
    lines written, or ``None`` when there is no such item."""
    path = root / IMPROVEMENTS_FILE
    if not steps or not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _OPEN_ITEM.match(line.rstrip("\r\n"))
        if match is None or match.group("title") != title:
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() and _continues(lines[end]):
            end += 1
        if not lines[end - 1].endswith("\n"):
            lines[end - 1] += "\n"
        written = "".join(f"{_step_line(step)}\n" for step in steps)
        lines.insert(end, written)
        path.write_text("".join(lines), encoding="utf-8")
        return written
    return None


def append_item(root: Path, item: Improvement) -> str:
    """Append ``item`` to the end of ``root``'s backlog; the lines written."""
    path = root / IMPROVEMENTS_FILE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    block = "\n".join(
        (
            f"- [ ] {item.title}",
            *(f"    {line}" for line in item.detail.splitlines() if line.strip()),
            *(_step_line(step) for step in item.steps),
        )
    )
    separator = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{separator}{block}\n", encoding="utf-8")
    return block
