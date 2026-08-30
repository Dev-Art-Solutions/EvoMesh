from __future__ import annotations

from typing import Protocol

import httpx


class ModelUnavailableError(RuntimeError):
    pass


class ModelProvider(Protocol):
    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str: ...

    async def health(self) -> tuple[bool, str]: ...

    async def list_models(self) -> list[str]: ...


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

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
            return False, f"Cannot reach Ollama at {self.base_url}: {exc}"

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                return sorted(str(item["name"]) for item in response.json().get("models", []))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailableError(str(exc)) from exc

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": model or self.model,
                        "prompt": prompt,
                        "system": system,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                return str(response.json()["response"])
            except (httpx.HTTPError, KeyError) as exc:
                raise ModelUnavailableError(str(exc)) from exc


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, model: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    async def health(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(
                    f"{self.base_url}/models", headers=self._headers
                )
                response.raise_for_status()
            return True, "ready"
        except httpx.HTTPError as exc:
            return False, f"Cannot reach local provider at {self.base_url}: {exc}"

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/models", headers=self._headers)
                response.raise_for_status()
                return sorted(str(item["id"]) for item in response.json().get("data", []))
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise ModelUnavailableError(str(exc)) from exc

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key or 'local'}"}

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json={"model": model or self.model, "messages": messages},
                )
                response.raise_for_status()
                return str(response.json()["choices"][0]["message"]["content"])
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                raise ModelUnavailableError(str(exc)) from exc


class MockProvider:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = responses or ["Mock response"]
        self.calls: list[dict[str, str | None]] = []

    async def health(self) -> tuple[bool, str]:
        return True, "ready"

    async def list_models(self) -> list[str]:
        return ["mock-model", "mock-specialist"]

    async def generate(
        self, prompt: str, *, system: str = "", model: str | None = None
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
