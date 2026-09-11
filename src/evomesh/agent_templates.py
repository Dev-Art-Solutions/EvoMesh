"""An agent template: a whole agent, bundled with the skills and tools it
needs, installable and instantiable as one unit.

`architect.py` already turns a human's description into one running agent,
interactively, one at a time. A template is the other direction: a name and a
purpose someone already worked out (Trader, NewsWatcher, ...), written once as
an ``AGENT.md`` plus whatever ``skills/*`` and ``tools/*`` it bundles beside
it, installed with one call the same way `SkillRegistry`/`ToolRegistry`
already are, and turned into a live, running `AgentDefinition` with
`instantiate()` -- possibly more than once, with a different name or its own
Telegram token each time.

Skills and tools stay singletons, shared by every agent that names them; a
template's bundle is only how they get onto disk the first time. Installing
the same bundle twice is not an error -- the second install just overwrites
the first, same as SkillRegistry.install_directory / ToolRegistry.install_directory.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

from evomesh.contracts import (
    AgentDefinition,
    AgentStatus,
    Autonomy,
    FilesystemGrant,
    TelegramSettings,
)

if TYPE_CHECKING:
    from evomesh.environment import Environment

logger = logging.getLogger(__name__)

AGENT_FILENAME = "AGENT.md"


class MissingAgentTemplateError(LookupError):
    pass


class InvalidAgentTemplateError(ValueError):
    pass


class TemplateGoal(BaseModel):
    text: str
    priority: int = 5
    recurring: bool = False
    interval_seconds: int | None = None
    cron: str | None = None
    notify: bool = False


class AgentTemplateDefinition(BaseModel):
    name: str
    description: str
    identity: str = ""
    purpose: str
    provider: str = ""
    model: str = ""
    autonomy: Autonomy = Autonomy.CYCLIC
    cycle_seconds: int | None = None
    goals: list[TemplateGoal] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    # See watchers.py -- a command polled on its own interval, never on this
    # agent's cognition cycle. May use the placeholder {template_dir} for the
    # template's own installed directory, the same way a tool's command may
    # use {tool_dir}.
    watch_command: str = ""
    watch_interval_seconds: float | None = None
    path: Path
    created_by: str = "system"


def parse_agent_template(
    path: Path, text: str, *, created_by: str = "system"
) -> AgentTemplateDefinition:
    """Split an AGENT.md into its frontmatter and the template it describes."""
    if not text.startswith("---"):
        raise InvalidAgentTemplateError(f"{path}: missing YAML frontmatter (a leading '---' block)")
    end = text.find("\n---", 3)
    if end == -1:
        raise InvalidAgentTemplateError(f"{path}: frontmatter is opened but never closed with '---'")
    try:
        meta = yaml.safe_load(text[3:end].strip("\n")) or {}
    except yaml.YAMLError as exc:
        raise InvalidAgentTemplateError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        kind = type(meta).__name__
        raise InvalidAgentTemplateError(f"{path}: frontmatter must be a mapping, not a {kind}")
    name = str(meta.get("name") or "").strip()
    purpose = str(meta.get("purpose") or "").strip()
    if not name or not purpose:
        raise InvalidAgentTemplateError(f"{path}: frontmatter needs 'name' and 'purpose'")
    description = str(meta.get("description") or purpose).strip()
    raw_goals = meta.get("goals") or []
    if not isinstance(raw_goals, list):
        raise InvalidAgentTemplateError(f"{path}: 'goals' must be a list")
    try:
        goals = [
            TemplateGoal.model_validate(item) if isinstance(item, dict) else TemplateGoal(text=str(item))
            for item in raw_goals
        ]
        autonomy = Autonomy(str(meta.get("autonomy") or "cyclic").strip().lower())
        skills = [str(item).strip() for item in (meta.get("skills") or []) if str(item).strip()]
        tools = [str(item).strip() for item in (meta.get("tools") or []) if str(item).strip()]
        cycle_seconds = meta.get("cycle_seconds")
        watch = meta.get("watch") or {}
        if not isinstance(watch, dict):
            raise InvalidAgentTemplateError(f"{path}: 'watch' must be a mapping")
        return AgentTemplateDefinition(
            name=name,
            description=description,
            identity=str(meta.get("identity") or name).strip(),
            purpose=purpose,
            provider=str(meta.get("provider") or "").strip(),
            model=str(meta.get("model") or "").strip(),
            autonomy=autonomy,
            cycle_seconds=int(cycle_seconds) if cycle_seconds is not None else None,
            goals=goals,
            skills=skills,
            tools=tools,
            watch_command=str(watch.get("command") or "").strip(),
            watch_interval_seconds=(
                float(watch["interval_seconds"]) if watch.get("interval_seconds") is not None else None
            ),
            path=path,
            created_by=created_by,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidAgentTemplateError(f"{path}: invalid template: {exc}") from exc


class AgentTemplateRegistry:
    """Discovers agent templates under ``root/agent-templates/*/AGENT.md``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._templates: dict[str, AgentTemplateDefinition] = {}

    @property
    def templates_dir(self) -> Path:
        return self.root / "agent-templates"

    async def load(self) -> None:
        self._templates = await asyncio.to_thread(self._scan)

    def _scan(self) -> dict[str, AgentTemplateDefinition]:
        found: dict[str, AgentTemplateDefinition] = {}
        if not self.templates_dir.is_dir():
            return found
        for entry in sorted(self.templates_dir.iterdir()):
            agent_file = entry / AGENT_FILENAME
            if not agent_file.is_file():
                continue
            try:
                text = agent_file.read_text(encoding="utf-8")
                definition = parse_agent_template(agent_file.relative_to(self.root), text)
            except (InvalidAgentTemplateError, OSError, UnicodeDecodeError) as exc:
                logger.warning("agent template at %s could not be loaded: %s", agent_file, exc)
                continue
            found[definition.name] = definition
        return found

    def discover(self, query: str = "") -> list[AgentTemplateDefinition]:
        needle = query.lower()
        return [
            template
            for template in self._templates.values()
            if needle in template.name.lower() or needle in template.description.lower()
        ]

    def get(self, name: str) -> AgentTemplateDefinition:
        template = self._templates.get(name)
        if template is None:
            raise MissingAgentTemplateError(name)
        return template

    async def install_directory(
        self, source: Path, *, created_by: str = "human"
    ) -> AgentTemplateDefinition:
        """Copy a template directory (AGENT.md, plus bundled skills/*, tools/*)
        into agent-templates/<name>/, same one-step mechanism as
        SkillRegistry.install_directory / ToolRegistry.install_directory."""
        agent_file = source / AGENT_FILENAME
        if not await asyncio.to_thread(agent_file.is_file):
            raise InvalidAgentTemplateError(f"{source}: no {AGENT_FILENAME} in this directory")
        text = await asyncio.to_thread(agent_file.read_text, encoding="utf-8")
        definition = parse_agent_template(Path("<new template>"), text, created_by=created_by)
        target = self.templates_dir / definition.name
        await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        await asyncio.to_thread(shutil.copytree, source, target)
        installed = definition.model_copy(update={"path": (target / AGENT_FILENAME).relative_to(self.root)})
        self._templates[installed.name] = installed
        return installed

    async def instantiate(
        self,
        environment: Environment,
        template_name: str,
        *,
        agent_name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        telegram_token: str = "",
    ) -> AgentDefinition:
        """Install the template's bundled skills/tools (if bundled beside it,
        rather than already installed) and bring up a live, running agent.

        Callable more than once per template: a template is a recipe, not a
        singleton, so a second Trader with a different name and its own
        Telegram bot is exactly `instantiate()` called again.
        """
        template = self.get(template_name)
        bundle_root = self.root / template.path.parent
        for skill_name in template.skills:
            bundled = bundle_root / "skills" / skill_name
            if await asyncio.to_thread(bundled.is_dir):
                await environment.skills.install_directory(bundled, created_by="agent-template")
        for tool_name in template.tools:
            bundled = bundle_root / "tools" / tool_name
            if await asyncio.to_thread(bundled.is_dir):
                await environment.tools.install_directory(bundled, created_by="agent-template")

        default_provider = provider or template.provider or environment.settings.models.default_provider
        provider_config = environment.settings.models.providers.get(default_provider)
        default_model = model or template.model or (provider_config.model if provider_config else "local-model")

        definition = AgentDefinition(
            name=agent_name or template.identity,
            type="agent",
            created_by=f"template:{template.name}",
            identity=template.identity,
            purpose=template.purpose,
            provider=default_provider,
            model_name=default_model,
            autonomy=template.autonomy,
            cycle_seconds=template.cycle_seconds,
            skills=list(template.skills),
            status=AgentStatus.ACTIVE,
            watch_command=template.watch_command.replace(
                "{template_dir}", str(bundle_root.resolve(strict=False))
            ),
            watch_interval_seconds=template.watch_interval_seconds,
        )
        for goal in template.goals:
            definition.mind.add_goal(
                goal.text,
                priority=goal.priority,
                recurring=goal.recurring,
                interval_seconds=goal.interval_seconds,
                cron_expression=goal.cron,
                notify=goal.notify,
            )
        if telegram_token.strip():
            definition.telegram = TelegramSettings(enabled=True, token=telegram_token.strip())

        await environment.register_agent(definition)
        if template.tools:
            # A custom tool only reaches a model through the harness, so a
            # template naming tools is asking for harness access to use them
            # -- the same grant `/harness grant <agent>` makes by hand.
            root = environment.default_harness_root(definition)
            definition.harness_root = str(root)
            await environment.repository.save_agent(definition)
            await environment.grant_access(
                FilesystemGrant(
                    agent_id=definition.id,
                    path=str(root),
                    read=True,
                    write=environment.settings.harness.allow_write,
                )
            )
        if definition.provider in environment.providers:
            await environment.start_agent(definition.id)
        return definition
