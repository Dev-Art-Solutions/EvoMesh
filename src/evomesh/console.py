from __future__ import annotations

import asyncio
import shlex

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
  /chat <agent-name>            Select an agent
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
        while self.running:
            try:
                text = await asyncio.to_thread(input, "evomesh> ")
            except (EOFError, KeyboardInterrupt):
                text = "/exit"
            response = await self.route(text)
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
            return f"Message sent to {agent.name}."
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
                f"{agent.name} [{agent.type}] - {agent.status}"
                for agent in self.environment.registry.all()
            )
        if command == "/skills":
            return "\n".join(skill.name for skill in self.environment.skills.discover())
        if command == "/chat" and len(parts) == 2:
            agent = self.environment.registry.get(parts[1])
            self.selected_agent = agent.id
            return f"Talking to {agent.name}."
        if command == "/confirm":
            definition = self.architect.confirm()
            await self.environment.register_agent(definition)
            return f"Agent '{definition.name}' activated and persisted."
        if command == "/cancel":
            self.architect = ArchitectInterview()
            return "Candidate discarded."
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
