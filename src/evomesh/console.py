from __future__ import annotations

import asyncio
import json
import shlex
import threading
from pathlib import Path

from evomesh.architect import ArchitectInterview
from evomesh.channels import Output
from evomesh.contracts import AgentStatus, FilesystemGrant, GoalStatus, Message
from evomesh.environment import Environment
from evomesh.harness import build_runner
from evomesh.harness_session import HarnessSession, next_session_path
from evomesh.harness_tools import ToolLimits
from evomesh.models import describe

HELP = """Commands:
  /help                         Show this help
  /status                       Environment and provider health
  /agents                       Agents with desired status and live phase
  /skills                       List available skills
  /models [provider]            List models exposed by a provider
  /chat <agent-name>            Select an agent
  /model <agent> <model> [prov] Change one agent's provider/model
  /num-ctx <agent> <n>|clear    Override (or clear) one agent's context window
  /agent start|stop <agent>     Control an individual agent loop
  /cycle <agent>                Run one deliberation cycle now
  /beliefs <agent>              What the agent currently holds true
  /goals <agent>                Its goals (desires), by priority
  /intentions <agent>           What it committed to, and the plan it is running
  /goal add <agent> "<text>" [priority]
  /goal done|drop <agent> <goal-id>
  /memory <agent>               Show the agent's memory.md
  /context <agent>|world        Show context.md
  /grant <agent> <path> <mode>  Grant read or write access
  /revoke <agent> <path>        Revoke access
  /confirm                      Activate the Architect draft
  /cancel                       Discard the Architect draft
  /evolution status             Show the generation and pipeline state
  /evolution start <objective>  Give the Evolver a new objective
  /evolution promote|discard [n]
  /evolution rollback           Return to the last known good generation
  /harness ask "<question>"     Let the model read the project before answering
  /harness do "<objective>" [path]  Let it change files, if harness.allow_write
  /harness status               Queue, workers, and what the last jobs did
  /harness grant <agent> [path] Let an agent use the harness in a directory
  /harness revoke <agent>       Take it away
  /telegram status              Whether the bot is connected, and who may use it
  /telegram test                Ask Telegram whether the configured token works
  /telegram allow|revoke <id>   Manage which chats may talk to the mesh
  /restart                      Restart the mesh into the code now in the tree
  /exit                         Stop EvoMesh
"""


def _directory(raw: str) -> Path | None:
    """The job root a human typed, or None if it is not a directory.

    Sync on purpose: touching the filesystem from inside the async command
    handler is what ASYNC240 objects to, and the check is one stat call.
    """
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


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
            except Exception as exc:  # noqa: BLE001 - a failed command never ends the console
                response = f"Error: {describe(exc)}"
            if response:
                self.output.write(response)

    async def route(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if not text.startswith("/"):
            return await self._talk(text)
        parts = shlex.split(text)
        command = parts[0].lower()
        handler = getattr(self, f"_command_{command[1:].replace('-', '_')}", None)
        if handler is None:
            return "Unknown command. Type /help."
        result = handler(parts)
        return await result if asyncio.iscoroutine(result) else str(result)

    # -- conversation ---------------------------------------------------

    async def _talk(self, text: str) -> str:
        if self.selected_agent == "architect":
            if not self.architect.answers:
                provider, model = self._default_model()
                return await self.architect.draft(text, provider, model, self._infer)
            return self.architect.refine(text)
        agent = self.environment.registry.get(self.selected_agent)
        await self.environment.send_message(
            Message(sender_id="human", recipient_id=agent.id, content=text)
        )
        try:
            response = await self.environment.bus.receive("human", wait_seconds=300)
        except TimeoutError:
            return f"Timed out waiting for {agent.name}."
        return f"{agent.name}> {response.content}"

    async def _infer(self, prompt: str, system: str) -> str:
        if not self.environment.provider_health[0]:
            raise RuntimeError("provider not ready")
        return await self.environment.request_model_inference(prompt, system=system)

    # -- commands -------------------------------------------------------

    def _command_help(self, parts: list[str]) -> str:
        return HELP

    def _command_exit(self, parts: list[str]) -> str:
        self.running = False
        return "Stopping EvoMesh."

    def _command_status(self, parts: list[str]) -> str:
        status = self.environment.status()
        return "\n".join(f"{key}: {value}" for key, value in status.items())

    def _command_agents(self, parts: list[str]) -> str:
        states = self.environment.runtime_states()
        rows: list[str] = []
        for agent in self.environment.registry.all():
            state = states[agent.id]
            detail = state.last_error or state.last_outcome
            rows.append(
                f"{agent.name} [{agent.type}] {agent.status}/{state.phase} "
                f"{agent.provider}:{agent.model_name} cycles={state.cycles}\n"
                f"    goal: {state.goal or 'none'}\n"
                f"    last: {detail or 'nothing yet'}"
            )
        return "\n".join(rows)

    def _command_skills(self, parts: list[str]) -> str:
        return "\n".join(skill.name for skill in self.environment.skills.discover())

    async def _command_models(self, parts: list[str]) -> str:
        provider = parts[1] if len(parts) > 1 else self._default_model()[0]
        models = await self.environment.available_models(provider)
        return f"Models on {provider}:\n" + "\n".join(models)

    def _command_chat(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /chat <agent>"
        agent = self.environment.registry.get(parts[1])
        self.selected_agent = agent.id
        return f"Talking to {agent.name}."

    async def _command_confirm(self, parts: list[str]) -> str:
        definition = self.architect.confirm()
        await self.environment.register_agent(definition)
        started = ""
        if definition.provider in self.environment.providers:
            await self.environment.start_agent(definition.id)
            started = " Its cycle loop is running."
        self.selected_agent = definition.id
        goal = definition.mind.goals[0].description if definition.mind.goals else "none"
        return (
            f"Agent '{definition.name}' activated with "
            f"{definition.provider}:{definition.model_name}.{started}\n"
            f"First goal: {goal}"
        )

    def _command_cancel(self, parts: list[str]) -> str:
        self.architect = ArchitectInterview()
        return "Draft discarded."

    async def _command_model(self, parts: list[str]) -> str:
        if len(parts) not in {3, 4}:
            return "Usage: /model <agent> <model> [provider]"
        agent_name, model = parts[1], parts[2]
        current = self.environment.registry.get(agent_name)
        provider = parts[3] if len(parts) == 4 else current.provider
        definition = await self.environment.configure_agent_model(agent_name, provider, model)
        return (
            f"Agent '{definition.name}' now uses "
            f"{definition.provider}:{definition.model_name}."
        )

    async def _command_num_ctx(self, parts: list[str]) -> str:
        if len(parts) != 3:
            return "Usage: /num-ctx <agent> <n>|clear"
        agent_name, value = parts[1], parts[2]
        if value.lower() == "clear":
            definition = await self.environment.configure_agent_num_ctx(agent_name, None)
            resolved = self.environment.num_ctx_for(definition)
            return (
                f"Agent '{definition.name}' no longer overrides its context window "
                f"(now {resolved or 'unset'}, from {definition.provider}'s own setting)."
            )
        if not value.isdigit() or int(value) <= 0:
            return "num_ctx must be a positive integer, or 'clear'."
        definition = await self.environment.configure_agent_num_ctx(agent_name, int(value))
        return f"Agent '{definition.name}' now uses a context window of {definition.num_ctx}."

    async def _command_agent(self, parts: list[str]) -> str:
        if len(parts) != 3:
            return "Usage: /agent start|stop <agent>"
        action, agent_name = parts[1].lower(), parts[2]
        definition = self.environment.registry.get(agent_name)
        if action == "start":
            definition.status = AgentStatus.ACTIVE
            await self.environment.start_agent(definition.id)
        elif action == "stop":
            await self.environment.stop_agent(definition.id)
        else:
            return "Agent action must be start or stop."
        return f"Agent '{definition.name}' {action}ed."

    async def _command_cycle(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /cycle <agent>"
        outcome = await self.environment.cycle_agent(parts[1])
        lines = [f"cycle: {outcome.summary}"]
        if outcome.step:
            lines.append(f"step: {outcome.step}")
        if outcome.fact:
            lines.append(f"remembered: {outcome.fact}")
        return "\n".join(lines)

    def _command_goals(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /goals <agent>"
        definition = self.environment.registry.get(parts[1])
        if not definition.mind.goals:
            return f"{definition.name} has no goals."
        rows = []
        for goal in definition.mind.goals:
            flag = " (recurring)" if goal.recurring else ""
            rows.append(
                f"{goal.id} [{goal.status}] p{goal.priority}{flag} {goal.description}"
                + (f"\n    last: {goal.notes[-1]}" if goal.notes else "")
            )
        return "\n".join(rows)

    async def _command_goal(self, parts: list[str]) -> str:
        if len(parts) < 4:
            return 'Usage: /goal add <agent> "<text>" [priority] | /goal done|drop <agent> <id>'
        action, agent_name = parts[1].lower(), parts[2]
        definition = self.environment.registry.get(agent_name)
        if action == "add":
            priority = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 5
            goal = definition.mind.add_goal(parts[3], priority=priority)
            message = f"Added goal {goal.id} to {definition.name}."
        elif action in {"done", "drop"}:
            goal = definition.mind.goal(parts[3])
            goal.status = GoalStatus.DONE if action == "done" else GoalStatus.FAILED
            goal.recurring = False
            message = f"Goal {goal.id} marked {goal.status}."
        else:
            return "Goal action must be add, done, or drop."
        definition.touch()
        await self.environment.repository.save_agent(definition)
        return message

    def _command_beliefs(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /beliefs <agent>"
        definition = self.environment.registry.get(parts[1])
        beliefs = sorted(definition.mind.beliefs, key=lambda item: item.updated_at)
        if not beliefs:
            return f"{definition.name} holds no beliefs yet."
        return "\n".join(
            f"{item.key}: {item.statement}"
            + (f"  ({item.source})" if item.source != "self" else "")
            for item in beliefs
        )

    def _command_intentions(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /intentions <agent>"
        definition = self.environment.registry.get(parts[1])
        if not definition.mind.intentions:
            return f"{definition.name} has committed to nothing yet."
        rows: list[str] = []
        for item in definition.mind.intentions[-4:]:
            try:
                goal = definition.mind.goal(item.goal_id).description
            except KeyError:
                goal = "(goal no longer exists)"
            rows.append(
                f"[{item.status}] plan '{item.plan}' for: {goal}\n"
                + "\n".join(f"    {step.render()}" for step in item.steps)
            )
        return "\n".join(rows)

    async def _command_memory(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /memory <agent>"
        definition = self.environment.registry.get(parts[1])
        memory = self.environment.memory_for(definition)
        await memory.ensure()
        return f"{memory.memory_path}\n\n{await memory.read_memory(4000)}"

    async def _command_context(self, parts: list[str]) -> str:
        if len(parts) != 2:
            return "Usage: /context <agent>|world"
        if parts[1].lower() == "world":
            await self.environment.refresh_world()
            return f"{self.environment.world.path}\n\n{await self.environment.world.read(4000)}"
        definition = self.environment.registry.get(parts[1])
        memory = self.environment.memory_for(definition)
        await memory.ensure()
        return f"{memory.context_path}\n\n{await memory.read_context(4000)}"

    async def _command_grant(self, parts: list[str]) -> str:
        if len(parts) < 4:
            return "Usage: /grant <agent> <path> read|write"
        agent = self.environment.registry.get(parts[1])
        mode = parts[-1].lower()
        path = " ".join(parts[2:-1])
        if mode not in {"read", "write"}:
            return "Mode must be read or write."
        await self.environment.grant_access(
            FilesystemGrant(agent_id=agent.id, path=path, read=True, write=mode == "write")
        )
        return f"Granted {mode} access to {self.environment.permissions.normalize(path)}."

    async def _command_revoke(self, parts: list[str]) -> str:
        if len(parts) < 3:
            return "Usage: /revoke <agent> <path>"
        agent = self.environment.registry.get(parts[1])
        path = " ".join(parts[2:])
        await self.environment.revoke_access(agent.id, path)
        return f"Revoked access to {self.environment.permissions.normalize(path)}."

    async def _command_harness(self, parts: list[str]) -> str:
        settings = self.environment.settings.harness
        if not settings.enabled:
            return (
                "The harness is off. Set harness.enabled: true in evomesh.yaml "
                "and restart the mesh."
            )
        action = parts[1].lower() if len(parts) > 1 else ""
        if action == "status":
            queue = self.environment.harness_queue
            workers = len(self.environment.harness_workers)
            rows = [job.describe() for job in queue.recent()]
            return (
                f"workers: {workers}, open jobs: {len(queue.open_jobs())}\n"
                + ("\n".join(f"  {row}" for row in rows) if rows else "  no jobs yet")
                + "\nThe queue is not durable: stopping the mesh cancels what is in it."
            )
        if action in ("grant", "revoke") and len(parts) > 2:
            agent = self.environment.registry.get(parts[2])
            if action == "revoke":
                agent.harness_root = ""
                await self.environment.repository.save_agent(agent)
                return f"{agent.name} may no longer submit harness jobs."
            root = _directory(parts[3]) if len(parts) > 3 else self.environment.project_root
            if root is None:
                return f"{parts[3]} is not a directory."
            agent.harness_root = str(root)
            await self.environment.repository.save_agent(agent)
            await self.environment.grant_access(
                FilesystemGrant(
                    agent_id=agent.id,
                    path=str(root),
                    read=True,
                    write=settings.allow_write,
                )
            )
            return f"{agent.name} may now submit harness jobs in {root}."
        if action == "job" and len(parts) > 2 and parts[2].isdigit():
            job = self.environment.harness_queue.jobs.get(int(parts[2]))
            return job.describe() if job else f"There is no job {parts[2]}."
        if action not in ("ask", "do") or len(parts) < 3:
            return (
                'Usage: /harness ask "<question>"  |  /harness do "<objective>" [path]'
                "  |  /harness status  |  /harness job <n>"
            )
        if action == "do" and not settings.allow_write:
            return (
                "The harness may not change files. Set harness.allow_write: true "
                "in evomesh.yaml and restart the mesh."
            )
        provider_name = self.environment.settings.models.default_provider
        provider = self.environment.providers.get(provider_name)
        if provider is None:
            return f"Provider '{provider_name}' is not configured."
        provider_settings = self.environment.settings.models.providers.get(provider_name)
        model_name = provider_settings.model if provider_settings else None
        num_ctx = self.environment.resolve_num_ctx(provider_name, model_name)
        writing = action == "do"
        root = self.environment.project_root
        task = parts[2]
        if writing and len(parts) > 3:
            chosen = _directory(parts[3])
            if chosen is None:
                return f"{parts[3]} is not a directory."
            root = chosen
        elif not writing:
            task = " ".join(parts[2:])
        runner = build_runner(
            provider,
            root,
            session=HarnessSession(next_session_path(settings.session_path)),
            limits=ToolLimits(
                result_chars=settings.tool_result_chars,
                result_lines=settings.tool_result_lines,
                grep_matches=settings.grep_matches,
            ),
            max_steps=settings.max_steps,
            max_seconds=settings.max_seconds,
            transcript_chars=settings.transcript_chars,
            shell_allow=settings.shell_programs(),
            shell_seconds=settings.shell_seconds,
            read_only=not writing,
            allow_write=writing,
            num_ctx=num_ctx,
            scraping_executable=(
                self.environment.settings.scraping.executable
                if self.environment.settings.scraping.enabled
                else ""
            ),
            scraping_timeout=self.environment.settings.scraping.timeout_seconds,
        )
        result = await runner.run(task)
        # Every tool call is printed, not just the answer: the point of the
        # harness phases is watching what a small model actually does with the
        # tools, and a summary line hides exactly that. A change prints its
        # diff, because that is the part a human has to check.
        trace = "\n".join(
            f"  {entry['name']} {json.dumps(entry['args'], ensure_ascii=False)}"
            if entry["kind"] == "tool"
            else f"  {entry['kind']} {entry['path']}\n{entry['diff']}"
            for entry in runner.session.entries
            if entry["kind"] in ("tool", "edit", "write")
        )
        body = (
            result.answer
            if result.outcome == "answered"
            else f"[{result.outcome}] {result.detail}"
        )
        tail = f"{body}\n  {result.summary()}"
        return f"{trace}\n{tail}" if trace else tail

    async def _command_evolution(self, parts: list[str]) -> str:
        evolver = self.environment.evolver
        supervisor = evolver.workspace.supervisor
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "status":
            state = await evolver.pipeline_state()
            metadata = supervisor.metadata()
            candidates = "\n".join(
                f"  {item.number} [{item.status}] {item.path}"
                for item in supervisor.candidates()
            )
            applied = str(metadata.get("active_commit") or "")
            restart = (
                "\nRESTART REQUIRED: the tree holds a newer generation than this "
                "process is running"
                if metadata.get("restart_required")
                else ""
            )
            return (
                f"active generation: {metadata['active']}"
                f"{f' ({applied[:8]})' if applied else ''}\n"
                f"last known good: {metadata['last_known_good']}\n"
                f"published: {self._publish_state(metadata)}\n"
                f"pipeline stage: {state.get('stage', 'plan')}\n"
                f"self-repairs on this candidate: {state.get('repairs', 0)}\n"
                f"candidates:\n{candidates or '  none'}{restart}"
            )
        if action == "start" and len(parts) > 2:
            definition = self.environment.registry.get("evolver")
            goal = definition.mind.add_goal(" ".join(parts[2:]), priority=1)
            definition.touch()
            await self.environment.repository.save_agent(definition)
            await evolver.reset_pipeline()
            return f"Evolver objective set ({goal.id}). It starts on its next cycle."
        if action in {"promote", "discard"} and len(parts) <= 3:
            latest = evolver.latest_candidate()
            number = int(parts[2]) if len(parts) == 3 else (latest.number if latest else 0)
            if not number:
                return "There is no candidate generation."
            if action == "promote":
                commit = await evolver.promote_candidate(number)
                await evolver.reset_pipeline()
                return (
                    f"Generation {number} promoted and applied as {commit[:8]}, "
                    f"{evolver.last_publish}. {self._restart_note()} "
                    "The pipeline is free for the next objective."
                )
            supervisor.discard(number)
            await evolver.reset_pipeline()
            return f"Generation {number} discarded. The pipeline is free for the next objective."
        if action == "rollback":
            supervisor.rollback()
            restored = await evolver.revert_tree()
            where = f" and the tree to {restored[:8]}" if restored else ""
            return (
                f"Rolled back to generation {supervisor.metadata()['active']}{where}. "
                "Restart the mesh to run it."
            )
        return "Usage: /evolution status|start <objective>|promote [n]|discard [n]|rollback"

    async def _command_telegram(self, parts: list[str]) -> str:
        """Report and manage the Telegram bot without opening a config file.

        The Control Center drives this, because the two questions a human
        actually has -- is my token any good, and who can talk to the mesh --
        cannot be answered by the settings file. A chat adopted at runtime is
        stored in the database, and a token is only good if Telegram says so.
        """
        settings = self.environment.settings.telegram
        channel = self.environment.channels.get("telegram")
        action = parts[1].lower() if len(parts) > 1 else "status"

        if action == "status":
            if not settings.enabled:
                return "Telegram is switched off. Enable it in the Control Center."
            if not settings.token.strip():
                return "Telegram is enabled but has no bot token yet."
            connected = bool(getattr(channel, "running", False))
            identity = str(getattr(channel, "identity", "") or "not connected yet")
            chats = list(getattr(channel, "allowed_chats", settings.allowed_chat_ids))
            return (
                f"bot: {identity}\n"
                f"polling: {'yes' if connected else 'no'}\n"
                f"announcements: {'on' if settings.announcements else 'off'}\n"
                f"first chat may claim it: {'yes' if settings.adopt_first_chat else 'no'}\n"
                f"allowed chats: {', '.join(str(item) for item in chats) or 'none yet'}"
            )

        if action == "test":
            if channel is None:
                return "Telegram is not running in this process."
            ok, detail = await channel.check()
            if ok:
                return f"Telegram accepted the token: {detail}"
            return f"Telegram refused it: {detail}"

        if action in {"allow", "revoke"} and len(parts) > 2:
            if channel is None:
                return "Telegram is not running in this process."
            try:
                chat_id = int(parts[2])
            except ValueError:
                return f"'{parts[2]}' is not a chat id."
            if action == "allow":
                added = await channel.allow(chat_id)
                return (
                    f"Chat {chat_id} may now talk to the mesh."
                    if added
                    else f"Chat {chat_id} was already allowed."
                )
            removed = await channel.revoke(chat_id)
            return (
                f"Chat {chat_id} can no longer talk to the mesh."
                if removed
                else f"Chat {chat_id} was not on the list."
            )

        return "Usage: /telegram status|test|allow <chat-id>|revoke <chat-id>"

    def _command_restart(self, parts: list[str]) -> str:
        """Ask whoever owns this process to bring it back on the current tree.

        The mesh cannot restart itself -- a process that exits is not around to
        start anything -- so this raises the same flag a landed generation does
        and lets the Control Center or the launcher script act on it.
        """
        self.environment.restart_reason = "a restart was requested from the console"
        self.environment.restart_requested.set()
        return (
            "Restarting EvoMesh into the code currently in the tree. "
            "The Control Center reconnects on its own once it is back."
        )

    # -- helpers --------------------------------------------------------

    def _restart_note(self) -> str:
        if self.environment.settings.evolution.auto_restart:
            return "The mesh restarts into it now."
        return "Restart the mesh to run it (auto_restart is off)."

    @staticmethod
    def _publish_state(metadata: dict[str, object]) -> str:
        detail = str(metadata.get("publish_detail") or "")
        if metadata.get("publish_ok"):
            published = str(metadata.get("published_commit") or "")
            return f"{published[:8]} to {detail}" if published else detail
        return f"no ({detail})" if detail else "nothing published yet"

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
                    f"Agents: {status['agents']} ({status['running']} running)",
                    f"Status: {status['status']}",
                    "",
                    "Type /help for commands.",
                ]
            )
        )
