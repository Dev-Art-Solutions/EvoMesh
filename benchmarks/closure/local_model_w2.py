"""W2 on a real local model (closure plan v2 20.5, AC-23).

Mode 4 (local-model experiment): the shipped ``report_comparison@1``
procedure runs through the real executor, the real JSON adapters and the
real Ollama provider, in a disposable workspace per run. Two context
profiles, three held-out input pairs, three runs each. Every attempt is
recorded -- malformed output, repairs and failures included.

This measures the orchestration's call accounting and the model's ability
to satisfy one bounded, checked contract. It is not evidence of analytic
quality beyond the checks named in each row.

    uv run python -m benchmarks.closure.local_model_w2 [--model TAG] [--runs N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from evomesh.cognitive_services import (
    CognitiveModelService,
    CognitiveServiceType,
    ModelInvocationReason,
)
from evomesh.contracts import FilesystemGrant, now_utc
from evomesh.harness_tools import ToolContext
from evomesh.models import OllamaProvider
from evomesh.permissions import FilesystemPolicy
from evomesh.procedure_runtime import ProcedureExecutor, ProcedureRegistry, core_catalog
from evomesh.storage import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs" / "architecture" / "closure-evidence" / "local-model-w2.json"
BASE_URL = "http://127.0.0.1:11434"
PROFILES = {"constrained-4k": 4096, "larger-32k": 32768}



def _findings(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": identifier, "text": text} for identifier, text in pairs]


# Held out: none of these appear in any test or in the procedure's own text.
INPUTS: dict[str, tuple[list[dict[str, str]], list[dict[str, str]]]] = {
    "latency": (
        _findings(("A1", "p95 checkout latency 420 ms"), ("A2", "error rate 1.2%")),
        _findings(("B1", "p95 checkout latency 210 ms"), ("B2", "error rate 1.3%")),
    ),
    "inventory": (
        _findings(
            ("INV-7", "warehouse Sofia stock 1,200 units"),
            ("INV-8", "warehouse Plovdiv stock 300 units"),
        ),
        _findings(
            ("INV-9", "warehouse Sofia stock 640 units"),
            ("INV-10", "warehouse Plovdiv stock 310 units"),
            ("INV-11", "a new warehouse in Varna holds 500 units"),
        ),
    ),
    "support": (
        _findings(("S-100", "median first response 9 hours"), ("S-101", "CSAT 71")),
        _findings(("S-200", "median first response 2 hours"), ("S-201", "CSAT 83")),
    ),
}


class Host:
    def __init__(
        self,
        root: Path,
        policy: FilesystemPolicy,
        provider: OllamaProvider,
        cognition: CognitiveModelService,
        num_ctx: int,
    ) -> None:
        self.root = root
        self.policy = policy
        self.provider = provider
        self.cognition = cognition
        self.num_ctx = num_ctx
        self.blackboard = None

    def capabilities(self, agent_id: str) -> set[str]:
        return {"artifact.read", "artifact.write"}

    def tool_context(self, agent_id: str) -> ToolContext:
        return ToolContext(root=self.root, policy=self.policy, agent_id=agent_id, allow_write=True)

    async def think(self, agent_id: str, prompt: str, **kwargs: Any) -> str:
        return await self.cognition.generate(
            self.provider,
            prompt,
            service=CognitiveServiceType(kwargs["service"]),
            reason=ModelInvocationReason(kwargs["reason"]),
            provider_name="ollama",
            agent_id=agent_id,
            goal_id=str(kwargs.get("goal_id", "")),
            task_id=str(kwargs.get("task_id", "")),
            system="You perform one typed reasoning step. Answer with JSON only.",
            model=self.provider.model,
            num_ctx=self.num_ctx,
            format=dict(kwargs["schema"]),
        )

    async def route(self, work: Any, requester_id: str) -> tuple[str | None, str]:
        return None, "not used"

    async def deliver(self, work: Any, requester_id: str, assignee_id: str) -> None:
        return None


async def one_run(model: str, profile: str, num_ctx: int, name: str, index: int) -> dict[str, Any]:
    first, second = INPUTS[name]
    base = Path(tempfile.mkdtemp(prefix="evomesh-w2-"))
    try:
        repository = SQLiteRepository(base / "state.db")
        await repository.initialize()
        policy = FilesystemPolicy(repository)
        root = base / "work"
        root.mkdir()
        (root / "first.json").write_text(json.dumps({"findings": first}), encoding="utf-8")
        (root / "second.json").write_text(json.dumps({"findings": second}), encoding="utf-8")
        await policy.grant(FilesystemGrant(agent_id="analyst", path=str(root), write=True))
        registry = ProcedureRegistry(repository, core_catalog())
        await registry.load()
        await registry.install(ROOT / "procedures")
        executor = ProcedureExecutor(repository, registry)
        cognition = CognitiveModelService()
        provider = OllamaProvider(BASE_URL, model, 600, num_ctx)
        host = Host(root, policy, provider, cognition, num_ctx)
        parameters = {"first": "first.json", "second": "second.json", "destination": "out.json"}
        match = registry.select("report_comparison", parameters, host.capabilities("analyst"))
        assert match.definition is not None and match.admission is not None, match.reasons
        execution = await executor.start(
            match.definition,
            match.admission,
            agent_id="analyst",
            goal_id=f"{name}-{index}",
            occurrence_id=f"{profile}:{name}:{index}",
            parameters=parameters,
        )
        started = time.perf_counter()
        outcome = None
        for _ in range(20):
            outcome = await executor.advance(execution.execution_id, host)
            if outcome.kind in {"completed", "failed", "cancelled", "needs_reconciliation"}:
                break
        elapsed = time.perf_counter() - started
        _, final = await executor.load(execution.execution_id)
        calls = [
            {
                "status": record.status.value,
                "input_chars": record.input_chars,
                "output_chars": record.output_chars,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "seconds": round(record.duration_seconds, 2),
                "error": record.error[:200],
            }
            for record in cognition.metrics.records
        ]
        output = final.results.get("compare") if isinstance(final.results, dict) else None
        given = {item["id"] for item in [*first, *second]}
        cited = list((output or {}).get("evidence_ids", [])) if isinstance(output, dict) else []
        return {
            "profile": profile,
            "num_ctx": num_ctx,
            "input": name,
            "run": index,
            "status": final.status.value,
            "code": outcome.code if outcome is not None else "",
            "model_calls": final.budget.model_calls,
            "repairs": max(0, final.budget.model_calls - 1),
            "calls": calls,
            "seconds": round(elapsed, 2),
            "cited_ids": cited,
            "all_citations_in_inputs": bool(cited) and set(cited) <= given,
            "summary": str(output.get("summary", ""))[:600] if isinstance(output, dict) else "",
            "artifact_written": (root / "out.json").is_file(),
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _loaded_context(model: str) -> Any:
    """What the server says it loaded, where it says (plan 20.5: a setting
    in the request is not proof it was enforced)."""
    try:
        running = httpx.get(f"{BASE_URL}/api/ps", timeout=10).json().get("models", [])
    except httpx.HTTPError as exc:
        return f"unavailable: {exc}"
    for item in running:
        if item.get("name") == model or item.get("model") == model:
            return {key: item.get(key) for key in ("context_length", "size_vram", "size")}
    return None


def server_facts(model: str) -> tuple[dict[str, Any], str | None]:
    tags = httpx.get(f"{BASE_URL}/api/tags", timeout=10).json().get("models", [])
    identity = next((item for item in tags if item.get("name") == model), {})
    return identity, httpx.get(f"{BASE_URL}/api/version", timeout=10).json().get("version")


async def main(model: str, runs: int) -> dict[str, Any]:
    identity, version = server_facts(model)
    rows: list[dict[str, Any]] = []
    loaded: dict[str, Any] = {}
    for profile, num_ctx in PROFILES.items():
        for name in INPUTS:
            for index in range(runs):
                row = await one_run(model, profile, num_ctx, name, index)
                rows.append(row)
                print(
                    f"{profile} {name} #{index}: {row['status']} {row['code']} "
                    f"calls={row['model_calls']} cited={row['cited_ids']} {row['seconds']}s",
                    flush=True,
                )
        loaded[profile] = _loaded_context(model)
    completed = [row for row in rows if row["status"] == "completed"]
    report = {
        "mode": "local_model_experiment",
        "workflow": "W2 report_comparison@1 through ProcedureExecutor with real adapters",
        "recorded_at": now_utc().isoformat(),
        "provider": {"kind": "ollama", "base_url": BASE_URL, "version": version},
        "model": {
            "tag": model,
            "digest": identity.get("digest"),
            "parameter_size": identity.get("details", {}).get("parameter_size"),
            "quantization": identity.get("details", {}).get("quantization_level"),
        },
        "machine": {"platform": platform.platform(), "python": platform.python_version()},
        "profiles": PROFILES,
        "effective_context_reported_by_server": loaded,
        "tool_free": True,
        "output_budget": "provider default (no num_predict set)",
        "inputs": {name: {"first": a, "second": b} for name, (a, b) in INPUTS.items()},
        "runs_per_input_profile": runs,
        "denominator": len(rows),
        "completed": len(completed),
        "first_call_success": sum(1 for row in completed if row["model_calls"] == 1),
        "needed_repair": sum(1 for row in completed if row["model_calls"] > 1),
        "failed": len(rows) - len(completed),
        "rows": rows,
        "limitations": [
            "Three held-out inputs and three runs each: an initial report, not statistical proof.",
            "Checks cover schema validity and that cited ids exist in the inputs; they do not "
            "judge whether the summary's analysis is correct.",
            "A single local model on one machine; nothing here speaks for other models or sizes.",
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="ornith-1.5:35b")
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    result = asyncio.run(main(arguments.model, arguments.runs))
    print(
        f"{result['completed']}/{result['denominator']} completed, "
        f"{result['first_call_success']} on the first call, {result['needed_repair']} repaired"
    )
