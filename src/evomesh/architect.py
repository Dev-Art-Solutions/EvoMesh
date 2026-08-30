from __future__ import annotations

from dataclasses import dataclass, field

from evomesh.contracts import AgentDefinition, AgentStatus, MindState


@dataclass
class ArchitectInterview:
    """Deterministic interview state; the LLM can enrich answers later."""

    answers: dict[str, str] = field(default_factory=dict)
    candidate: AgentDefinition | None = None

    QUESTIONS = (
        ("name", "What should the new agent be called?"),
        ("purpose", "What should this agent accomplish?"),
        ("constraints", "What must it never do, and how will success be evaluated?"),
        ("access", "Which folders may it read or write? Use 'none' if it needs no files."),
        ("skills", "Which capabilities or skills does it need?"),
        (
            "model",
            "Which provider and model should it use? Example: ollama:qwen3 or 'default'.",
        ),
    )

    def begin(self, initial_need: str) -> str:
        self.answers = {"initial_need": initial_need}
        self.candidate = None
        return self.QUESTIONS[0][1]

    def answer(self, text: str, provider: str = "ollama", model: str = "qwen3") -> str:
        next_key = next((key for key, _ in self.QUESTIONS if key not in self.answers), None)
        if next_key is None:
            return "The interview is complete. Type /confirm to activate the candidate."
        self.answers[next_key] = text.strip()
        remaining = [(key, question) for key, question in self.QUESTIONS if key not in self.answers]
        if remaining:
            return remaining[0][1]
        skills = [item.strip() for item in self.answers["skills"].split(",") if item.strip()]
        selected_provider, selected_model = provider, model
        model_answer = self.answers["model"].strip()
        if model_answer.lower() != "default":
            if ":" in model_answer:
                selected_provider, selected_model = model_answer.split(":", 1)
            else:
                selected_model = model_answer
        self.candidate = AgentDefinition(
            name=self.answers["name"],
            created_by="architect",
            identity=f"A purpose-built agent requested as: {self.answers['initial_need']}",
            purpose=self.answers["purpose"],
            provider=selected_provider.strip(),
            model_name=selected_model.strip(),
            mind=MindState(
                beliefs=[{"statement": self.answers["constraints"], "source": "human"}],
                goals=[{"description": self.answers["purpose"], "status": "active"}],
            ),
            skills=skills,
            permissions=(
                [] if self.answers["access"].lower() == "none" else [self.answers["access"]]
            ),
            status=AgentStatus.CANDIDATE,
        )
        return (
            f"Candidate '{self.candidate.name}' is ready: {self.candidate.purpose} "
            "Type /confirm to activate it or /cancel to discard it."
        )

    def confirm(self) -> AgentDefinition:
        if self.candidate is None:
            raise ValueError("No candidate agent is awaiting confirmation")
        self.candidate.status = AgentStatus.ACTIVE
        result = self.candidate
        self.candidate = None
        return result
