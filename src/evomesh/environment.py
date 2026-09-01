from __future__ import annotations

from enum import StrEnum

from evomesh.agents import AgentRegistry, AgentRuntime, system_agent_definitions
from evomesh.config import Settings
from evomesh.contracts import AgentDefinition, FilesystemGrant, Message, SkillDefinition
from evomesh.messaging import MessageBus
from evomesh.models import ModelProvider, OllamaProvider, OpenAICompatibleProvider
from evomesh.permissions import FilesystemPolicy
from evomesh.skills import SkillRegistry
from evomesh.storage import SQLiteRepository


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

    def _build_providers(self) -> dict[str, ModelProvider]:
        result: dict[str, ModelProvider] = {}
        for name, config in self.settings.models.providers.items():
            if name == "ollama":
                result[name] = OllamaProvider(config.base_url, config.model)
            else:
                result[name] = OpenAICompatibleProvider(
                    config.base_url, config.model, config.api_key
                )
        return result

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
        definitions = stored or system_definitions
        known_ids: set[str] = set()
        for definition in definitions:
            if definition.id in system_models:
                definition.provider, definition.model_name = system_models[definition.id]
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
        provider = self.providers.get(default_name)
        if provider:
            self.provider_health = await provider.health()
        else:
            self.provider_health = (False, f"Provider '{default_name}' is not configured")
        if start_agent_loops:
            for definition in self.registry.all():
                if definition.id != "architect" and definition.provider in self.providers:
                    await self.start_agent(definition.id)
        self.health_state = HealthState.READY

    async def stop(self) -> None:
        for runtime in list(self.runtimes.values()):
            await runtime.stop()
        self.runtimes.clear()
        self.health_state = HealthState.STOPPED

    async def register_agent(self, definition: AgentDefinition) -> None:
        self.registry.register(definition)
        self.bus.register(definition.id)
        await self.repository.save_agent(definition)

    async def configure_agent_model(
        self, agent_id_or_name: str, provider_name: str, model_name: str
    ) -> AgentDefinition:
        if provider_name not in self.providers:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        definition = self.registry.get(agent_id_or_name)
        was_running = definition.id in self.runtimes
        if was_running:
            await self.stop_agent(definition.id)
        definition.provider = provider_name
        definition.model_name = model_name
        await self.repository.save_agent(definition)
        if was_running:
            await self.start_agent(definition.id)
        return definition

    async def available_models(self, provider_name: str) -> list[str]:
        provider = self.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Provider '{provider_name}' is not configured")
        return await provider.list_models()

    async def start_agent(self, agent_id: str) -> None:
        if agent_id in self.runtimes:
            return
        definition = self.registry.get(agent_id)
        provider = self.providers.get(definition.provider)
        if provider is None:
            raise RuntimeError(f"Provider '{definition.provider}' is unavailable")
        runtime = AgentRuntime(definition, provider, self.bus, self.repository)
        await runtime.start()
        self.runtimes[agent_id] = runtime

    async def stop_agent(self, agent_id: str) -> None:
        runtime = self.runtimes.pop(agent_id, None)
        if runtime:
            await runtime.stop()

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
        return {
            "environment": self.settings.environment_name,
            "generation": 1,
            "status": self.health_state,
            "agents": len(self.registry.all()),
            "provider": self.settings.models.default_provider,
            "provider_ready": self.provider_health[0],
            "provider_message": self.provider_health[1],
        }
