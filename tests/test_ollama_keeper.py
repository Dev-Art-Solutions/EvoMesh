"""The mesh keeps its local Ollama answering (ollama_keeper.py)."""

from __future__ import annotations

from pathlib import Path

from evomesh.browser import ProcessInfo
from evomesh.config import ProviderSettings
from evomesh.ollama_keeper import OllamaKeeper, is_local, is_serving, keepers_for

URL = "http://127.0.0.1:11434"
EXE = Path("C:/Ollama/ollama.exe")


class _World:
    """Ollama's answer, the process table and the spawns, scripted."""

    def __init__(self, answers: list[bool], processes: list[ProcessInfo] | None = None) -> None:
        self.answers = answers
        self.processes = processes or []
        self.spawned: list[tuple[Path, str, Path]] = []

    async def probe(self, url: str) -> bool:
        assert url == URL
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def spawn(self, exe: Path, url: str, log: Path) -> int:
        self.spawned.append((exe, url, log))
        return 4242

    def keeper(self, tmp_path: Path, wait: float = 0.05) -> OllamaKeeper:
        return OllamaKeeper(
            URL,
            tmp_path / "ollama-serve.log",
            exe=EXE,
            start_wait_seconds=wait,
            probe=self.probe,
            processes=lambda: self.processes,
            spawn=self.spawn,
        )


async def test_an_answering_ollama_is_left_alone(tmp_path: Path) -> None:
    world = _World([True])
    assert await world.keeper(tmp_path).ensure() == "up"
    assert world.spawned == []


async def test_a_silent_ollama_with_no_server_is_started(tmp_path: Path) -> None:
    world = _World([False, False, True])
    keeper = world.keeper(tmp_path, wait=5)
    assert await keeper.ensure() == "started"
    assert world.spawned == [(EXE, URL, tmp_path / "ollama-serve.log")]
    assert keeper.started == [4242]


async def test_a_server_that_is_still_loading_is_waited_for_never_doubled(tmp_path: Path) -> None:
    loading = ProcessInfo(77, r'"C:\Users\me\AppData\Local\Programs\Ollama\ollama.exe" serve')
    world = _World([False], [loading])
    assert await world.keeper(tmp_path).ensure() == "waiting"
    assert world.spawned == [], "a second server would fight the first for the port"


async def test_a_start_that_never_answers_is_reported_not_retried_in_a_loop(
    tmp_path: Path,
) -> None:
    world = _World([False])
    assert await world.keeper(tmp_path).ensure() == "failed"
    assert len(world.spawned) == 1


def test_only_ollama_serve_counts_as_a_server() -> None:
    assert is_serving(ProcessInfo(1, r'"C:\Programs\Ollama\ollama.exe" serve'))
    assert is_serving(ProcessInfo(2, "/usr/local/bin/ollama serve"))
    assert not is_serving(ProcessInfo(3, r'"C:\Programs\Ollama\ollama app.exe"'))
    assert not is_serving(ProcessInfo(4, r'"C:\Programs\Ollama\ollama.exe" runner --model x'))
    assert not is_serving(ProcessInfo(5, "python -m http.server serve"))


def test_only_a_local_ollama_provider_that_allows_it_is_kept(tmp_path: Path) -> None:
    providers = {
        "ollama": ProviderSettings(base_url=URL, model="m"),
        "same": ProviderSettings(base_url=URL + "/", model="n", kind="ollama"),
        "remote": ProviderSettings(base_url="http://gpu-box:11434", model="m", kind="ollama"),
        "off": ProviderSettings(
            base_url="http://localhost:11500", model="m", kind="ollama", autostart=False
        ),
        "openai": ProviderSettings(base_url="http://127.0.0.1:8000/v1", model="m"),
    }
    keepers = keepers_for(dict(providers), tmp_path)
    assert [keeper.base_url for keeper in keepers] == [URL]
    assert keepers[0].log_path == tmp_path / "ollama-serve.log"
    assert is_local("http://localhost:11434") and not is_local("http://10.0.0.5:11434")
