from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ModelUnavailableError(RuntimeError):
    pass


class ToolsUnsupportedError(RuntimeError):
    """The provider or the model has no tool-calling in its chat template.

    Not a failure. Most models that fit on a small card cannot call tools, and
    the harness answers this by driving them with a text protocol instead, so
    what this exception means is "take the other front end", not "give up".
    """


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    # OpenAI-compatible servers correlate a tool result with the call by id.
    # Ollama does not send one, so we mint it and both dialects stay one shape.
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class ChatTurn:
    """One answer from the model: what it said, and what it wants run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ChatMessage:
    """A transcript entry in our own shape, translated per provider dialect.

    Keeping our own shape is what lets the same transcript drive an Ollama
    ``/api/chat`` call, an OpenAI-compatible one, and the text protocol for a
    model that can do neither.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


def _parse_arguments(raw: object) -> dict[str, Any]:
    """Tool arguments arrive as an object from Ollama and a string from OpenAI."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tools_are_unsupported(exc: httpx.HTTPStatusError) -> bool:
    """Whether a 4xx is the server saying this model has no tools.

    Ollama answers 400 with "does not support tools"; llama.cpp and vLLM word it
    differently. Matching on the word rather than the sentence keeps one refusal
    from being reported to a human as an unreachable provider.
    """
    if exc.response.status_code not in (400, 404, 422, 501):
        return False
    return "tool" in exc.response.text.lower()


def describe(exc: Exception) -> str:
    """A message a human can act on.

    httpx raises timeouts with an empty str(), so a bare str(exc) reaches the
    console as "Model error for ollama:qwen3:" with nothing after the colon.
    """
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


class ModelProvider(Protocol):
    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str: ...

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
    ) -> ChatTurn: ...

    async def health(self) -> tuple[bool, str]: ...

    async def list_models(self) -> list[str]: ...


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 600,
        num_ctx: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        # Sent as `options.num_ctx` on every call. Unset, Ollama loads the model
        # at its own default (2048 tokens on most Modelfiles) no matter how
        # generous the caller's character budgets are, and the server truncates
        # the prompt from the oldest end -- silently, and before this class ever
        # sees it. Configuring this is what makes the project's own budgets the
        # thing that trims, per the load-bearing rule in CLAUDE.md.
        self.num_ctx = num_ctx

    def _options(self) -> dict[str, Any] | None:
        return {"num_ctx": self.num_ctx} if self.num_ctx else None

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                models = {item["name"].split(":")[0] for item in response.json().get("models", [])}
                if self.model.split(":")[0] not in models:
                    return False, f"Ollama is running, but model '{self.model}' is not installed"
                return True, "ready"
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return False, f"Cannot reach Ollama at {self.base_url}: {describe(exc)}"

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                return sorted(str(item["name"]) for item in response.json().get("models", []))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        body: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
        }
        if options := self._options():
            body["options"] = options
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(f"{self.base_url}/api/generate", json=body)
                response.raise_for_status()
                return str(response.json()["response"])
            except (httpx.HTTPError, KeyError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc

    @staticmethod
    def _wire(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.role == "tool" and message.name:
            payload["tool_name"] = message.name
        return payload

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
    ) -> ChatTurn:
        wire = [ChatMessage(role="system", content=system)] if system else []
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [self._wire(item) for item in wire + messages],
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if options := self._options():
            body["options"] = options
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(f"{self.base_url}/api/chat", json=body)
                response.raise_for_status()
                answer = response.json()["message"]
            except httpx.HTTPStatusError as exc:
                if tools and _tools_are_unsupported(exc):
                    raise ToolsUnsupportedError(describe(exc)) from exc
                raise ModelUnavailableError(describe(exc)) from exc
            except (httpx.HTTPError, KeyError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc
        calls = [
            ToolCall(
                name=str(item["function"]["name"]),
                arguments=_parse_arguments(item["function"].get("arguments")),
            )
            for item in answer.get("tool_calls") or []
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        ]
        return ChatTurn(text=str(answer.get("content") or ""), tool_calls=calls)


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(
                    f"{self.base_url}/models", headers=self._headers
                )
                response.raise_for_status()
            return True, "ready"
        except httpx.HTTPError as exc:
            return False, f"Cannot reach local provider at {self.base_url}: {describe(exc)}"

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/models", headers=self._headers)
                response.raise_for_status()
                return sorted(str(item["id"]) for item in response.json().get("data", []))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key or 'local'}"}

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json={"model": model or self.model, "messages": messages},
                )
                response.raise_for_status()
                return str(response.json()["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc

    @staticmethod
    def _wire(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool":
            payload["tool_call_id"] = message.tool_call_id
        return payload

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
    ) -> ChatTurn:
        wire = [ChatMessage(role="system", content=system)] if system else []
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [self._wire(item) for item in wire + messages],
        }
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions", headers=self._headers, json=body
                )
                response.raise_for_status()
                answer = response.json()["choices"][0]["message"]
            except httpx.HTTPStatusError as exc:
                if tools and _tools_are_unsupported(exc):
                    raise ToolsUnsupportedError(describe(exc)) from exc
                raise ModelUnavailableError(describe(exc)) from exc
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc
        calls = [
            ToolCall(
                name=str(item["function"]["name"]),
                arguments=_parse_arguments(item["function"].get("arguments")),
                id=str(item.get("id") or uuid.uuid4().hex[:12]),
            )
            for item in answer.get("tool_calls") or []
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        ]
        return ChatTurn(text=str(answer.get("content") or ""), tool_calls=calls)


class MockProvider:
    def __init__(
        self,
        responses: list[str] | None = None,
        turns: list[ChatTurn] | None = None,
    ) -> None:
        self.responses = responses or ["Mock response"]
        self.calls: list[dict[str, str | None]] = []
        # None means "this model has no tools", which is the case the harness
        # has to work in anyway -- so it is the default a test gets for free.
        self.turns = turns
        self.chats: list[list[ChatMessage]] = []

    async def health(self) -> tuple[bool, str]:
        return True, "ready"

    async def list_models(self) -> list[str]:
        return ["mock-model", "mock-specialist"]

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
    ) -> ChatTurn:
        if self.turns is None:
            raise ToolsUnsupportedError("mock model has no tool calling")
        self.chats.append(list(messages))
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
