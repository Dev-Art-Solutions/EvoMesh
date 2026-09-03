from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from evomesh.agents import AgentRegistry, AgentRuntime, system_agent_definitions
from evomesh.bdi import ReflectiveBehavior
from evomesh.behaviors import default_behaviors
from evomesh.cognition import AgentBehavior, CycleOutcome
from evomesh.config import Settings
from evomesh.contracts import (
    AgentDefinition,
    AgentPhase,
    AgentRuntimeState,
    AgentStatus,
    FilesystemGrant,
    Message,
)
from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
from evomesh.harness import HarnessResult, build_runner
from evomesh.harness_queue import HarnessGateway, HarnessJob, HarnessQueue, HarnessWorker
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.memory import AgentMemory, WorldContext
from evomesh.messaging import MessageBus
from evomesh.models import ModelProvider, OllamaProvider, OpenAICompatibleProvider
from evomesh.permissions import FilesystemPolicy
from evomesh.skills import SkillRegistry
from evomesh.storage import SQLiteRepository

logger = logging.getLogger(__name__)


class HealthState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    STOPPED = "STOPPED"


class Environment:
    def __init__(
        self, settings: Settings, providers: dict[str, ModelProvider] | None = None
    ) -> None:
        self.settings = settings
        self.repository = SQLiteRepository(settings.data_path)
        self.registry = AgentRegistry()
        self.bus = MessageBus(self.repository)
        self.permissions = FilesystemPolicy(self.repository)
        self.skills = SkillRegistry(self.project_root)
        self.providers = providers or self._build_providers()
        self.runtimes: dict[str, AgentRuntime] = {}
        self.harness_queue = HarnessQueue(settings.harness.max_queue)
        self.harness_workers: list[HarnessWorker] = []
        # What each finished job actually wrote, keyed by job number. Held here
        # rather than re-read from the JSONL: the record of a generation must not
        # depend on a file a human may have moved.
        self.harness_sessions: dict[int, list[dict[str, Any]]] = {}
        self.harness = HarnessGateway(self.harness_queue, self.harness_sessions)
        self.health_state = HealthState.STOPPED
        self.provider_health: tuple[bool, str] = (False, "not checked")
        self.world = WorldContext(settings.workspace_path)
        self.budget = settings.runtime.budget()
        self.behaviors: dict[str, AgentBehavior] = default_behaviors(
            settings.evolution.auto_validate,
            settings.evolution.max_repairs,
            settings.evolution.auto_promote,
            settings.evolution.auto_restart,
            settings.evolution.validate_seconds,
        )
        self.evolver = EnvironmentEvolver(
            CandidateWorkspace(
                self.project_root,
                settings.generation_path,
                # Never copy live state into a candidate generation.
                exclude=(settings.data_path, settings.workspace_path),
            ),
            self.repository,
            identity=settings.git.identity(),
            publish=settings.git.publish_policy(),
        )
        self.evolver.on_generation_landed = self._on_generation_landed
        # Set when a generation has landed in the tree this process is not
        # running. Whoever owns the process -- __main__, a test, a script --
        # decides what to do about it; the environment only raises the flag.
        self.restart_requested = asyncio.Event()
        self.restart_reason = ""
        # Channels that want to hear what the mesh did on its own, rather than
        # only what it was asked. Telegram registers one; the console does not,
        # because it is already printing the cycle summaries.
        self.notifiers: list[Callable[[str], Awaitable[None]]] = []
        # Long-lived channels the process owns, registered by whoever started
        # them. Held as plain objects rather than imported types: the console
        # only has to ask them about themselves, and importing the Telegram
        # channel here would make the environment depend on something that
        # already depends on it.
        self.channels: dict[str, Any] = {}
        # Offline states for agents that never started, so status is always
        # explainable instead of a stale "active" left behind by a previous run.
        self._offline: dict[str, AgentRuntimeState] = {}

    @property
    def project_root(self) -> Path:
        return self.settings.generation_path.parent

    # -- restarting into a landed generation ----------------------------

    def _on_generation_landed(self, number: int, commit: str) -> None:
        """A generation is now in the tree, and this process is not running it.

        Restarting is the whole point of applying a generation: until the
        process comes back up on the new commit, the mesh has evolved on disk
        and nowhere else. The flag is always raised so ``/evolution status``
        stays truthful even when the automatic restart is switched off.
        """
        self.restart_reason = (
            f"generation {number} landed as {commit[:8]}"
            f"{f' and was {self.evolver.last_publish}' if self.evolver.last_publish else ''}"
        )
        if not self.settings.evolution.auto_restart:
            logger.info("%s; auto_restart is off, so a human has to restart", self.restart_reason)
            return
        logger.info("%s; restarting into it", self.restart_reason)
        self.restart_requested.set()

    async def announce(self, text: str) -> None:
        """Tell every listening channel something the mesh did unprompted."""
        for notify in list(self.notifiers):
            try:
                await notify(text)
            except Exception:  # noqa: BLE001 - a broken channel never stops the mesh
                logger.exception("A notification channel failed")

    def _build_providers(self) -> dict[str, ModelProvider]:
        result: dict[str, ModelProvider] = {}
        for name, config in self.settings.models.providers.items():
            if name == "ollama":
                result[name] = OllamaProvider(
                    config.base_url, config.model, config.timeout_seconds, config.num_ctx
                )
            else:
                result[name] = OpenAICompatibleProvider(
                    config.base_url, config.model, config.api_key, config.timeout_seconds
                )
        return result

    # -- lifecycle ------------------------------------------------------

    async def start(self, *, start_agent_loops: bool = False) -> None:
        self.health_state = HealthState.STARTING
        # This process is starting from whatever the tree holds right now, so a
        # restart owed by an earlier promotion has just been paid.
        self.evolver.workspace.supervisor.clear_restart_flag()
        await self.repository.initialize()
        await self.skills.load()
        stored = await self.repository.load_agents()
        default_name = self.settings.models.default_provider
        provider_config = self.settings.models.providers.get(default_name)
        model = provider_config.model if provider_config else "local-model"
        system_models = {
            agent_id: (configuration.provider, configuration.model, configuration.num_ctx)
            for agent_id, configuration in self.settings.system_agents.items()
        }
        system_definitions = system_agent_definitions(default_name, model, system_models)
        seeded = {item.id: item for item in system_definitions}
        definitions = stored or system_definitions
        known_ids: set[str] = set()
        for definition in definitions:
            if definition.id in system_models:
                definition.provider, definition.model_name, definition.num_ctx = system_models[
                    definition.id
                ]
            self._reconcile(definition, seeded.get(definition.id))
            if definition.id not in known_ids:
                self.registry.register(definition)
                self.bus.register(definition.id)
                await self.repository.save_agent(definition)
                known_ids.add(definition.id)
        for system_definition in system_definitions:
            if system_definition.id not in known_ids:
                self.registry.register(system_definition)
                self.bus.register(system_definition.id)
                await self.repository.save_agent(system_definition)
        self._apply_evolution_settings()
        provider = self.providers.get(default_name)
        if provider:
            self.provider_health = await provider.health()
        else:
            self.provider_health = (False, f"Provider '{default_name}' is not configured")
        self.evolver.provider = self.providers.get(default_name)
        self._start_harness_workers()
        if start_agent_loops:
            await self.start_all()
        await self.refresh_world()
        self.health_state = HealthState.READY

    def _reconcile(self, definition: AgentDefinition, seed: AgentDefinition | None) -> None:
        """Bring a persisted definition back to a state a boot can act on.

        A definition saved at shutdown carries status STOPPED, and older records
        predate goals entirely. Without this, system agents come back inert or,
        worse, come back labelled ACTIVE with no loop behind the label.
        """
        if seed is not None:
            definition.type = "system"
            definition.autonomy = seed.autonomy
            definition.purpose = definition.purpose or seed.purpose
            definition.identity = definition.identity or seed.identity
            if definition.status is not AgentStatus.ACTIVE:
                definition.status = AgentStatus.ACTIVE
            if not definition.mind.goals:
                for goal in seed.mind.goals:
                    definition.mind.goals.append(goal)
        elif definition.status is AgentStatus.STOPPED:
            # A human stopped it, or the last run ended. Either way it is not
            # running now, and start_all leaves it alone until asked.
            pass

    def _apply_evolution_settings(self) -> None:
        evolver = self.registry.get("evolver") if self._has("evolver") else None
        if evolver is None:
            return
        if not self.settings.evolution.autonomous:
            evolver.status = AgentStatus.STOPPED
        objective = self.settings.evolution.objective
        if objective and not any(
            goal.description == objective for goal in evolver.mind.goals
        ):
            evolver.mind.add_goal(objective, priority=2)

    def _has(self, agent_id: str) -> bool:
        try:
            self.registry.get(agent_id)
        except KeyError:
            return False
        return True

    async def start_all(self) -> None:
        """Start every agent that is supposed to be running, and explain the rest."""
        self._offline.clear()
        for index, definition in enumerate(self.registry.all()):
            if definition.status is not AgentStatus.ACTIVE:
                self._mark_offline(definition, f"status is {definition.status}")
                continue
            if definition.provider not in self.providers:
                self._mark_offline(
                    definition, f"provider '{definition.provider}' is not configured"
                )
                continue
            try:
                await self.start_agent(definition.id, start_delay=index * self._stagger)
            except (RuntimeError, ValueError) as exc:
                self._mark_offline(definition, str(exc))

    @property
    def _stagger(self) -> float:
        return max(0.0, self.settings.runtime.stagger_seconds)

    def _mark_offline(self, definition: AgentDefinition, reason: str) -> None:
        logger.info("Agent %s is not running: %s", definition.name, reason)
        self._offline[definition.id] = AgentRuntimeState(
            agent_id=definition.id,
            name=definition.name,
            phase=AgentPhase.OFFLINE,
            goal=(goal.description if (goal := definition.mind.next_goal()) else None),
            last_error=reason,
        )

    async def stop(self) -> None:
        # Shutting the mesh down leaves every agent's desired status untouched,
        # so the next boot starts exactly what was running before.
        await self._stop_harness_workers()
        await self.evolver.cancel_validation()
        for runtime in list(self.runtimes.values()):
            await runtime.stop(persist_status=False)
        self.runtimes.clear()
        self.health_state = HealthState.STOPPED

    # -- agents ---------------------------------------------------------

    async def register_agent(self, definition: AgentDefinition) -> None:
        self.registry.register(definition)
        self.bus.register(definition.id)
        await self.repository.save_agent(definition)

    def memory_for(self, definition: AgentDefinition) -> AgentMemory:
        return AgentMemory(self.settings.workspace_path, definition, self.budget)

    def cycle_seconds_for(self, definition: AgentDefinition) -> float:
        if definition.cycle_seconds:
            return float(definition.cycle_seconds)
        if definition.id == "evolver":
            return float(self.settings.evolution.cycle_seconds)
        return float(self.settings.runtime.cycle_seconds)

    def resolve_num_ctx(
        self, provider_name: str, model_name: str | None, override: int | None = None
    ) -> int | None:
        """The window a request against this provider/model should ask for.

        Checked in order: an explicit override (an agent's own setting) first,
        since it names the one agent that needs something different from its
        siblings; then the provider's per-model entry, for a provider serving
        more than one model with genuinely different needs; then the
        provider's own default. Absent all three, there is nowhere left to
        look and the provider's own default (unset) applies.
        """
        if override is not None:
            return override
        provider = self.settings.models.providers.get(provider_name)
        if provider is None:
            return None
        if model_name and model_name in provider.model_num_ctx:
            return provider.model_num_ctx[model_name]
        return provider.num_ctx

    def num_ctx_for(self, definition: AgentDefinition) -> int | None:
        return self.resolve_num_ctx(definition.provider, definition.model_name, definition.num_ctx)

    async def start_agent(self, agent_id: str, *, start_delay: float = 0.0) -> None:
        if agent_id in self.runtimes:
            return
        definition = self.registry.get(agent_id)
        provider = self.providers.get(definition.provider)
        if provider is None:
            raise RuntimeError(f"Provider '{definition.provider}' is unavailable")
        runtime = AgentRuntime(
            definition=definition,
            provider=provider,
            bus=self.bus,
            repository=self.repository,
            memory=self.memory_for(definition),
            behavior=self.behaviors.get(definition.id, ReflectiveBehavior()),
            budget=self.budget,
            cycle_seconds=self.cycle_seconds_for(definition),
            num_ctx=self.num_ctx_for(definition),
            start_delay=start_delay,
            services=self._services,
            world_context=self._world_snapshot,
        )
        await runtime.start()
        self.runtimes[agent_id] = runtime
        self._offline.pop(agent_id, None)

    async def stop_agent(self, agent_id: str, *, persist_status: bool = True) -> None:
        runtime = self.runtimes.pop(agent_id, None)
        if runtime:
            await runtime.stop(persist_status=persist_status)
            self._mark_offline(runtime.definition, "stopped by request")

    async def cycle_agent(self, agent_id_or_name: str) -> CycleOutcome:
        definition = self.registry.get(agent_id_or_name)
        runtime = self.runtimes.get(definition.id)
        if runtime is None:
            raise RuntimeError(f"Agent '{definition.name}' is not running")
        outcome = await runtime.run_cycle()
        await self.refresh_world()
        return outcome

    def runtime_states(self) -> dict[str, AgentRuntimeState]:
        states = {name: state for name, state in self._offline.items()}
        for agent_id, runtime in self.runtimes.items():
            states[agent_id] = runtime.state
        for definition in self.registry.all():
            states.setdefault(
                definition.id,
                AgentRuntimeState(agent_id=definition.id, name=definition.name),
            )
        # Derived, never stored: an agent with a job in flight is doing
        # something, and reporting it as `thinking` would be a label with no
        # loop behind it -- the exact failure the status/phase split exists to
        # prevent. An offline agent keeps its reason; a job cannot revive it.
        for job in self.harness_queue.open_jobs():
            state = states.get(job.agent_id)
            if state is not None and state.phase is not AgentPhase.OFFLINE:
                state.phase = AgentPhase.AWAITING_HARNESS
                state.last_outcome = job.describe()
        return states

    # -- the harness worker ---------------------------------------------

    def _start_harness_workers(self) -> None:
        """No harness configured, no worker task. Rule: off means absent."""
        if not self.settings.harness.enabled or self.harness_workers:
            return
        for index in range(max(1, self.settings.harness.workers)):
            worker = HarnessWorker(self.harness_queue, self._run_harness_job, self._deliver_harness)
            worker.start(f"evomesh-harness-{index + 1}")
            self.harness_workers.append(worker)

    async def _stop_harness_workers(self) -> None:
        for worker in self.harness_workers:
            await worker.stop()
        self.harness_workers.clear()

    def submit_harness_job(
        self, objective: str, *, agent_id: str = "", root: Path | None = None
    ) -> HarnessJob:
        """Queue a job and return its handle, which may be one already running."""
        if not self.settings.harness.enabled:
            raise RuntimeError("the harness is off; set harness.enabled in evomesh.yaml")
        return self.harness_queue.submit(
            objective,
            root or self.project_root,
            agent_id=agent_id,
            allow_write=self.settings.harness.allow_write,
        )

    async def _run_harness_job(self, job: HarnessJob) -> HarnessResult:
        settings = self.settings.harness
        provider_name = self.settings.models.default_provider
        model: str | None = None
        num_ctx_override: int | None = None
        if job.agent_id and self._has(job.agent_id):
            definition = self.registry.get(job.agent_id)
            provider_name, model = definition.provider, definition.model_name
            num_ctx_override = definition.num_ctx
        provider = self.providers.get(provider_name)
        if provider is None:
            raise RuntimeError(f"Provider '{provider_name}' is not configured")
        session = HarnessSession(next_session_path(settings.session_path))
        runner = build_runner(
            provider,
            job.root,
            session=session,
            limits=settings.limits(),
            model=model,
            max_steps=settings.max_steps,
            max_seconds=settings.max_seconds,
            transcript_chars=settings.transcript_chars,
            shell_allow=settings.shell_programs(),
            shell_seconds=settings.shell_seconds,
            read_only=not job.allow_write,
            allow_write=job.allow_write,
            num_ctx=self.resolve_num_ctx(provider_name, model, num_ctx_override),
            scraping_executable=(
                self.settings.scraping.executable if self.settings.scraping.enabled else ""
            ),
            scraping_timeout=self.settings.scraping.timeout_seconds,
        )
        # An agent's job runs under that agent's grants, so the harness is the
        # loudest user of the permission policy rather than a way around it.
        #
        # Which is why the environment has to grant the root it just handed out.
        # A candidate generation is a directory the mesh created *for this agent
        # to work in*, and without a grant every tool is denied -- the first real
        # generation through the harness spent four steps discovering that. The
        # grant is scoped to that one disposable copy, is visible in `/grant`
        # like any other, and dies with the directory.
        if job.agent_id:
            await self.permissions.grant(
                FilesystemGrant(
                    agent_id=job.agent_id,
                    path=str(job.root),
                    read=True,
                    write=job.allow_write,
                )
            )
            runner.context.policy = self.permissions
            runner.context.agent_id = job.agent_id
        # Every skill is a file under skills/, inside the job root the same as
        # any other -- this line is the only thing that makes one reachable at
        # all: naming it and where to read it, so the model decides whether to
        # spend a step on it, rather than the description being pinned to
        # every job whether it turns out relevant or not.
        catalog = self.skills.render_catalog()
        task = f"{catalog}\n\n{job.objective}" if catalog else job.objective
        try:
            return await runner.run(task)
        finally:
            # Kept even when the job failed: what it managed to change before
            # it broke is the part a human has to look at.
            self.harness_sessions[job.number] = list(session.entries)

    async def _deliver_harness(self, job: HarnessJob) -> None:
        """A finished job is an ordinary inbound message, not a callback.

        It lands in the mailbox, gets the audit record every message gets, and
        wakes the loop the agent already has -- so no behavior has to know that
        a worker exists.
        """
        if not job.agent_id:
            return
        if job.result is not None:
            said = job.result.answer or job.result.detail
            body = f"Harness job {job.number} {job.result.outcome}: {said}"
        else:
            body = f"Harness job {job.number} did not finish: {job.detail}"
        await self.send_message(
            Message(sender_id="harness", recipient_id=job.agent_id, content=body)
        )

    def _services(self) -> dict[str, object]:
        return {
            "evolver": self.evolver,
            "skills": self.skills,
            "permissions": self.permissions,
            "repository": self.repository,
            "runtime_states": self.runtime_states(),
            "provider_health": self.provider_health,
            "registry": self.registry,
            "harness": self.harness if self.settings.harness.enabled else None,
        }

    async def configure_agent_model(
        self, agent_id_or_name: str, provider_name: str, model_name: str
    ) -> AgentDefinition:
        if provider_name not in self.providers:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        definition = self.registry.get(agent_id_or_name)
        was_running = definition.id in self.runtimes
        if was_running:
            # A model swap is not a request to disable the agent.
            await self.stop_agent(definition.id, persist_status=False)
        definition.provider = provider_name
        definition.model_name = model_name
        definition.touch()
        await self.repository.save_agent(definition)
        if was_running:
            definition.status = AgentStatus.ACTIVE
            await self.start_agent(definition.id)
        return definition

    async def configure_agent_num_ctx(
        self, agent_id_or_name: str, num_ctx: int | None
    ) -> AgentDefinition:
        """Set or clear one agent's context-window override.

        None (not zero) means "stop overriding" -- the agent goes back to
        whatever its provider's own num_ctx resolves to, the same as it would
        for an agent that never had an override at all.
        """
        definition = self.registry.get(agent_id_or_name)
        was_running = definition.id in self.runtimes
        if was_running:
            await self.stop_agent(definition.id, persist_status=False)
        definition.num_ctx = num_ctx
        definition.touch()
        await self.repository.save_agent(definition)
        if was_running:
            definition.status = AgentStatus.ACTIVE
            await self.start_agent(definition.id)
        return definition

    async def available_models(self, provider_name: str) -> list[str]:
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        return await provider.list_models()

    # -- world ----------------------------------------------------------

    def _world_snapshot(self) -> str:
        lines = [
            f"Environment: {self.settings.environment_name}",
            f"Provider: {self.settings.models.default_provider} "
            f"({'ready' if self.provider_health[0] else self.provider_health[1]})",
        ]
        for state in self.runtime_states().values():
            goal = f" goal: {state.goal}" if state.goal else ""
            lines.append(f"- {state.name} [{state.phase}]{goal}")
        return "\n".join(lines)

    async def refresh_world(self) -> None:
        states = self.runtime_states()
        roster = "\n".join(
            f"- {state.name}: {state.phase}"
            + (f", goal: {state.goal}" if state.goal else "")
            + (f", last: {state.last_outcome}" if state.last_outcome else "")
            for state in states.values()
        )
        pipeline = await self.evolver.pipeline_state()
        await self.world.write(
            {
                "Environment": (
                    f"name: {self.settings.environment_name}\n"
                    f"generation: {self.evolver.workspace.supervisor.metadata()['active']}\n"
                    f"provider: {self.settings.models.default_provider} "
                    f"({'ready' if self.provider_health[0] else self.provider_health[1]})"
                ),
                "Agents": roster,
                "Evolution": f"stage: {pipeline.get('stage', 'plan')}",
            }
        )

    # -- passthroughs ---------------------------------------------------

    async def send_message(self, message: Message) -> None:
        await self.bus.send(message)

    async def grant_access(self, grant: FilesystemGrant) -> None:
        await self.permissions.grant(grant)

    async def revoke_access(self, agent_id: str, path: str) -> None:
        await self.permissions.revoke(agent_id, path)

    async def request_model_inference(
        self,
        prompt: str,
        *,
        provider_name: str | None = None,
        model_name: str | None = None,
        system: str = "",
    ) -> str:
        name = provider_name or self.settings.models.default_provider
        provider = self.providers.get(name)
        if provider is None:
            raise RuntimeError(f"Provider '{name}' is unavailable")
        num_ctx = self.resolve_num_ctx(name, model_name)
        return await provider.generate(prompt, system=system, model=model_name, num_ctx=num_ctx)

    def status(self) -> dict[str, object]:
        states = self.runtime_states()
        return {
            "environment": self.settings.environment_name,
            "generation": self.evolver.workspace.supervisor.metadata()["active"],
            "status": self.health_state,
            "agents": len(self.registry.all()),
            "running": len(self.runtimes),
            "cycles": sum(state.cycles for state in states.values()),
            "provider": self.settings.models.default_provider,
            "provider_ready": self.provider_health[0],
            "provider_message": self.provider_health[1],
        }
