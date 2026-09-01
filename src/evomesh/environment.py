from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

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
    SkillDefinition,
)
from evomesh.evolution import CandidateWorkspace, EnvironmentEvolver
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
        self.skills = SkillRegistry(self.repository, self.permissions)
        self.providers = providers or self._build_providers()
        self.runtimes: dict[str, AgentRuntime] = {}
        self.health_state = HealthState.STOPPED
        self.provider_health: tuple[bool, str] = (False, "not checked")
        self.world = WorldContext(settings.workspace_path)
        self.budget = settings.runtime.budget()
        self.behaviors: dict[str, AgentBehavior] = default_behaviors(
            settings.evolution.auto_validate,
            settings.evolution.max_repairs,
            settings.evolution.auto_promote,
        )
        self.evolver = EnvironmentEvolver(
            CandidateWorkspace(
                self.project_root,
                settings.generation_path,
                # Never copy live state into a candidate generation.
                exclude=(settings.data_path, settings.workspace_path),
            ),
            self.repository,
        )
        # Offline states for agents that never started, so status is always
        # explainable instead of a stale "active" left behind by a previous run.
        self._offline: dict[str, AgentRuntimeState] = {}

    @property
    def project_root(self) -> Path:
        return self.settings.generation_path.parent

    def _build_providers(self) -> dict[str, ModelProvider]:
        result: dict[str, ModelProvider] = {}
        for name, config in self.settings.models.providers.items():
            if name == "ollama":
                result[name] = OllamaProvider(
                    config.base_url, config.model, config.timeout_seconds
                )
            else:
                result[name] = OpenAICompatibleProvider(
                    config.base_url, config.model, config.api_key, config.timeout_seconds
                )
        return result

    # -- lifecycle ------------------------------------------------------

    async def start(self, *, start_agent_loops: bool = False) -> None:
        self.health_state = HealthState.STARTING
        await self.repository.initialize()
        await self.skills.load()
        await self.skills.register_builtins()
        stored = await self.repository.load_agents()
        default_name = self.settings.models.default_provider
        provider_config = self.settings.models.providers.get(default_name)
        model = provider_config.model if provider_config else "local-model"
        system_models = {
            agent_id: (configuration.provider, configuration.model)
            for agent_id, configuration in self.settings.system_agents.items()
        }
        system_definitions = system_agent_definitions(default_name, model, system_models)
        seeded = {item.id: item for item in system_definitions}
        definitions = stored or system_definitions
        known_ids: set[str] = set()
        for definition in definitions:
            if definition.id in system_models:
                definition.provider, definition.model_name = system_models[definition.id]
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
        return states

    def _services(self) -> dict[str, object]:
        return {
            "evolver": self.evolver,
            "skills": self.skills,
            "permissions": self.permissions,
            "repository": self.repository,
            "runtime_states": self.runtime_states(),
            "provider_health": self.provider_health,
            "registry": self.registry,
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

    async def register_skill(self, definition: SkillDefinition, handler: object) -> None:
        await self.skills.register(definition, handler)  # type: ignore[arg-type]

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
        return await provider.generate(prompt, system=system, model=model_name)

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
