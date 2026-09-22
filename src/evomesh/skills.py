"""What a skill is, and how the mesh finds one.

A skill is a description an agent reads, never a capability the mesh executes
on its behalf. The earlier shape of this module -- a registry of Python
handlers keyed by name, "invoked" with arguments like a function call -- built
exactly the wrong thing: every one of those handlers was a tool wearing a
skill's name. Filesystem.Read/Write and Git.Status/Diff duplicated what the
harness's read/write/edit tools and its shell (with git allowed) already do
more honestly; Web.Fetch duplicated the harness's own fetch tool outright.
A skill should add nothing a tool call could not already do -- it adds the
*procedure*, in prose, for when to make which calls, the same shape a skill
has in Claude Code itself.

Skills live on disk, not in the database, for the reason memory.md and
context.md do (rule 18): a human reads or edits them directly. One skill is
one directory under skills/, a SKILL.md with YAML frontmatter (name,
description) over a Markdown body -- the frontmatter is what
SkillRegistry.discover() searches without opening the file, and the body is
what an agent reads, with the same `read` tool it reads any other file with,
once render_catalog() has told it the skill exists and where. Nothing here
ever runs the body; that is the whole point.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from evomesh.contracts import SkillDefinition, now_utc

logger = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
# The largest hand-written skill in this repo (news-impact-analysis) is
# ~6.3KB; nothing legitimate here is anywhere close to this. A skill is read
# with the same `read` tool as any other file, so a runaway one (a small
# model's learn_skill/patch_skill dumping a whole transcript instead of a
# procedure) does not fail loudly -- it just quietly eats a large slice of
# harness.tool_result_chars the next agent that reads it never gets back.
# 15KB mirrors the same ceiling Nous's hermes-agent-self-evolution puts on
# an evolved skill file, for the same reason: a hard, generous cap that
# only a genuinely broken write ever reaches.
MAX_SKILL_BYTES = 15_000


@dataclass
class PendingSkillWrite:
    """A learn_skill/patch_skill call staged for a human to review, when
    HarnessSettings.skill_write_approval is on -- see Environment.
    _make_learn_skill/_make_patch_skill. ``text`` is already the exact final
    SKILL.md content install() would write; approving is nothing more than
    handing it to install() unchanged, so what a human reviews is exactly
    what lands.

    In-memory only, like HarnessQueue's own jobs -- restarting the mesh
    loses whatever was awaiting review, the same tradeoff the harness queue
    already makes and says so for (README: "not durable").
    """

    number: int
    agent_id: str
    kind: str  # "learn" or "patch"
    name: str
    summary: str
    text: str
    created_at: datetime = field(default_factory=now_utc)


class MissingSkillError(LookupError):
    pass


class InvalidSkillError(ValueError):
    pass


# A skill's body is prose a future model reads the same way it reads any
# other file -- with no more scrutiny than that. Two ways that goes wrong:
# a secret ends up quoted inside a "helpful" procedure and gets echoed back
# the next time the skill is read, or the body itself carries text meant to
# steer whatever reads it later (a prompt-injection payload one skill's
# author staged for a future session to fall into). Small, named regexes
# rather than a classifier, on purpose -- see CLAUDE.md rule 16: this stays
# a few lines of stdlib `re`, not a dependency, and every finding names
# exactly what tripped it instead of a bare "rejected".
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "an OpenAI-shaped API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key block"),
    (
        re.compile(
            r"(?i)(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*"
            r"['\"][A-Za-z0-9_\-]{12,}['\"]"
        ),
        "a hardcoded credential",
    ),
)
_INJECTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)ignore (all |any )?(previous|prior|above|earlier) instructions"),
        "an instruction-override phrase",
    ),
    (
        re.compile(
            r"(?i)disregard (your |the )?(system prompt|previous instructions"
            r"|instructions above)"
        ),
        "an instruction-override phrase",
    ),
    (
        re.compile(r"(?i)you are now (DAN|in developer mode|an unrestricted)"),
        "a known jailbreak phrase",
    ),
)


def scan_skill_content(text: str) -> list[str]:
    """What, if anything, a skill's raw text (frontmatter and body alike)
    should never be allowed to reach disk carrying. Empty means clean.

    Called from every write path -- a human's own `/skill install`, and an
    agent's own `learn_skill`/`patch_skill` -- so neither source is trusted
    more than the other; only the content is judged.
    """
    findings = [
        f"looks like it contains {label}"
        for pattern, label in _SECRET_PATTERNS
        if pattern.search(text)
    ]
    findings += [
        f"contains {label}" for pattern, label in _INJECTION_PATTERNS if pattern.search(text)
    ]
    return findings


def parse_skill(path: Path, text: str, *, created_by: str = "system") -> SkillDefinition:
    """Split a SKILL.md into its frontmatter and the definition it describes.

    Frontmatter is YAML between the file's opening ``---`` lines -- the same
    shape every skill in Claude Code already uses, because name and
    description are the metadata the registry needs before anything decides
    to read the body at all.
    """
    if not text.startswith("---"):
        raise InvalidSkillError(f"{path}: missing YAML frontmatter (a leading '---' block)")
    end = text.find("\n---", 3)
    if end == -1:
        raise InvalidSkillError(f"{path}: frontmatter is opened but never closed with '---'")
    try:
        meta = yaml.safe_load(text[3:end].strip("\n")) or {}
    except yaml.YAMLError as exc:
        raise InvalidSkillError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        kind = type(meta).__name__
        raise InvalidSkillError(f"{path}: frontmatter must be a mapping, not a {kind}")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise InvalidSkillError(f"{path}: frontmatter needs both 'name' and 'description'")
    return SkillDefinition(name=name, description=description, path=path, created_by=created_by)


def skill_body(text: str) -> str:
    """The instructions, with the frontmatter stripped -- what an agent acts
    on once it has decided, from the catalog line, that this skill applies."""
    if not text.startswith("---"):
        return text.strip()
    end = text.find("\n---", 3)
    return text[end + 4 :].strip() if end != -1 else text.strip()


class SkillRegistry:
    """Discovers skills under ``root/skills/*/SKILL.md``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._skills: dict[str, SkillDefinition] = {}

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    async def load(self) -> None:
        """Rescan disk. A handful of small files -- cheap enough to redo
        after every /skill install rather than patch the registry in place."""
        self._skills = await asyncio.to_thread(self._scan)

    def _scan(self) -> dict[str, SkillDefinition]:
        found: dict[str, SkillDefinition] = {}
        if not self.skills_dir.is_dir():
            return found
        for entry in sorted(self.skills_dir.iterdir()):
            skill_file = entry / SKILL_FILENAME
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8")
                definition = parse_skill(skill_file.relative_to(self.root), text)
            except (InvalidSkillError, OSError, UnicodeDecodeError) as exc:
                logger.warning("skill at %s could not be loaded: %s", skill_file, exc)
                continue
            found[definition.name] = definition
        return found

    def discover(self, query: str = "") -> list[SkillDefinition]:
        needle = query.lower()
        return [
            skill
            for skill in self._skills.values()
            if needle in skill.name.lower() or needle in skill.description.lower()
        ]

    def get(self, name: str) -> SkillDefinition:
        skill = self._skills.get(name)
        if skill is None:
            raise MissingSkillError(name)
        return skill

    async def read(self, name: str) -> str:
        """The skill's instructions, frontmatter stripped -- for previewing
        one from the console, not for handing to an agent (render_catalog
        does that, by name and path, so the agent still does its own reading
        through the same tool it reads any other file with)."""
        skill = self.get(name)
        text = await asyncio.to_thread((self.root / skill.path).read_text, encoding="utf-8")
        return skill_body(text)

    def render_catalog(self) -> str:
        """One line per skill, sized for a prompt: name, description, and the
        path to read for the rest. Never the body -- spending that budget
        unconditionally would undo the reason a skill is a file an agent
        reads only when it decides to, rather than a paragraph pinned to
        every prompt whether it is relevant or not."""
        if not self._skills:
            return ""
        lines = [
            f"- {skill.name}: {skill.description} (read {skill.path.as_posix()} for how)"
            for skill in sorted(self._skills.values(), key=lambda item: item.name)
        ]
        return "Skills available -- read one only if it is relevant to this task:\n" + "\n".join(
            lines
        )

    async def install(self, source_text: str, *, created_by: str = "human") -> SkillDefinition:
        """Write a new skill from a SKILL.md's raw text -- a file already
        read, or a URL already fetched. This one step is the whole
        installation mechanism: whatever produced valid frontmatter and a
        body becomes a skill, and a market is just a place skills like this
        one get shared from.
        """
        definition = parse_skill(Path("<new skill>"), source_text, created_by=created_by)
        if findings := scan_skill_content(source_text):
            raise InvalidSkillError(f"{definition.name}: refused -- {'; '.join(findings)}")
        if (size := len(source_text.encode("utf-8"))) > MAX_SKILL_BYTES:
            raise InvalidSkillError(
                f"{definition.name}: refused -- {size} bytes exceeds the "
                f"{MAX_SKILL_BYTES}-byte limit for a skill file"
            )
        target = self.skills_dir / definition.name / SKILL_FILENAME
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, source_text, encoding="utf-8")
        installed = definition.model_copy(update={"path": target.relative_to(self.root)})
        self._skills[installed.name] = installed
        return installed

    async def install_directory(
        self, source: Path, *, created_by: str = "human"
    ) -> SkillDefinition:
        """Like install(), for a skill that is a group of commands rather
        than only a description: a directory with SKILL.md plus one or more
        bundled scripts, copied in as a unit so the file the description
        tells an agent to run ("run scripts/check.sh") actually exists next
        to it. The scripts stay inert either way -- an agent runs one with
        its own harness tools, on purpose, the same as reading any skill.
        """
        skill_file = source / SKILL_FILENAME
        if not await asyncio.to_thread(skill_file.is_file):
            raise InvalidSkillError(f"{source}: no {SKILL_FILENAME} in this directory")
        text = await asyncio.to_thread(skill_file.read_text, encoding="utf-8")
        definition = parse_skill(Path("<new skill>"), text, created_by=created_by)
        if findings := scan_skill_content(text):
            raise InvalidSkillError(f"{definition.name}: refused -- {'; '.join(findings)}")
        if (size := len(text.encode("utf-8"))) > MAX_SKILL_BYTES:
            raise InvalidSkillError(
                f"{definition.name}: refused -- {size} bytes exceeds the "
                f"{MAX_SKILL_BYTES}-byte limit for a skill file"
            )
        target = self.skills_dir / definition.name
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        await asyncio.to_thread(shutil.copytree, source, target)
        installed = definition.model_copy(
            update={"path": (target / SKILL_FILENAME).relative_to(self.root)}
        )
        self._skills[installed.name] = installed
        return installed
