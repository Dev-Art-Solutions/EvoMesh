from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .mesh import Mesh


async def _post_with_retry(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """POST, retrying up to twice more on a 503 before handing the response
    back to the caller's own raise_for_status()/except handling.

    Found live: pointing a system agent at Gemini's OpenAI-compatible
    endpoint, roughly half of this mesh's real harness/propose calls came
    back "503 ... currently experiencing high demand ... temporary" within
    minutes of switching -- while every manually-replicated request of the
    same shape (plain chat, a large prompt, tool schemas, a system message,
    all tried separately) succeeded on the first try. Discarding a whole
    harness job -- and the generation it was working on -- over one
    transient server blip is a worse failure than waiting a few seconds.
    Deliberately narrow: only 503 is retried here, so a real refusal (401,
    404, a genuine 429 quota exhaustion) still reaches the caller as exactly
    one request, unchanged from before this existed.
    """
    response = await client.post(url, **kwargs)
    for attempt in range(2):
        if response.status_code != 503:
            return response
        await asyncio.sleep(2.0 * (attempt + 1))
        response = await client.post(url, **kwargs)
    return response


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
    # Gemini-specific, carried opaquely: its OpenAI-compatible layer 400s a
    # later turn in the same conversation ("Function call is missing a
    # thought_signature... required for tools to work correctly") unless this
    # exact value, minted on the turn that made the call, is echoed back on
    # that same tool_calls entry when it is replayed as history. Found live,
    # 2026-09-23: a real repair job on gemini-3.5-flash-lite hit this on its
    # second turn, fell back to the text protocol mid-job, and the weaker
    # fallback then failed to actually repair anything -- the generation was
    # discarded that would otherwise have landed. None on every other
    # provider dialect, which never populates or reads it.
    thought_signature: str | None = None


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


def _extract_thought_signature(item: dict[str, Any]) -> str | None:
    """Gemini's OpenAI-compatible layer nests this under a tool_call entry's
    own ``extra_content.google.thought_signature`` -- see ToolCall's own
    docstring for why it must be captured here and echoed back later."""
    extra = item.get("extra_content")
    if not isinstance(extra, dict):
        return None
    google = extra.get("google")
    if not isinstance(google, dict):
        return None
    signature = google.get("thought_signature")
    return signature if isinstance(signature, str) else None


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
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str: ...

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
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

    def _options(self, num_ctx: int | None) -> dict[str, Any] | None:
        effective = num_ctx if num_ctx is not None else self.num_ctx
        return {"num_ctx": effective} if effective else None

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
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
        }
        if options := self._options(num_ctx):
            body["options"] = options
        if format is not None:
            # Ollama's own grammar-constrained decoding: the whole response
            # is forced to validate against this JSON Schema. Used by the
            # harness's text-protocol fallback (see harness.py's
            # structured_fallback) to make a malformed/fabricated tool-call
            # envelope structurally impossible instead of merely unlikely.
            body["format"] = format
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await _post_with_retry(
                    client, f"{self.base_url}/api/generate", json=body
                )
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
        num_ctx: int | None = None,
    ) -> ChatTurn:
        wire = [ChatMessage(role="system", content=system)] if system else []
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [self._wire(item) for item in wire + messages],
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if options := self._options(num_ctx):
            body["options"] = options
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await _post_with_retry(client, f"{self.base_url}/api/chat", json=body)
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
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        # No OpenAI-compatible equivalent to Ollama's options.num_ctx exists in
        # the chat-completions spec; a server this points at sizes its own
        # context (e.g. vLLM's --max-model-len), so the argument is accepted
        # for interface parity with OllamaProvider and otherwise unused.
        del num_ctx
        # Same parity reasoning for `format`: Ollama's grammar-constrained
        # decoding has no OpenAI chat-completions equivalent reachable from
        # this thin wrapper.
        del format
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await _post_with_retry(
                    client,
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
            wired_calls = []
            for call in message.tool_calls:
                entry: dict[str, Any] = {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                if call.thought_signature is not None:
                    # Gemini-only: see ToolCall.thought_signature's own
                    # docstring. Echoed back in the exact shape Gemini's own
                    # response used it in, not invented here.
                    entry["extra_content"] = {
                        "google": {"thought_signature": call.thought_signature}
                    }
                wired_calls.append(entry)
            payload["tool_calls"] = wired_calls
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
        num_ctx: int | None = None,
    ) -> ChatTurn:
        del num_ctx  # see generate(): no equivalent on this dialect
        wire = [ChatMessage(role="system", content=system)] if system else []
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [self._wire(item) for item in wire + messages],
        }
        if tools:
            body["tools"] = tools
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await _post_with_retry(
                    client, f"{self.base_url}/chat/completions", headers=self._headers, json=body
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
                thought_signature=_extract_thought_signature(item),
            )
            for item in answer.get("tool_calls") or []
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        ]
        return ChatTurn(text=str(answer.get("content") or ""), tool_calls=calls)


class AnthropicProvider:
    """Claude via Anthropic's own Messages API.

    A genuinely different wire format from every other provider here (Ollama's
    ``/api/chat``, and any OpenAI-compatible ``/chat/completions`` server --
    OpenAI itself, OpenRouter, a local vLLM/llama.cpp): ``system`` is a
    top-level field rather than a message in the list, a tool result rides
    back as a ``tool_result`` content block on a *user* turn instead of its
    own ``tool``-role message, tool schemas are named ``input_schema`` rather
    than ``parameters``, and auth is an ``x-api-key`` header plus a required
    ``anthropic-version`` rather than a bearer token. All of that is
    translated at the edges here so the rest of this project keeps one
    provider-neutral shape (``ChatMessage``/``ChatTurn``/``ToolCall``).
    """

    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 600,
        max_output_tokens: int = 8192,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key or "", "anthropic-version": self.ANTHROPIC_VERSION}

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers)
                response.raise_for_status()
            return True, "ready"
        except httpx.HTTPError as exc:
            return False, f"Cannot reach Anthropic at {self.base_url}: {describe(exc)}"

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/models", headers=self._headers)
                response.raise_for_status()
                return sorted(str(item["id"]) for item in response.json().get("data", []))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        del num_ctx  # see chat(): no equivalent on this dialect
        del format  # see OpenAICompatibleProvider.generate(): same, no equivalent
        turn = await self.chat(
            [ChatMessage(role="user", content=prompt)], system=system, model=model
        )
        return turn.text

    @staticmethod
    def _content_blocks(message: ChatMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return blocks

    @classmethod
    def _wire_messages(cls, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        """Translate the shared transcript into Anthropic's turn shape.

        harness.py appends one ``tool``-role ChatMessage per call the
        previous assistant turn made, back to back. Anthropic expects every
        ``tool_use`` block from an assistant turn answered together in the
        single user turn that follows it -- one ``tool_result`` block per
        call, not one user turn per result -- so consecutive ``tool``
        entries are merged here rather than sent as separate turns.
        """
        wire: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []

        def flush() -> None:
            if pending:
                wire.append({"role": "user", "content": pending.copy()})
                pending.clear()

        for message in messages:
            if message.role == "tool":
                pending.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
                continue
            flush()
            wire.append({"role": message.role, "content": cls._content_blocks(message)})
        flush()
        return wire

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> ChatTurn:
        del num_ctx  # see generate(): no equivalent on this dialect
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_output_tokens,
            "messages": self._wire_messages(messages),
        }
        if system:
            body["system"] = system
        if tools:
            # This project's tool schemas are OpenAI's function-calling shape
            # everywhere (Tool.schema() in harness_tools.py) -- translated to
            # Anthropic's here rather than making every caller dialect-aware.
            body["tools"] = [
                {
                    "name": entry["function"]["name"],
                    "description": entry["function"].get("description", ""),
                    "input_schema": entry["function"].get("parameters", {}),
                }
                for entry in tools
                if entry.get("type") == "function" and "function" in entry
            ]
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await _post_with_retry(
                    client, f"{self.base_url}/messages", headers=self._headers, json=body
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                if tools and _tools_are_unsupported(exc):
                    raise ToolsUnsupportedError(describe(exc)) from exc
                raise ModelUnavailableError(describe(exc)) from exc
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                raise ModelUnavailableError(describe(exc)) from exc
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        name=str(block.get("name", "")),
                        arguments=block.get("input") or {},
                        id=str(block.get("id") or uuid.uuid4().hex[:12]),
                    )
                )
        return ChatTurn(text="".join(text_parts), tool_calls=calls)


class MockProvider:
    def __init__(
        self,
        responses: list[str] | None = None,
        turns: list[ChatTurn] | None = None,
    ) -> None:
        self.responses = responses or ["Mock response"]
        self.calls: list[dict[str, Any]] = []
        # None means "this model has no tools", which is the case the harness
        # has to work in anyway -- so it is the default a test gets for free.
        self.turns = turns
        self.chats: list[list[ChatMessage]] = []

    async def health(self) -> tuple[bool, str]:
        return True, "ready"

    async def list_models(self) -> list[str]:
        return ["mock-model", "mock-specialist"]

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
        format: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model,
                "num_ctx": num_ctx,
                "format": format,
            }
        )
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        system: str = "",
        model: str | None = None,
        num_ctx: int | None = None,
    ) -> ChatTurn:
        del num_ctx
        if self.turns is None:
            raise ToolsUnsupportedError("mock model has no tool calling")
        self.chats.append(list(messages))
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]


class NetworkModel:
    """The graph view the agents reason over, backed by a :class:`Mesh`.

    The agents never touch the mesh directly -- they reason through the nodes
    and edges they can see. ``NetworkModel`` is that single entry point: it
    holds a ``self.mesh`` and passes every graph question straight through to
    it, keyed on the same string node ids, while still keeping ``self.edges``
    as an additional view of the same graph.

    These are thin pass-throughs to ``self.mesh``; nothing here computes a
    degree or a neighbour list, so the model can never drift out of sync with
    the mesh it reflects.
    """

    def __init__(self, mesh: Mesh) -> None:
        self.mesh = mesh

    @property
    def edges(self) -> dict[str, dict[str, str]]:
        """The graph's edges, as a view of ``self.mesh.edges``."""
        return self.mesh.edges

    def neighbours(self, node: str) -> list[str]:
        """The nodes ``node`` is connected to, straight from ``self.mesh``."""
        return self.mesh.neighbours(node)

    def out_degree(self, node: str) -> int:
        """How many edges leave ``node``, straight from ``self.mesh``."""
        return self.mesh.out_degree(node)

    def in_degree(self, node: str) -> int:
        """How many edges enter ``node``, straight from ``self.mesh``."""
        return self.mesh.in_degree(node)
