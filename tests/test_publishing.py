"""Publishing a generation, restarting into it, and talking to it over Telegram.

The three features share one thread: a generation that validated is worth
nothing until the code is in the tree, the tree is on the remote, and the
process is actually running it. Each test here covers one link in that chain,
plus the second console a human reaches it all from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from evomesh.config import (
    EvolutionSettings,
    GitSettings,
    RuntimeSettings,
    Settings,
    TelegramSettings,
)
from evomesh.console import ConsoleChannel
from evomesh.environment import Environment
from evomesh.evolution import (
    CandidateWorkspace,
    EnvironmentEvolver,
    Generation,
    ValidationResult,
)
from evomesh.git import GitError, GitIdentity, GitRepository, PublishPolicy
from evomesh.models import MockProvider
from evomesh.storage import SQLiteRepository
from evomesh.telegram import TelegramChannel

MUTATED = "ACTIVE = False\n"


async def checkout(root: Path) -> Path:
    """A real checkout. Authorship and pushing cannot be faked usefully.

    The ignores mirror the real project's: generations and runtime state live
    inside the checkout, and a tree that counted them as changes would refuse
    every generation for uncommitted work nobody wrote.
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("ACTIVE = True\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "generations/\nworkspace/\ndata/\n*.db\n", encoding="utf-8"
    )
    repository = GitRepository(root, GitIdentity("A Human", "human@example.com"))
    await repository.run("init", "-b", "main")
    await repository.run("add", "-A")
    await repository.run("commit", "-m", "initial")
    return root


async def remote_for(project: Path, bare: Path) -> Path:
    """A bare repository standing in for GitHub."""
    await GitRepository(bare.parent).run("init", "--bare", str(bare))
    await GitRepository(project).run("remote", "add", "origin", str(bare))
    return bare


async def evolver_for(
    tmp_path: Path, project: Path, publish: PublishPolicy | None = None
) -> EnvironmentEvolver:
    repository = SQLiteRepository(tmp_path / "state.db")
    await repository.initialize()
    return EnvironmentEvolver(
        CandidateWorkspace(project, tmp_path / "generations"),
        repository,
        publish=publish,
    )


async def change(
    evolver: EnvironmentEvolver,
    generation: Generation,
    objective: str,
    rationale: str,
    status: str = "applied",
    diff: str = "",
) -> None:
    """Write a file into the candidate and record it, as the harness does.

    The harness writes the file and hands the pipeline the session entries; a
    test does the same two things by hand rather than through a model.
    """
    (generation.path / "src" / "app.py").write_text(MUTATED, encoding="utf-8")
    await evolver.record_harness_changes(
        generation,
        [{"kind": "edit", "path": "src/app.py", "diff": diff}],
        objective,
        rationale,
        status,
    )


async def landed(evolver: EnvironmentEvolver) -> str:
    """Open a candidate, change one file in it, and apply it to the checkout."""
    generation = await evolver.create_candidate("keep the mesh honest")
    await change(evolver, generation, "keep the mesh honest", "flip it")
    return await evolver.apply_generation(generation.number, "keep the mesh honest")


# -- authorship ----------------------------------------------------------


async def test_a_generation_is_signed_by_the_mesh_not_by_whoever_owns_the_machine(
    tmp_path: Path,
) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project)

    await landed(evolver)

    author = await GitRepository(project).run("log", "-1", "--format=%an <%ae>")
    assert author.strip() == "Mesh Evo Agent <mesh-evo-agent@evomesh.local>"
    # The committer matters too: a history where the agent authored and a human
    # committed reads as work the human signed off on, which nobody did.
    committer = await GitRepository(project).run("log", "-1", "--format=%cn")
    assert committer.strip() == "Mesh Evo Agent"


async def test_the_author_identity_is_configurable(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project)
    evolver.identity = GitIdentity("Other Agent", "other@example.com")

    await landed(evolver)

    author = await GitRepository(project).run("log", "-1", "--format=%an <%ae>")
    assert author.strip() == "Other Agent <other@example.com>"


# -- publishing ----------------------------------------------------------


async def test_a_landed_generation_reaches_the_remote(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    bare = await remote_for(project, tmp_path / "remote.git")
    evolver = await evolver_for(tmp_path, project)

    applied = await landed(evolver)

    assert evolver.last_publish == "published to origin/main"
    published = await GitRepository(bare).run("rev-parse", "main")
    assert published.strip() == applied
    metadata = evolver.workspace.supervisor.metadata()
    assert metadata["publish_ok"] is True
    assert metadata["published_commit"] == applied


async def test_a_remote_that_is_not_there_is_reported_not_swallowed(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project)

    applied = await landed(evolver)

    # The generation is in the tree either way: a push is the last step, never
    # a gate on work that already validated.
    assert (project / "src" / "app.py").read_text(encoding="utf-8") == MUTATED
    assert applied
    assert evolver.last_publish.startswith("not published:")
    assert "origin" in evolver.last_publish
    assert evolver.workspace.supervisor.metadata()["publish_ok"] is False


async def test_publishing_can_be_switched_off(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    bare = await remote_for(project, tmp_path / "remote.git")
    evolver = await evolver_for(tmp_path, project, PublishPolicy(enabled=False))

    await landed(evolver)

    assert evolver.last_publish == "not published (auto_push is off)"
    # Nothing was pushed, so the remote still has no branch at all.
    try:
        await GitRepository(bare).run("rev-parse", "main")
    except GitError:
        pass
    else:
        raise AssertionError("the remote received a commit with auto_push off")


async def test_a_detached_head_is_refused_by_name(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    await remote_for(project, tmp_path / "remote.git")
    repository = GitRepository(project)
    await repository.run("checkout", "--detach", "HEAD")

    evolver = await evolver_for(tmp_path, project)
    await landed(evolver)

    assert "detached" in evolver.last_publish


# -- restarting into the new code ---------------------------------------


def settings_for(tmp_path: Path, project: Path, **overrides: Any) -> Settings:
    return Settings(
        data_path=tmp_path / "state.db",
        generation_path=project / "generations",
        workspace_path=tmp_path / "workspace",
        runtime=RuntimeSettings(cycle_seconds=3600, stagger_seconds=0),
        **overrides,
    )


async def test_a_landed_generation_asks_the_process_to_restart(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    environment = Environment(
        settings_for(tmp_path, project, git=GitSettings(auto_push=False)),
        {"ollama": MockProvider([])},
    )
    await environment.start()

    await landed(environment.evolver)

    assert environment.restart_requested.is_set()
    assert "landed as" in environment.restart_reason
    # The durable flag is what survives a process that does not come back.
    assert environment.evolver.workspace.supervisor.metadata()["restart_required"] is True


async def test_auto_restart_off_still_records_that_a_restart_is_owed(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    environment = Environment(
        settings_for(
            tmp_path,
            project,
            evolution=EvolutionSettings(auto_restart=False),
            git=GitSettings(auto_push=False),
        ),
        {"ollama": MockProvider([])},
    )
    await environment.start()

    await landed(environment.evolver)

    assert not environment.restart_requested.is_set()
    assert environment.evolver.workspace.supervisor.metadata()["restart_required"] is True


async def test_starting_up_clears_the_restart_it_just_paid(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    settings = settings_for(tmp_path, project, git=GitSettings(auto_push=False))
    first = Environment(settings, {"ollama": MockProvider([])})
    await first.start()
    await landed(first.evolver)
    await first.stop()

    second = Environment(settings, {"ollama": MockProvider([])})
    await second.start()

    assert not second.restart_requested.is_set()
    assert not second.evolver.workspace.supervisor.metadata()["restart_required"]


# -- the backlog ---------------------------------------------------------


async def test_a_landed_generation_explains_itself_in_the_commit(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project, PublishPolicy(enabled=False))

    applied = await landed(evolver)

    # The reasoning is in the repository, not only in a database on one machine.
    entry = project / "docs" / "evolution" / "000002.md"
    text = entry.read_text(encoding="utf-8")
    assert "# Generation 2" in text
    assert "keep the mesh honest" in text
    assert "src/app.py" in text
    assert "flip it" in text
    assert "Mesh Evo Agent" in text

    committed = await GitRepository(project).run("show", "--stat", "--name-only", applied)
    assert "docs/evolution/000002.md" in committed
    assert "docs/evolution/README.md" in committed


async def test_the_backlog_index_lists_every_generation(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project, PublishPolicy(enabled=False))
    await landed(evolver)

    index = (project / "docs" / "evolution" / "README.md").read_text(encoding="utf-8")

    assert "# Evolution backlog" in index
    assert "[Generation 2](000002.md)" in index
    # The index heading is the model's own rationale for its edit, not the
    # Evolver's standing goal repeated on every generation.
    assert "flip it" in index


async def test_the_backlog_records_the_verdict_and_the_repairs(tmp_path: Path) -> None:
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project, PublishPolicy(enabled=False))
    generation = await evolver.create_candidate("make the mesh honest")
    await change(evolver, generation, "make the mesh honest", "flip")
    await change(
        evolver, generation, "make the mesh honest", "unbreak", status="repaired"
    )
    (generation.path / "validation-result.json").write_text(
        ValidationResult(
            passed=False,
            commands=[
                {"command": "uv run ruff check .", "exit_code": 0, "output": ""},
                {"command": "uv run pytest", "exit_code": 1, "output": "E   assert 1 == 2"},
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    text = evolver.render_backlog(generation, repairs=2)

    assert "**Repair to `src/app.py`** — unbreak" in text
    assert "**Change to `src/app.py`** — flip" in text
    assert "The suite **failed**" in text
    assert "| `uv run pytest` | 1 |" in text
    assert "assert 1 == 2" in text
    # The headline is the model's own reason for the edit, not the goal.
    assert "**What it set out to do.** flip" in text
    assert "*Standing goal: make the mesh honest*" in text


async def test_a_generation_with_no_rationale_still_produces_a_readable_entry(
    tmp_path: Path,
) -> None:
    """A small local model routinely returns an empty rationale field.

    The entry still has to say what changed; an empty section would make the
    backlog look broken rather than sparse.
    """
    project = await checkout(tmp_path / "project")
    evolver = await evolver_for(tmp_path, project, PublishPolicy(enabled=False))
    generation = await evolver.create_candidate("do something")
    await change(evolver, generation, "do something", "  ")

    text = evolver.render_backlog(generation)

    assert "src/app.py" in text
    assert "the model gave no rationale" in text
    assert "Validation did not run" in text
    # The headline says so honestly instead of repeating the standing goal.
    assert (
        "**What it set out to do.** The model changed `src/app.py` but gave no "
        "rationale for it" in text
    )


# -- Telegram ------------------------------------------------------------


class FakeTelegram:
    """The three Telegram endpoints the channel uses, and nothing else."""

    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.sent: list[dict[str, Any]] = []
        self.channel: TelegramChannel | None = None
        self._served = False

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        if method == "getMe":
            return self._ok({"username": "evomesh_test_bot"})
        if method == "sendMessage":
            self.sent.append(payload)
            return self._ok({"message_id": len(self.sent)})
        if method == "getUpdates":
            if self._served:
                # One pass is enough; stop the loop rather than spinning.
                assert self.channel is not None
                self.channel.stop()
                return self._ok([])
            self._served = True
            return self._ok(self.updates)
        raise AssertionError(f"unexpected Telegram method {method}")

    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})


def update(chat_id: int, text: str, update_id: int = 1) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


async def telegram_environment(tmp_path: Path) -> Environment:
    project = await checkout(tmp_path / "project")
    environment = Environment(settings_for(tmp_path, project), {"ollama": MockProvider([])})
    await environment.start()
    return environment


async def run_channel(
    environment: Environment, settings: TelegramSettings, fake: FakeTelegram
) -> TelegramChannel:
    channel = TelegramChannel(environment, settings, fake.client())
    fake.channel = channel
    await channel.run()
    return channel


async def test_a_telegram_message_is_answered_by_the_same_command_router(
    tmp_path: Path,
) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegram([update(4242, "/status")])

    await run_channel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake,
    )

    assert [item["chat_id"] for item in fake.sent] == [4242]
    # The same text a human would see in the desktop console.
    assert "environment:" in fake.sent[0]["text"]


async def test_the_first_chat_claims_the_bot_and_is_remembered(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegram([update(777, "/start")])

    channel = await run_channel(
        environment, TelegramSettings(enabled=True, token="1:abc"), fake
    )

    assert "EvoMesh is connected" in fake.sent[0]["text"]
    stored = await environment.repository.load_state("telegram.allowed_chats")
    assert stored == [777]
    assert channel._allowed == {777}  # noqa: SLF001 - the point of the test


async def test_a_stranger_is_turned_away_once_the_bot_is_claimed(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegram([update(999, "/status")])

    await run_channel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake,
    )

    assert "not allowed" in fake.sent[0]["text"]
    assert "999" in fake.sent[0]["text"]


async def test_the_mesh_cannot_be_shut_down_from_a_phone(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegram([update(4242, "/exit")])

    await run_channel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242]),
        fake,
    )

    assert "only possible from the Control Center" in fake.sent[0]["text"]
    assert environment.health_state == "READY"


async def test_the_offset_survives_a_restart_so_commands_are_not_replayed(
    tmp_path: Path,
) -> None:
    environment = await telegram_environment(tmp_path)
    settings = TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[4242])
    await run_channel(environment, settings, FakeTelegram([update(4242, "/status", 17)]))

    assert await environment.repository.load_state("telegram.offset") == 18

    # A second process -- the one an automatic restart brings up -- must not see
    # the message that caused the restart a second time.
    again = FakeTelegram([update(4242, "/status", 17)])
    channel = TelegramChannel(environment, settings, again.client())
    again.channel = channel
    await channel.run()
    assert channel._offset == 18  # noqa: SLF001 - the point of the test


async def test_an_announcement_reaches_every_allowed_chat(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    settings = TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[1, 2])
    fake = FakeTelegram([])
    channel = TelegramChannel(environment, settings, fake.client())

    await channel.announce("generation 3 landed")

    assert [item["chat_id"] for item in fake.sent] == [1, 2]
    assert fake.sent[0]["text"] == "generation 3 landed"


async def test_an_answer_past_the_telegram_limit_arrives_in_pieces(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    fake = FakeTelegram([])
    channel = TelegramChannel(
        environment,
        TelegramSettings(enabled=True, token="1:abc", allowed_chat_ids=[7]),
        fake.client(),
    )

    await channel.send(7, "\n".join(f"line {index}" for index in range(1000)))

    assert len(fake.sent) > 1
    assert all(len(item["text"]) <= 3800 for item in fake.sent)
    # Nothing is dropped between the pieces.
    assert "line 999" in fake.sent[-1]["text"]


async def test_the_console_reports_who_may_use_the_bot(tmp_path: Path) -> None:
    """The Control Center's 'Live status' button runs exactly this."""
    environment = await telegram_environment(tmp_path)
    environment.settings.telegram = TelegramSettings(
        enabled=True, token="1:abc", allowed_chat_ids=[4242]
    )
    fake = FakeTelegram([])
    channel = TelegramChannel(environment, environment.settings.telegram, fake.client())
    environment.channels["telegram"] = channel
    # A chat that claimed the bot at runtime is in the database, not in the
    # config file, so the settings tab alone can never show it.
    await channel.allow(777)

    answer = await ConsoleChannel(environment).route("/telegram status")

    assert "4242" in answer
    assert "777" in answer
    assert "polling: no" in answer


async def test_the_console_can_add_and_remove_a_chat(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    settings = TelegramSettings(enabled=True, token="1:abc")
    fake = FakeTelegram([])
    channel = TelegramChannel(environment, settings, fake.client())
    environment.settings.telegram = settings
    environment.channels["telegram"] = channel
    console = ConsoleChannel(environment)

    assert "may now talk" in await console.route("/telegram allow 5150")
    assert channel.allowed_chats == [5150]
    assert "already allowed" in await console.route("/telegram allow 5150")
    assert "no longer" in await console.route("/telegram revoke 5150")
    assert channel.allowed_chats == []
    assert "not a chat id" in await console.route("/telegram allow banana")

    # Survives a restart, because it is written to the database.
    assert await environment.repository.load_state("telegram.allowed_chats") == []


async def test_the_console_reports_a_token_telegram_refuses(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)
    settings = TelegramSettings(enabled=True, token="1:wrong")
    environment.settings.telegram = settings

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    channel = TelegramChannel(
        environment, settings, httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    environment.channels["telegram"] = channel

    answer = await ConsoleChannel(environment).route("/telegram test")

    assert "refused" in answer
    assert "401" in answer


async def test_the_console_says_so_when_telegram_is_switched_off(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)

    assert "switched off" in await ConsoleChannel(environment).route("/telegram status")


async def test_telegram_stays_off_until_it_is_configured(tmp_path: Path) -> None:
    environment = await telegram_environment(tmp_path)

    assert not TelegramChannel(environment, TelegramSettings()).configured
    assert not TelegramChannel(environment, TelegramSettings(enabled=True)).configured
    assert TelegramChannel(environment, TelegramSettings(enabled=True, token="1:a")).configured
