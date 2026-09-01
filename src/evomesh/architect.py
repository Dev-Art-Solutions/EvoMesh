"""Agent Architect: turn one sentence into a complete agent definition.

The old interview asked six mandatory questions before it would build anything.
On a small local model that is a disaster: the human answers drift, the model
forgets the beginning of the interview by the end of it, and no agent is created.

So the interview is inverted. Everything is derived deterministically from the
first description, the model is given exactly one optional pass to improve the
wording, and the human refines by saying what to change rather than by answering
a questionnaire. There is never more than one question, and even that one has a
working default behind it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from evomesh.contracts import AgentDefinition, AgentStatus, Autonomy

Inference = Callable[[str, str], Awaitable[str]]

FILLER = re.compile(
    r"^\s*(please\s+)?(i\s+(want|need)\s+)?(you\s+to\s+)?"
    r"(create|make|build|add|design|set\s?up|give\s+me)?\s*"
    r"(a|an|the)?\s*(new\s+)?(agent\s+(that|which|to|for)?)?\s*",
    re.IGNORECASE,
)
NAMED = re.compile(r"\b(?:called|named)\s+[\"']?([\w .-]{2,40}?)[\"']?(?:[,.]|\s+that\b|$)", re.I)
MODEL_SPEC = re.compile(r"\b([a-z_]+):([\w.:-]+)\b")
PATH_LIKE = re.compile(r"(?:^|\s)((?:[A-Za-z]:[\\/]|~[\\/]|\.{0,2}[\\/])[^\s,;]+)")
STOP_WORDS = frozenset(
    {
        "a", "an", "the", "that", "which", "for", "to", "of", "and", "or", "my",
        "our", "with", "on", "in", "it", "its", "should", "will", "can", "agent",
    }
)

SKILL_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("markdown", ".md", "notes", "documentation", "docs"), ("Markdown.Read",)),
    (("write", "save", "create file", "edit", "author"), ("Filesystem.Write",)),
    (("read", "scan", "summar", "review", "analyse", "analyze", "research"), ("Filesystem.Read",)),
    (("git", "commit", "repository", "repo", "diff"), ("Git.Status", "Git.Diff")),
)

DEFAULT_CONSTRAINTS = (
    "Stay inside granted paths, act only on the stated purpose, "
    "and report instead of guessing when information is missing."
)

REFINE_KEYS = ("name", "purpose", "model", "skills", "access", "constraints")

# A name is a name, not a path, a JSON fragment, or a sentence.
NAME_FORBIDDEN = "/\\:{}[]\"'\n\r\t"

# Names a small model reaches for when it has nothing to say. The derived name is
# always more useful than these, so they never win.
GENERIC_NAMES = frozenset(
    {
        "", "agent", "new agent", "the agent", "my agent", "an agent", "assistant",
        "ai", "ai agent", "bot", "chatbot", "helper", "name", "agent name",
        "agentname", "newagent", "untitled", "todo", "tbd", "none", "null",
    }
)

DERIVE_PROMPT = (
    "A human asked for an agent. Fill in the blanks. Return only JSON with keys "
    'name, purpose, constraints. Keep name under four words. No questions.\n\nRequest: '
)


def _title(text: str) -> str:
    return " ".join(word[:1].upper() + word[1:] for word in text.split() if word)


def plausible_name(value: str) -> bool:
    """A name, not a path, a sentence, an empty string, or the word "agent"."""
    if not 2 <= len(value) <= 40 or not any(character.isalpha() for character in value):
        return False
    if any(character in value for character in NAME_FORBIDDEN):
        return False
    if not 1 <= len(value.split()) <= 4:
        return False
    lowered = value.strip().lower()
    if lowered in GENERIC_NAMES:
        return False
    # "Agent" on its own carries nothing; the derived name always beats it.
    return lowered.removesuffix("agent").strip(" -_") not in GENERIC_NAMES


def plausible_purpose(value: str, need: str) -> bool:
    """A purpose must not be less informative than what the human already said.

    Small models love to answer with the first clause and drop the rest, which
    quietly halves the agent's job. When in doubt the human's own sentence wins.
    """
    floor = max(25, min(int(len(need) * 0.6), 200))
    return len(value.split()) >= 4 and len(value) >= floor


def plausible_constraints(value: str) -> bool:
    return len(value.split()) >= 4


def derive_name(need: str) -> str:
    explicit = NAMED.search(need)
    if explicit:
        return _title(explicit.group(1).strip())
    stripped = FILLER.sub("", need, count=1)
    words = [
        word
        for word in re.findall(r"[A-Za-z][\w-]*", stripped)
        if word.lower() not in STOP_WORDS
    ][:2]
    return f"{_title(' '.join(words))} Agent".strip() if words else "New Agent"


def derive_skills(need: str) -> list[str]:
    lowered = need.lower()
    found: list[str] = []
    for hints, skills in SKILL_HINTS:
        if any(hint in lowered for hint in hints):
            found.extend(skill for skill in skills if skill not in found)
    return found


def derive_access(need: str) -> str:
    match = PATH_LIKE.search(need)
    return match.group(1).strip() if match else "none"


def derive_model(need: str, provider: str, model: str) -> tuple[str, str]:
    for candidate_provider, candidate_model in MODEL_SPEC.findall(need):
        if candidate_provider.lower() in {"ollama", "inferhub", "openai_compatible", "local"}:
            return candidate_provider.lower(), candidate_model
    return provider, model


@dataclass
class ArchitectInterview:
    """Holds one draft candidate. Refined by instruction, not by questionnaire."""

    answers: dict[str, str] = field(default_factory=dict)
    candidate: AgentDefinition | None = None

    def begin(
        self, initial_need: str, provider: str = "ollama", model: str = "qwen3"
    ) -> str:
        need = initial_need.strip()
        if not need:
            return "Describe the agent you need in one sentence and I will draft it."
        selected_provider, selected_model = derive_model(need, provider, model)
        self.answers = {
            "initial_need": need,
            "name": derive_name(need),
            "purpose": need,
            "constraints": DEFAULT_CONSTRAINTS,
            "access": derive_access(need),
            "skills": ", ".join(derive_skills(need)),
            "model": f"{selected_provider}:{selected_model}",
        }
        self._build()
        return self.summary()

    async def draft(
        self,
        initial_need: str,
        provider: str = "ollama",
        model: str = "qwen3",
        infer: Inference | None = None,
    ) -> str:
        """Deterministic draft, then at most one model call to improve the wording."""
        self.begin(initial_need, provider, model)
        if infer is None or self.candidate is None:
            return self.summary()
        try:
            raw = await infer(
                DERIVE_PROMPT + initial_need.strip(),
                "You name and describe agents. Output JSON only, never a question.",
            )
            self._absorb(raw)
        except (RuntimeError, ValueError, TimeoutError, json.JSONDecodeError):
            pass  # the deterministic draft already stands on its own
        self._build()
        return self.summary()

    def _absorb(self, raw: str) -> None:
        """Take the model's wording only where it is plausibly an improvement.

        A small model will happily answer {"name": "D:/notes", "purpose": "read"}.
        Accepting that silently replaces a working draft with junk, so each field
        has to look like the thing it claims to be before it overrides the
        deterministic value.
        """
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            return
        payload = json.loads(raw[start : end + 1])
        if not isinstance(payload, dict):
            return
        need = self.answers["initial_need"]
        checks = {
            "name": plausible_name,
            "purpose": lambda value: plausible_purpose(value, need),
            "constraints": plausible_constraints,
        }
        for key, is_plausible in checks.items():
            value = payload.get(key)
            if isinstance(value, str) and is_plausible(value.strip()):
                self.answers[key] = value.strip()

    def refine(self, text: str) -> str:
        """Apply a correction. Anything unrecognised sharpens the purpose."""
        if self.candidate is None:
            return self.begin(text)
        instruction = text.strip()
        if not instruction:
            return self.summary()
        lowered = instruction.lower()
        key, _, value = instruction.partition(":")
        if key.strip().lower() in REFINE_KEYS and value.strip():
            self.answers[key.strip().lower()] = value.strip()
        elif MODEL_SPEC.fullmatch(instruction.strip()):
            self.answers["model"] = instruction.strip()
        elif lowered.startswith(("call it ", "name it ")):
            self.answers["name"] = _title(instruction.split(" ", 2)[2].strip())
        else:
            self.answers["purpose"] = f"{self.answers['purpose']} {instruction}".strip()
            self.answers.setdefault("skills", "")
            extra = derive_skills(instruction)
            merged = [
                item
                for item in [*self.answers["skills"].split(","), *extra]
                if item.strip()
            ]
            self.answers["skills"] = ", ".join(dict.fromkeys(i.strip() for i in merged))
        self._build()
        return self.summary()

    # Kept so existing callers and scripts that answered questions still work.
    def answer(self, text: str, provider: str = "ollama", model: str = "qwen3") -> str:
        if self.candidate is None and not self.answers:
            return self.begin(text, provider, model)
        return self.refine(text)

    def _build(self) -> None:
        provider, _, model = self.answers["model"].partition(":")
        if not model:
            provider, model = "ollama", self.answers["model"]
        skills = [item.strip() for item in self.answers["skills"].split(",") if item.strip()]
        access = self.answers["access"]
        candidate = AgentDefinition(
            id=self.candidate.id if self.candidate else str(uuid4()),
            name=self.answers["name"],
            created_by="architect",
            identity=f"A purpose-built agent requested as: {self.answers['initial_need']}",
            purpose=self.answers["purpose"],
            provider=provider.strip(),
            model_name=model.strip(),
            skills=skills,
            permissions=[] if access.lower() == "none" else [access],
            autonomy=Autonomy.CYCLIC,
            status=AgentStatus.CANDIDATE,
        )
        candidate.mind.remember(self.answers["constraints"], source="human")
        # The goal is what the cycle loop picks up the moment it is confirmed, so a
        # new agent starts working instead of waiting to be prompted. It is
        # recurring because a purpose is an ongoing job: an agent whose only goal
        # closes would sit idle forever and look broken.
        candidate.mind.add_goal(self.answers["purpose"], priority=4, recurring=True)
        self.candidate = candidate

    def summary(self) -> str:
        if self.candidate is None:
            return "No candidate drafted yet."
        candidate = self.candidate
        access = candidate.permissions[0] if candidate.permissions else "none"
        return (
            f"Draft ready.\n"
            f"  name:     {candidate.name}\n"
            f"  purpose:  {candidate.purpose}\n"
            f"  model:    {candidate.provider}:{candidate.model_name}\n"
            f"  skills:   {', '.join(candidate.skills) or 'none'}\n"
            f"  access:   {access}\n"
            f"  first goal: {candidate.mind.goals[0].description}\n"
            "Type /confirm to activate it, /cancel to discard it, or just tell me what to "
            "change (for example: name: Scout, or model: ollama:qwen3:4b)."
        )

    def confirm(self) -> AgentDefinition:
        if self.candidate is None:
            raise ValueError("No candidate agent is awaiting confirmation")
        self.candidate.status = AgentStatus.ACTIVE
        result = self.candidate
        self.candidate = None
        self.answers = {}
        return result
