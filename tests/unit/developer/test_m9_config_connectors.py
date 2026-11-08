"""M9: config loading and checks, GitHub Models adapter, Jira and GitHub REST backends against recorded responses."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from hapm_config import GithubConfig, JiraConfig, RefError, RepoConfig, check, load_project, resolve_ref
from hapm_gateway.config import GatewayConfig
from hapm_gateway.providers import GitHubModelsProvider, ProviderError, build_provider
from hapm_github.backends import RestGithub
from hapm_github.main import build as build_github
from hapm_jira.backends import JiraError, RestJira
from hapm_jira.main import build as build_jira

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "config"


# ---------- configuration ----------


def test_example_config_loads_and_everything_is_mocked() -> None:
    project = load_project(EXAMPLES)
    assert list(project.repos) == ["payments-service"]
    assert project.sources["jira"] == "jira.example.yaml"
    providers = yaml.safe_load((EXAMPLES / "providers.example.yaml").read_text(encoding="utf-8"))
    GatewayConfig.model_validate(providers)
    report = check(project, providers)
    assert not report.pilot_ready
    assert any("data_approval" in m for m in report.missing)
    assert "llm: only the Fake provider is usable" in report.mocked


def test_secret_values_are_rejected_only_references_allowed() -> None:
    with pytest.raises(ValueError, match="reference"):
        JiraConfig(backend="jira", token_ref="plain-secret-value")
    with pytest.raises(ValueError):
        RepoConfig(name="Bad Name", jira_project="PAY")


def test_refs_resolve_from_env_and_key_vault_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAPM_TEST_TOKEN", "abc")
    assert resolve_ref("env:HAPM_TEST_TOKEN") == "abc"
    (tmp_path / "jira-token").write_text("kv-value\n")
    monkeypatch.setenv("HAPM_KV_MOUNT", str(tmp_path))
    assert resolve_ref("kv:jira-token") == "kv-value"
    assert resolve_ref("env:HAPM_MISSING_X", required=False) is None
    with pytest.raises(RefError):
        resolve_ref("env:HAPM_MISSING_X")


def test_office_config_reports_missing_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "billing.yaml").write_text(
        yaml.safe_dump({"name": "billing", "backend": "github", "github_repo": "acme/billing", "jira_project": "BIL"})
    )
    (tmp_path / "jira.yaml").write_text(
        yaml.safe_dump(
            {
                "backend": "jira",
                "site_url": "https://acme.atlassian.net",
                "allowed_projects": ["BIL"],
                "email_ref": "env:JIRA_EMAIL_T",
                "token_ref": "env:JIRA_TOKEN_T",
            }
        )
    )
    (tmp_path / "github.yaml").write_text(yaml.safe_dump({"backend": "github", "token_ref": "env:GH_TOKEN_T"}))
    providers = {
        "data_approval": "SEC-123",
        "providers": {"github-models": {"type": "github_models", "token_ref": "env:GHM_T"}},
    }
    report = check(load_project(tmp_path), providers)
    assert {"jira.token_ref: env:JIRA_TOKEN_T is not set", "github.token_ref: env:GH_TOKEN_T is not set"} <= set(
        report.missing
    )
    for name in ("JIRA_EMAIL_T", "JIRA_TOKEN_T", "GH_TOKEN_T", "GHM_T"):
        monkeypatch.setenv(name, "x")
    report = check(load_project(tmp_path), providers)
    assert report.pilot_ready, report.missing
    assert "llm providers: github-models" in report.live


# ---------- GitHub Models adapter ----------


@respx.mock
def test_github_models_adapter_chat_embed_and_errors() -> None:
    chat = respx.post("https://models.github.ai/inference/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )
    )
    respx.post("https://models.github.ai/inference/embeddings").mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.5]}], "usage": {"prompt_tokens": 2}}
        )
    )

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            p = GitHubModelsProvider("ghm", "https://models.github.ai/inference", "tok", client)
            r = await p.chat("m", [{"role": "user", "content": "x"}], 5, {})
            assert (r.content, r.prompt_tokens) == ("hi", 4)
            assert chat.calls.last.request.headers["authorization"] == "Bearer tok"
            assert json.loads(chat.calls.last.request.content)["model"] == "m"
            assert (await p.embed("e", ["x"])).vectors == [[0.5]]
            chat.mock(return_value=httpx.Response(401))
            with pytest.raises(ProviderError) as e:
                await p.chat("m", [{"role": "user", "content": "x"}], None, {})
            assert not e.value.retryable

    asyncio.run(run())


def test_github_models_provider_needs_token(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = GatewayConfig.model_validate(
        yaml.safe_load((EXAMPLES / "providers.example.yaml").read_text(encoding="utf-8"))
    )
    monkeypatch.delenv("GITHUB_MODELS_TOKEN", raising=False)
    assert build_provider("github-models", cfg.providers["github-models"], httpx.AsyncClient()) is None
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "t")
    assert isinstance(
        build_provider("github-models", cfg.providers["github-models"], httpx.AsyncClient()), GitHubModelsProvider
    )


# ---------- Jira REST against recorded responses ----------

ISSUE = {
    "key": "BIL-7",
    "fields": {
        "summary": "Fix totals",
        "description": {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Totals drift"}]}],
        },
        "status": {"name": "To Do"},
        "issuetype": {"name": "Bug"},
        "labels": ["x"],
        "parent": {"key": "BIL-1"},
    },
}


@pytest.fixture
def jira(monkeypatch: pytest.MonkeyPatch) -> RestJira:
    monkeypatch.setenv("JIRA_EMAIL_T", "me@acme.test")
    monkeypatch.setenv("JIRA_TOKEN_T", "tok")
    monkeypatch.setattr("time.sleep", lambda s: None)
    return RestJira(
        JiraConfig(
            backend="jira",
            site_url="https://acme.atlassian.net",
            allowed_projects=["BIL"],
            email_ref="env:JIRA_EMAIL_T",
            token_ref="env:JIRA_TOKEN_T",
        )
    )


@respx.mock
def test_jira_cloud_read_search_create_comment(jira: RestJira) -> None:
    base = "https://acme.atlassian.net/rest/api/3"
    get = respx.get(f"{base}/issue/BIL-7").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=ISSUE)]
    )
    issue = jira.get_issue("BIL-7")
    assert get.call_count == 2  # retried after 429
    get.mock(side_effect=None, return_value=httpx.Response(200, json=ISSUE))
    assert issue == {
        "key": "BIL-7",
        "project": "BIL",
        "type": "Bug",
        "summary": "Fix totals",
        "description": "Totals drift",
        "status": "To Do",
        "epic": "BIL-1",
        "labels": ["x"],
    }
    search = respx.post(f"{base}/search/jql").mock(return_value=httpx.Response(200, json={"issues": [ISSUE]}))
    assert jira.list_epic_issues("BIL-1")[0]["key"] == "BIL-7"
    assert 'parent = "BIL-1"' in json.loads(search.calls.last.request.content)["jql"]
    search.mock(return_value=httpx.Response(200, json={"issues": []}))
    create = respx.post(f"{base}/issue").mock(return_value=httpx.Response(201, json={"key": "BIL-7"}))
    made = jira.create_issue("BIL", "Bug", "Fix totals", "Totals drift", ["hapm"], "BIL-1", "run_1:tickets:f1")
    sent = json.loads(create.calls.last.request.content)["fields"]
    assert made["created"] and sent["parent"] == {"key": "BIL-1"} and sent["description"]["type"] == "doc"
    assert any(label.startswith("hapm-idem-") for label in sent["labels"])
    search.mock(return_value=httpx.Response(200, json={"issues": [ISSUE]}))
    assert jira.create_issue("BIL", "Bug", "Fix totals", "d", [], None, "run_1:tickets:f1")["created"] is False
    respx.post(f"{base}/issue/BIL-7/comment").mock(return_value=httpx.Response(201, json={"id": "100"}))
    assert jira.add_comment("BIL-7", "hello", "k") == {"id": "100", "issue": "BIL-7"}
    assert create.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
def test_jira_errors_and_data_center(monkeypatch: pytest.MonkeyPatch, jira: RestJira) -> None:
    respx.get("https://acme.atlassian.net/rest/api/3/issue/BIL-9").mock(return_value=httpx.Response(404, text="nope"))
    with pytest.raises(JiraError, match="404"):
        jira.get_issue("BIL-9")
    dc = RestJira(
        JiraConfig(
            backend="jira", deployment="data_center", site_url="https://jira.acme.test", token_ref="env:JIRA_TOKEN_T"
        )
    )
    route = respx.get("https://jira.acme.test/rest/api/2/issue/BIL-7").mock(
        return_value=httpx.Response(200, json=ISSUE)
    )
    dc.get_issue("BIL-7")
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


def test_jira_tools_limited_to_allowed_projects() -> None:
    class Stub:
        def get_issue(self, key: str) -> dict[str, Any]:
            return {"key": key}

    server = build_jira(Stub(), ["BIL"], ["localhost"], lambda: True)  # type: ignore[arg-type]

    async def run() -> None:
        tools = {t.name for t in await server.list_tools()}
        assert {
            "jira_get_epic",
            "jira_list_epic_issues",
            "jira_search_issues",
            "jira_get_issue",
            "jira_create_issue",
            "jira_add_comment",
        } == tools
        await server.call_tool("jira_get_issue", {"issue_key": "BIL-1"})
        with pytest.raises(Exception, match="allowed_projects"):
            await server.call_tool("jira_get_issue", {"issue_key": "HR-1"})

    asyncio.run(run())


# ---------- GitHub REST against recorded responses ----------


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> RestGithub:
    monkeypatch.setenv("GH_TOKEN_T", "pat")
    monkeypatch.setattr("time.sleep", lambda s: None)
    return RestGithub(GithubConfig(backend="github", token_ref="env:GH_TOKEN_T"))


REPO = RepoConfig(name="billing", backend="github", github_repo="acme/billing", jira_project="BIL")


@respx.mock
def test_github_read_paths(gh: RestGithub) -> None:
    api = "https://api.github.com/repos/acme/billing"
    respx.get(f"{api}/branches/main").mock(return_value=httpx.Response(200, json={"commit": {"sha": "c0ffee"}}))
    respx.get(f"{api}/git/trees/c0ffee").mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "a.py", "type": "blob", "sha": "b1", "size": 3},
                    {"path": "src", "type": "tree", "sha": "t1"},
                ]
            },
        )
    )
    respx.get(f"{api}/contents/a.py").mock(
        return_value=httpx.Response(200, json={"content": base64.b64encode(b"x=1").decode()})
    )
    respx.get(f"{api}/commits").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(200, json=[{"sha": "c0ffee", "commit": {"message": "init", "author": {"name": "dev"}}}]),
        ]
    )
    assert gh.head(REPO) == "c0ffee"
    assert gh.list_tree(REPO, "") == [{"path": "a.py", "sha": "b1", "size": 3}]
    assert gh.read_file(REPO, "a.py", "c0ffee") == "x=1"
    assert gh.list_commits(REPO, 5)[0]["message"] == "init"


@respx.mock
def test_github_push_branch_and_pull_request(gh: RestGithub) -> None:
    api = "https://api.github.com/repos/acme/billing"
    respx.get(f"{api}/git/ref/heads/hapm/run1").mock(return_value=httpx.Response(404))
    respx.get(f"{api}/git/commits/base1").mock(return_value=httpx.Response(200, json={"tree": {"sha": "tree0"}}))
    tree = respx.post(f"{api}/git/trees").mock(return_value=httpx.Response(201, json={"sha": "tree1"}))
    respx.post(f"{api}/git/commits").mock(return_value=httpx.Response(201, json={"sha": "commit1"}))
    ref = respx.post(f"{api}/git/refs").mock(return_value=httpx.Response(201, json={}))
    out = gh.push_branch(REPO, "hapm/run1", "base1", {"a.py": "x=2"}, "fix", "k")
    assert out == {"branch": "hapm/run1", "commit_sha": "commit1"}
    assert json.loads(tree.calls.last.request.content)["base_tree"] == "tree0"
    assert json.loads(ref.calls.last.request.content)["ref"] == "refs/heads/hapm/run1"
    respx.get(f"{api}/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{api}/pulls").mock(
        return_value=httpx.Response(201, json={"id": 9, "number": 3, "html_url": "https://x/3"})
    )
    assert gh.open_pull_request(REPO, "hapm/run1", "main", "t", "b", "k")["number"] == 3
    assert ref.calls.last.request.headers["authorization"] == "Bearer pat"


@respx.mock
def test_github_app_installation_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for name, value in {"APP_ID_T": "123", "INST_T": "456", "KEY_T": pem}.items():
        monkeypatch.setenv(name, value)
    client = RestGithub(
        GithubConfig(
            backend="github", app_id_ref="env:APP_ID_T", installation_id_ref="env:INST_T", private_key_ref="env:KEY_T"
        )
    )
    token = respx.post("https://api.github.com/app/installations/456/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "inst-tok"})
    )
    respx.get("https://api.github.com/repos/acme/billing/branches/main").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "s"}})
    )
    client.head(REPO)
    client.head(REPO)
    assert token.call_count == 1  # cached until near expiry


def test_github_tools_have_no_merge_and_guard_branches() -> None:
    project = load_project(EXAMPLES)

    class Stub:
        def push_branch(self, *a: Any) -> dict[str, Any]:
            return {"ok": True}

    server = build_github(project, {"mock": Stub()}, ["localhost"], lambda: True)  # type: ignore[dict-item]

    async def run() -> None:
        names = {t.name for t in await server.list_tools()}
        forbidden = [
            n for n in names if any(w in n for w in ("merge", "delete", "force", "settings", "protect", "admin"))
        ]
        assert not forbidden, forbidden
        assert {"github_push_branch", "github_open_pull_request", "github_read_file", "github_list_tree"} <= names
        with pytest.raises(Exception, match="hapm"):
            await server.call_tool(
                "github_push_branch",
                {
                    "repo": "payments-service",
                    "branch": "main",
                    "base_sha": "x",
                    "files": {"a": "b"},
                    "message": "m",
                    "idempotency_key": "k",
                },
            )
        with pytest.raises(Exception, match="not onboarded"):
            await server.call_tool("github_read_file", {"repo": "not-onboarded", "path": "a"})
        ok = await server.call_tool(
            "github_push_branch",
            {
                "repo": "payments-service",
                "branch": "hapm/x",
                "base_sha": "x",
                "files": {"a": "b"},
                "message": "m",
                "idempotency_key": "k",
            },
        )
        assert ok

    asyncio.run(run())
