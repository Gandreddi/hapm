"""Provider adapters behind one interface: Fake (tests and demo), Azure OpenAI, Anthropic."""

import asyncio
import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from hapm_gateway.config import ProviderConfig

EMBED_DIM = 1536
JUDGE_MARKER = "You are an evaluation judge"


@dataclass
class ChatResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    prompt_tokens: int
    model: str


class ProviderError(Exception):
    """status is the HTTP status the gateway returns; retryable errors trigger fallback."""

    def __init__(self, status: int, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Provider(Protocol):
    name: str

    async def chat(
        self, model: str, messages: list[dict[str, str]], max_tokens: int | None, options: dict[str, Any]
    ) -> ChatResult: ...

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult: ...


def _words(text: str) -> int:
    return max(1, len(text.split()))


def map_http_error(exc: Exception) -> ProviderError:
    """Map provider transport and HTTP errors to gateway errors."""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(504, "provider timed out", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return ProviderError(429, "provider rate limited", retryable=True)
        if code >= 500:
            return ProviderError(502, f"provider error {code}", retryable=True)
        if code in (401, 403):
            return ProviderError(502, "provider rejected credentials", retryable=False)
        return ProviderError(400, f"provider rejected request ({code})", retryable=False)
    if isinstance(exc, httpx.TransportError):
        return ProviderError(502, "provider unreachable", retryable=True)
    return ProviderError(500, f"unexpected provider error: {exc}", retryable=False)


class FakeProvider:
    """Deterministic: the same input always gives the same output, cost and tokens."""

    def __init__(self, name: str, always_fail: bool = False) -> None:
        self.name = name
        self.always_fail = always_fail

    async def chat(
        self, model: str, messages: list[dict[str, str]], max_tokens: int | None, options: dict[str, Any]
    ) -> ChatResult:
        delay = min(float(options.get("fake_delay_seconds", 0) or 0), 60.0)
        if delay:
            await asyncio.sleep(delay)
        if self.always_fail:
            raise ProviderError(502, "fake provider configured to fail", retryable=True)
        prompt = " ".join(m["content"] for m in messages)
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "FAKE_PROVIDER_ERROR" in user:
            raise ProviderError(400, "fake provider rejected request", retryable=False)
        if any(JUDGE_MARKER in m["content"] for m in messages if m["role"] == "system"):
            score = 0.0 if "FAIL_JUDGE" in user else 1.0
            content = f'{{"score": {score}, "reason": "deterministic fake judge"}}'
        else:
            text = user.split(":", 1)[-1].strip() if ":" in user else user
            words = text.split()
            content = "Summary: " + " ".join(words[:20]) + ("..." if len(words) > 20 else "")
        if max_tokens:
            content = " ".join(content.split()[:max_tokens])
        return ChatResult(content, _words(prompt), _words(content), model)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        if self.always_fail:
            raise ProviderError(502, "fake provider configured to fail", retryable=True)
        return EmbedResult([fake_embedding(t) for t in inputs], sum(_words(t) for t in inputs), model)


def fake_embedding(text: str) -> list[float]:
    """Bag-of-words hashing: texts sharing words get similar vectors. Mechanics only, not quality."""
    vec = [0.0] * EMBED_DIM
    for word in text.lower().split():
        digest = hashlib.sha256(word.strip(".,!?;:").encode()).digest()
        vec[int.from_bytes(digest[:4], "big") % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class AzureOpenAIProvider:
    def __init__(self, name: str, endpoint: str, api_key: str, api_version: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self._base = endpoint.rstrip("/")
        self._key = api_key
        self._version = api_version
        self._client = client

    async def _post(self, deployment: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/openai/deployments/{deployment}/{path}"
        try:
            resp = await self._client.post(
                url, params={"api-version": self._version}, headers={"api-key": self._key}, json=body
            )
            resp.raise_for_status()
        except Exception as exc:
            raise map_http_error(exc) from exc
        data: dict[str, Any] = resp.json()
        return data

    async def chat(
        self, model: str, messages: list[dict[str, str]], max_tokens: int | None, options: dict[str, Any]
    ) -> ChatResult:
        body: dict[str, Any] = {"messages": messages}
        if max_tokens:
            body["max_tokens"] = max_tokens
        data = await self._post(model, "chat/completions", body)
        usage = data.get("usage", {})
        return ChatResult(
            data["choices"][0]["message"]["content"] or "",
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            str(data.get("model", model)),
        )

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        data = await self._post(model, "embeddings", {"input": inputs})
        vectors = [item["embedding"] for item in sorted(data["data"], key=lambda i: i["index"])]
        return EmbedResult(vectors, int(data.get("usage", {}).get("prompt_tokens", 0)), model)


class AnthropicProvider:
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, name: str, api_key: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self._key = api_key
        self._client = client

    async def chat(
        self, model: str, messages: list[dict[str, str]], max_tokens: int | None, options: dict[str, Any]
    ) -> ChatResult:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 1024,
            "messages": [m for m in messages if m["role"] in ("user", "assistant")],
        }
        if system:
            body["system"] = system
        try:
            resp = await self._client.post(
                self.URL,
                headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
                json=body,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise map_http_error(exc) from exc
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        return ChatResult(
            text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), str(data.get("model", model))
        )

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        raise ProviderError(400, "anthropic provider does not offer embeddings", retryable=False)


class GitHubModelsProvider:
    """Official GitHub Models API: OpenAI-compatible chat and embeddings with a bearer token."""

    def __init__(self, name: str, endpoint: str, token: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self._base = endpoint.rstrip("/")
        self._token = token
        self._client = client

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(
                f"{self._base}/{path}",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                json=body,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise map_http_error(exc) from exc
        data: dict[str, Any] = resp.json()
        return data

    async def chat(
        self, model: str, messages: list[dict[str, str]], max_tokens: int | None, options: dict[str, Any]
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens:
            body["max_tokens"] = max_tokens
        data = await self._post("chat/completions", body)
        usage = data.get("usage", {})
        return ChatResult(
            data["choices"][0]["message"].get("content") or "",
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            str(data.get("model", model)),
        )

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        data = await self._post("embeddings", {"model": model, "input": inputs})
        vectors = [item["embedding"] for item in sorted(data["data"], key=lambda i: i["index"])]
        return EmbedResult(vectors, int(data.get("usage", {}).get("prompt_tokens", 0)), model)


def _secret(ref: str | None, legacy_env: str | None) -> str:
    """Resolve env:/kv: references (hapm_config), falling back to the older *_env field names."""
    from hapm_config import resolve_ref

    if ref:
        return resolve_ref(ref, required=False) or ""
    return os.environ.get(legacy_env or "", "")


def build_provider(name: str, cfg: ProviderConfig, client: httpx.AsyncClient) -> Provider | None:
    """Return None when a real provider has no credentials, so it is simply unavailable."""
    if cfg.type == "fake":
        return FakeProvider(name, cfg.always_fail)
    if cfg.type == "github_models":
        token = _secret(cfg.token_ref, None)
        if not (token and cfg.endpoint):
            return None
        return GitHubModelsProvider(name, cfg.endpoint, token, client)
    key = _secret(cfg.api_key_ref, cfg.api_key_env)
    if cfg.type == "azure_openai":
        endpoint = _secret(cfg.endpoint_ref, cfg.endpoint_env) or (cfg.endpoint or "")
        if not (key and endpoint):
            return None
        return AzureOpenAIProvider(name, endpoint, key, cfg.api_version or "2024-10-21", client)
    if not key:
        return None
    return AnthropicProvider(name, key, client)
