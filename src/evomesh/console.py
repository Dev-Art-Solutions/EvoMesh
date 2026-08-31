from __future__ import annotations

import asyncio
import shlex
import threading

from evomesh.architect import ArchitectInterview
from evomesh.channels import Output
from evomesh.contracts import FilesystemGrant, Message
from evomesh.environment import Environment
from evomesh.evolution import CandidateWorkspace

HELP = """Commands:
  /help                         Show this help
  /status                       Environment and provider health
  /agents                       List registered agents
  /skills                       List available skills
  /models [provider]            List models exposed by a provider
  /chat <agent-name>            Select an agent
  /model <agent> <model> [prov] Change one agent's provider/model
  /agent start|stop <agent>     Control an individual agent loop
  /grant <agent> <path> <mode>  Grant read or write access
  /revoke <agent> <path>        Revoke access
  /confirm                      Activate Architect candidate
  /cancel                       Discard Architect candidate
  /evolution status             Show generation metadata
  /exit                         Stop EvoMesh
"""


class ConsoleChannel:
    def __init__(self, environment: Environment, output: Output | None = None) -> None:
        self.environment = environment
        self.output = output or Output()
        self.selected_agent = "architect"
        self.architect = ArchitectInterview()
        self.running = True

    async def run(self) -> None:
        self._banner()
        inputs: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def read_input() -> None:
            while self.running:
                try:
                    text = input("evomesh> ")
                except (EOFError, KeyboardInterrupt):
                    text = "/exit"
                try:
                    loop.call_soon_threadsafe(inputs.put_nowait, text)
                except RuntimeError:
                    return
                if text.strip().lower() == "/exit":
                    return

        threading.Thread(target=read_input, name="evomesh-console-input", daemon=True).start()
        while self.running:
            text = await inputs.get()
            try:
                response = await self.route(text)
            except (KeyError, ValueError, RuntimeError) as exc:
                response = f"Error: {exc}"
            if response:
                self.output.write(response)

    async def route(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if not text.startswith("/"):
            if self.selected_agent == "architect":
                if not self.architect.answers:
                    return self.architect.begin(text)
                provider, model = self._default_model()
                return self.architect.answer(text, provider, model)
            agent = self.environment.registry.get(self.selected_agent)
            await self.environment.send_message(
                Message(sender_id="human", recipient_id=agent.id, content=text)
            )
            try:
                response = await self.environment.bus.receive("human", wait_seconds=300)
            except TimeoutError:
                return f"Timed out waiting for {agent.name}."
            return f"{agent.name}> {response.content}"
        parts = shlex.split(text)
        command = parts[0].lower()
        if command == "/help":
            return HELP
        if command == "/exit":
            self.running = False
            return "Stopping EvoMesh."
        if command == "/status":
            status = self.environment.status()
            return "\n".join(f"{key}: {value}" for key, value in status.items())
        if command == "/agents":
            return "\n".join(
                f"{agent.name} [{agent.type}] - {agent.status} - "
                f"{agent.provider}:{agent.model_name}"
                for agent in self.environment.registry.all()
            )
        if command == "/skills":
            return "\n".join(skill.name for skill in self.environment.skills.discover())
        if command == "/models":
            provider = parts[1] if len(parts) > 1 else self._default_model()[0]
            models = await self.environment.available_models(provider)
            return f"Models on {provider}:\n" + "\n".join(models)
        if command == "/chat" and len(parts) == 2:
            agent = self.environment.registry.get(parts[1])
            self.selected_agent = agent.id
            return f"Talking to {agent.name}."
        if command == "/confirm":
            definition = self.architect.confirm()
            await self.environment.register_agent(definition)
            if definition.provider in self.environment.providers:
                await self.environment.start_agent(definition.id)
            self.selected_agent = definition.id
            return (
                f"Agent '{definition.name}' activated with "
                f"{definition.provider}:{definition.model_name}, persisted, and selected."
            )
        if command == "/cancel":
            self.architect = ArchitectInterview()
            return "Candidate discarded."
        if command == "/model" and len(parts) in {3, 4}:
            agent_name, model = parts[1], parts[2]
            current = self.environment.registry.get(agent_name)
            provider = parts[3] if len(parts) == 4 else current.provider
            definition = await self.environment.configure_agent_model(
                agent_name, provider, model
            )
            return (
                f"Agent '{definition.name}' now uses "
                f"{definition.provider}:{definition.model_name}."
            )
        if command == "/agent" and len(parts) == 3:
            action, agent_name = parts[1].lower(), parts[2]
            definition = self.environment.registry.get(agent_name)
            if action == "start":
                await self.environment.start_agent(definition.id)
            elif action == "stop":
                await self.environment.stop_agent(definition.id)
            else:
                return "Agent action must be start or stop."
            return f"Agent '{definition.name}' {action}ed."
        if command == "/grant" and len(parts) >= 4:
            agent = self.environment.registry.get(parts[1])
            mode = parts[-1].lower()
            path = " ".join(parts[2:-1])
            if mode not in {"read", "write"}:
                return "Mode must be read or write."
            await self.environment.grant_access(
                FilesystemGrant(
                    agent_id=agent.id, path=path, read=True, write=mode == "write"
                )
            )
            normalized = self.environment.permissions.normalize(path)
            return f"Granted {mode} access to {normalized}."
        if command == "/revoke" and len(parts) >= 3:
            agent = self.environment.registry.get(parts[1])
            path = " ".join(parts[2:])
            await self.environment.revoke_access(agent.id, path)
            normalized = self.environment.permissions.normalize(path)
            return f"Revoked access to {normalized}."
        if parts[:2] == ["/evolution", "status"]:
            workspace = CandidateWorkspace(
                self.environment.settings.data_path.parent.parent,
                self.environment.settings.generation_path,
            )
            return str(workspace.supervisor.metadata())
        return "Unknown or incomplete command. Type /help."

    def _default_model(self) -> tuple[str, str]:
        provider = self.environment.settings.models.default_provider
        config = self.environment.settings.models.providers.get(provider)
        return provider, config.model if config else "local-model"

    def _banner(self) -> None:
        status = self.environment.status()
        provider_mark = "READY" if status["provider_ready"] else status["provider_message"]
        self.output.write(
            "\n".join(
                [
                    "EvoMesh",
                    f"Environment: {status['environment']}",
                    f"Generation: {status['generation']}",
                    f"Model provider: {status['provider']} ({provider_mark})",
                    f"Agents: {status['agents']}",
                    f"Status: {status['status']}",
                    "",
                    "Type /help for commands.",
                ]
            )
        )
