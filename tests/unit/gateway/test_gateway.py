"""M2 DONE checks against the Fake provider: routing, fallback, budgets, redaction, error mapping."""

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from hapm_gateway.budgets import MemoryCounter
from hapm_gateway.config import GatewayConfig
from hapm_gateway.main import create_app, tcp_check
from hapm_gateway.providers import (
    AnthropicProvider,
    AzureOpenAIProvider,
    FakeProvider,
    ProviderError,
    build_provider,
    fake_embedding,
    map_http_error,
)
from hapm_gateway.recorder import MemoryRecorder
from hapm_gateway.settings import GatewaySettings
from hapm_policy import PolicyEngine

ROOT = Path(__file__).resolve().parents[3]
CONFIG = GatewayConfig.load(ROOT / "services/gateway/config.yaml")
HEADERS = {"X-HAPM-Agent-Id": "dummy_echo", "X-HAPM-Team-Id": "core", "X-HAPM-Run-Id": "run_1"}


def make(**kwargs) -> tuple[TestClient, MemoryRecorder]:  # type: ignore[no-untyped-def]
    recorder = MemoryRecorder(PolicyEngine.from_file(ROOT / "policies/default.yaml"))
    app = create_app(
        GatewaySettings(db_port=1, ready_timeout_seconds=0.1),
        config=kwargs.get("config", CONFIG),
        recorder=recorder,
        counter=MemoryCounter(),
        providers=kwargs.get("providers"),
    )
    return TestClient(app), recorder


def chat(client: TestClient, alias: str = "fast", text: str = "summarise: the cat sat", headers=None):  # type: ignore[no-untyped-def]
    return client.post(
        "/v1/chat", json={"model": alias, "messages": [{"role": "user", "content": text}]}, headers=headers or HEADERS
    )


def test_routing_uses_alias_target_and_records_usage() -> None:
    client, recorder = make()
    body = chat(client).json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Summary: the cat sat"
    assert body["hapm"]["provider"] == "fake" and body["hapm"]["provider_model"] == "fake-fast"
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    action, usage = recorder.usages[0]
    assert action == "llm.chat" and usage.cost_usd > 0 and usage.status == "ok"
    assert recorder.decisions[0][2].rule_id == "llm-agents-allowed"


def test_fallback_on_provider_error() -> None:
    client, _ = make()
    body = chat(client, alias="fallback-demo").json()
    assert body["hapm"]["provider"] == "fake"
    assert body["hapm"]["attempts"] == ["fake-broken:502", "fake:ok"]


def test_fallback_on_timeout() -> None:
    cfg = CONFIG.model_copy(deep=True)
    cfg.aliases["fallback-demo"].timeout_seconds = 0.2
    client, _ = make(config=cfg, providers={"fake-broken": FakeProvider("fake-broken"), "fake": FakeProvider("fake")})
    response = client.post(
        "/v1/chat",
        json={"model": "fallback-demo", "messages": [{"role": "user", "content": "hi"}]},
        headers={**HEADERS, "X-HAPM-Fake-Delay": "1"},
    )
    assert response.status_code == 504
    assert "fake-broken:timeout" in response.json()["error"]["message"]


def test_budget_exceeded_returns_429_with_reason() -> None:
    client, _ = make()
    headers = {**HEADERS, "X-HAPM-Team-Id": "tiny-team"}
    assert chat(client, headers=headers).status_code == 200
    response = chat(client, headers=headers)
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "budget_exceeded" and "budget exceeded" in error["message"]


def test_redaction_hook_runs_on_prompt_and_response() -> None:
    client, recorder = make()
    chat(client, text="summarise: mail bob@example.com key sk-abcdefghijklmnopqrstu")
    usage = recorder.usages[0][1]
    stored_prompt = usage.request["messages"][0]["content"]
    assert "bob@example.com" not in stored_prompt and "[REDACTED:email]" in stored_prompt
    assert "[REDACTED:api_key]" in usage.response["content"]


def test_provider_error_mapping_and_unknown_alias() -> None:
    client, _ = make()
    response = chat(client, text="FAKE_PROVIDER_ERROR")
    assert response.status_code == 400 and response.json()["error"]["code"] == "provider_error"
    assert chat(client, alias="nope").status_code == 400
    assert map_http_error(httpx.ReadTimeout("t")).status == 504
    assert map_http_error(httpx.ConnectError("c")).retryable
    req = httpx.Request("POST", "http://x")
    for code, status, retry in [(429, 429, True), (503, 502, True), (401, 502, False), (422, 400, False)]:
        err = map_http_error(httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req)))
        assert (err.status, err.retryable) == (status, retry)
    assert map_http_error(RuntimeError("x")).status == 500


def test_missing_identity_and_bad_input_rejected() -> None:
    client, _ = make()
    assert chat(client, headers={"X-HAPM-Agent-Id": "a"}).status_code == 400
    assert chat(client, headers={"X-HAPM-Agent-Id": "Bad Agent", "X-HAPM-Team-Id": "t"}).status_code == 400
    bad = client.post("/v1/chat", json={"model": "fast", "messages": []}, headers=HEADERS)
    assert bad.status_code == 422


def test_policy_deny_returns_403() -> None:
    recorder = MemoryRecorder(PolicyEngine.from_dict({"rules": [{"id": "nothing", "effect": "deny"}]}))
    app = create_app(GatewaySettings(db_port=1), config=CONFIG, recorder=recorder, counter=MemoryCounter())
    response = TestClient(app).post(
        "/v1/chat", json={"model": "fast", "messages": [{"role": "user", "content": "x"}]}, headers=HEADERS
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "policy_denied"


def test_embeddings_and_similarity() -> None:
    client, recorder = make()
    body = client.post(
        "/v1/embeddings", json={"input": ["release notes", "release notes draft"]}, headers=HEADERS
    ).json()
    a, b = (d["embedding"] for d in body["data"])
    assert len(a) == 1536 and sum(x * y for x, y in zip(a, b, strict=True)) > 0.7
    assert recorder.usages[0][0] == "llm.embed"
    assert client.post("/v1/embeddings", json={"input": []}, headers=HEADERS).status_code == 422
    assert fake_embedding("x") == fake_embedding("x")


def test_fake_judge_and_health() -> None:
    client, _ = make()
    judged = client.post(
        "/v1/chat",
        json={
            "model": "judge",
            "messages": [
                {"role": "system", "content": "You are an evaluation judge."},
                {"role": "user", "content": "rate it"},
            ],
        },
        headers=HEADERS,
    ).json()
    assert '"score": 1.0' in judged["choices"][0]["message"]["content"]
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503


@respx.mock
def test_azure_openai_adapter_against_mocked_http() -> None:
    respx.post("https://aoai.test/openai/deployments/gpt/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "gpt",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )
    )
    respx.post("https://aoai.test/openai/deployments/emb/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 2}}
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            p = AzureOpenAIProvider("aoai", "https://aoai.test", "k", "2024-10-21", client)
            r = await p.chat("gpt", [{"role": "user", "content": "x"}], 10, {})
            assert (r.content, r.prompt_tokens, r.completion_tokens) == ("hi", 3, 1)
            e = await p.embed("emb", ["x"])
            assert e.vectors == [[0.1]]

    asyncio.run(run())


@respx.mock
def test_anthropic_adapter_against_mocked_http_and_errors() -> None:
    route = respx.post(AnthropicProvider.URL)
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "claude",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            p = AnthropicProvider("anthropic", "k", client)
            r = await p.chat("claude", [{"role": "system", "content": "s"}, {"role": "user", "content": "x"}], None, {})
            assert r.content == "ok" and r.prompt_tokens == 5
            assert route.calls.last.request.headers["x-api-key"] == "k"
            route.mock(return_value=httpx.Response(529))
            with pytest.raises(ProviderError) as exc:
                await p.chat("claude", [{"role": "user", "content": "x"}], None, {})
            assert exc.value.retryable
            with pytest.raises(ProviderError):
                await p.embed("m", ["x"])

    asyncio.run(run())


def test_real_providers_unavailable_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    client = httpx.AsyncClient()
    assert build_provider("anthropic", CONFIG.providers["anthropic"], client) is None
    assert build_provider("azure-openai", CONFIG.providers["azure-openai"], client) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(build_provider("anthropic", CONFIG.providers["anthropic"], client), AnthropicProvider)


def test_tcp_check_passes_against_a_listening_socket() -> None:
    async def run() -> bool:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await tcp_check("127.0.0.1", port, 1.0)()
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run())


def test_invalid_config_rejected() -> None:
    data = CONFIG.model_dump()
    data["aliases"]["x"] = {"targets": [{"provider": "missing", "model": "m"}]}
    with pytest.raises(ValueError, match="unknown provider"):
        GatewayConfig.model_validate(data)
