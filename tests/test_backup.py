"""A backup restores into its own directory with grants, outbox and agents
intact, and refuses a tampered archive or a live installation."""

import sys
import zipfile
from pathlib import Path

import pytest

from evomesh.backup import BackupError, create, main, restore, verify
from evomesh.contracts import AgentDefinition, DataCollectorGrant
from evomesh.datacollector import build_event
from evomesh.storage import SQLiteRepository


async def _installation(root: Path) -> None:
    repository = SQLiteRepository(root / "data" / "evomesh.db")
    await repository.initialize()
    await repository.save_agent(
        AgentDefinition(
            name="Scout",
            purpose="p",
            mail_read=["support"],
            data_collector=DataCollectorGrant(write=["business"]),
        )
    )
    event = build_event(
        agent_id="Scout", entity_type="business", source_key="b:1", payload={"n": 1}
    )
    await repository.dc_enqueue(
        {
            "event_id": event["event_id"],
            "source_key": event["source_key"],
            "content_hash": event["content_hash"],
            "entity_type": "business",
            "agent_id": "Scout",
            "payload": "{}",
            "state": "pending",
            "created_at": event["collected_at"],
        }
    )
    (root / "workspace" / "agents" / "scout").mkdir(parents=True)
    (root / "workspace" / "agents" / "scout" / "memory.md").write_text("remember", encoding="utf-8")
    (root / "skills" / "mine").mkdir(parents=True)
    (root / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\n---\n", encoding="utf-8")
    (root / "data" / "browser" / "profile").mkdir(parents=True)
    (root / "data" / "browser" / "profile" / "Cookies").write_bytes(b"session")
    (root / "evomesh.yaml").write_text("environment_name: test\n", encoding="utf-8")
    (root / "evomesh.secrets.yaml").write_text("k: secret\n", encoding="utf-8")


async def test_create_verify_restore_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "live"
    await _installation(source)
    archive = tmp_path / "b.zip"
    manifest = create(source, archive)
    assert manifest["schema_version"] >= 4
    names = set(manifest["files"])
    assert "workspace/agents/scout/memory.md" in names and "skills/mine/SKILL.md" in names
    assert not any("profile" in name or "secrets" in name for name in names)
    assert verify(archive)["files"] == manifest["files"]

    target = tmp_path / "rehearsal"
    restore(archive, target)
    restored = SQLiteRepository(target / "data" / "evomesh.db")
    await restored.initialize()
    agents = await restored.load_agents()
    assert agents[0].mail_read == ["support"]
    assert agents[0].data_collector == DataCollectorGrant(write=["business"])
    assert (await restored.dc_counts()) == {"pending": 1}

    with pytest.raises(BackupError, match="already has a database"):
        restore(archive, target)
    restore(archive, target, overwrite=True)


async def test_a_tampered_archive_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "live"
    await _installation(source)
    archive = tmp_path / "b.zip"
    create(source, archive)
    tampered = tmp_path / "t.zip"
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(tampered, "w") as copy:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == "evomesh.yaml":
                data = b"environment_name: evil\n"
            copy.writestr(item, data)
    with pytest.raises(BackupError, match="checksum"):
        verify(tampered)
    assert main(["verify", str(tampered)]) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
async def test_secrets_are_dpapi_protected_and_come_back(tmp_path: Path) -> None:
    source = tmp_path / "live"
    await _installation(source)
    archive = tmp_path / "b.zip"
    create(source, archive, include_secrets=True)
    with zipfile.ZipFile(archive) as opened:
        assert b"secret" not in opened.read("evomesh.secrets.yaml.dpapi")
    restore(archive, tmp_path / "r")
    assert (tmp_path / "r" / "evomesh.secrets.yaml").read_text(encoding="utf-8") == "k: secret\n"
