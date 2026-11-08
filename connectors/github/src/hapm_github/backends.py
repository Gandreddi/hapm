"""GitHub backends behind one interface. Writes are limited to new hapm/* branches and pull requests."""

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt
from hapm_codeintel.snapshot import blob_sha
from hapm_config import GithubConfig, RepoConfig, resolve_ref
from hapm_core.ids import new_id
from sqlalchemy import Engine, text


class GithubError(Exception):
    pass


class GithubBackend(Protocol):
    def head(self, repo: RepoConfig) -> str: ...

    def list_tree(self, repo: RepoConfig, ref: str) -> list[dict[str, Any]]: ...

    def read_file(self, repo: RepoConfig, path: str, ref: str) -> str: ...

    def list_commits(self, repo: RepoConfig, limit: int) -> list[dict[str, Any]]: ...

    def list_pull_requests(self, repo: RepoConfig) -> list[dict[str, Any]]: ...

    def push_branch(
        self, repo: RepoConfig, branch: str, base_sha: str, files: dict[str, str], message: str, key: str
    ) -> dict[str, Any]: ...

    def open_pull_request(
        self, repo: RepoConfig, head: str, base: str, title: str, body: str, key: str
    ) -> dict[str, Any]: ...


class MockGithub:
    """Serves a fixture folder as the default branch; pushed branches and PRs live in Postgres."""

    def __init__(self, engine: Engine, root: Path) -> None:
        self.engine = engine
        self.root = root

    def _files(self, repo: RepoConfig) -> dict[str, str]:
        if not repo.mock_path:
            raise GithubError(f"repo {repo.name} has no mock_path")
        base = (self.root / repo.mock_path).resolve()
        return {
            p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(base.rglob("*"))
            if p.is_file()
        }

    def head(self, repo: RepoConfig) -> str:
        listing = json.dumps(sorted((p, blob_sha(t)) for p, t in self._files(repo).items()))
        return hashlib.sha1(listing.encode(), usedforsecurity=False).hexdigest()

    def _at(self, repo: RepoConfig, ref: str) -> dict[str, str]:
        files = self._files(repo)
        if ref in ("", repo.default_branch, self.head(repo)):
            return files
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT files FROM github_branches WHERE repo = :r AND (name = :n OR commit_sha = :n)"),
                {"r": repo.name, "n": ref},
            ).first()
        if row is None:
            raise GithubError(f"unknown ref {ref}")
        return {**files, **row.files}

    def list_tree(self, repo: RepoConfig, ref: str) -> list[dict[str, Any]]:
        return [{"path": p, "sha": blob_sha(t), "size": len(t.encode())} for p, t in self._at(repo, ref).items()]

    def read_file(self, repo: RepoConfig, path: str, ref: str) -> str:
        files = self._at(repo, ref)
        if path not in files:
            raise GithubError(f"{path} not found at {ref or repo.default_branch}")
        return files[path]

    def list_commits(self, repo: RepoConfig, limit: int) -> list[dict[str, Any]]:
        return [{"sha": self.head(repo), "message": "fixture snapshot", "author": "fixture"}][:limit]

    def list_pull_requests(self, repo: RepoConfig) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, number, title, head, base, state FROM github_pull_requests WHERE repo = :r "
                    "ORDER BY number DESC"
                ),
                {"r": repo.name},
            ).all()
        return [dict(r._mapping) for r in rows]

    def push_branch(
        self, repo: RepoConfig, branch: str, base_sha: str, files: dict[str, str], message: str, key: str
    ) -> dict[str, Any]:
        commit = hashlib.sha1(
            f"{base_sha}{json.dumps(files, sort_keys=True)}{message}".encode(), usedforsecurity=False
        ).hexdigest()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO github_branches (repo, name, base_sha, commit_sha, message, files, idempotency_key) "
                    "VALUES (:r, :n, :b, :c, :m, CAST(:f AS jsonb), :k) ON CONFLICT DO NOTHING"
                ),
                {
                    "r": repo.name,
                    "n": branch,
                    "b": base_sha,
                    "c": commit,
                    "m": message,
                    "f": json.dumps(files),
                    "k": key,
                },
            )
            row = conn.execute(
                text("SELECT commit_sha FROM github_branches WHERE repo = :r AND name = :n"),
                {"r": repo.name, "n": branch},
            ).one()
        return {"branch": branch, "commit_sha": row.commit_sha}

    def open_pull_request(
        self, repo: RepoConfig, head: str, base: str, title: str, body: str, key: str
    ) -> dict[str, Any]:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO github_pull_requests (id, repo, title, body, head, base, idempotency_key) "
                    "VALUES (:i, :r, :t, :b, :h, :ba, :k) ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"i": new_id("pr"), "r": repo.name, "t": title, "b": body, "h": head, "ba": base, "k": key},
            )
            row = conn.execute(
                text("SELECT id, number FROM github_pull_requests WHERE idempotency_key = :k"), {"k": key}
            ).one()
        return {"id": row.id, "number": row.number, "url": f"mock://{repo.name}/pull/{row.number}"}


class RestGithub:
    """Real GitHub REST client (github.com or Enterprise Server), GitHub App or fine-grained PAT auth."""

    def __init__(self, cfg: GithubConfig, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.http = client or httpx.Client(base_url=cfg.api_url.rstrip("/"), timeout=30)
        self._token: str | None = None
        self._expires = 0.0

    def _auth(self) -> str:
        if self.cfg.token_ref:
            return resolve_ref(self.cfg.token_ref) or ""
        if self._token and time.time() < self._expires - 60:
            return self._token
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iat": now - 30, "exp": now + 540, "iss": resolve_ref(self.cfg.app_id_ref)},
            resolve_ref(self.cfg.private_key_ref) or "",
            algorithm="RS256",
        )
        installation = resolve_ref(self.cfg.installation_id_ref)
        resp = self.http.post(
            f"/app/installations/{installation}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code >= 400:
            raise GithubError(f"GitHub App token request failed with {resp.status_code}")
        self._token = str(resp.json()["token"])
        self._expires = time.time() + 3600
        return self._token

    def _call(self, method: str, path: str, ok404: bool = False, **kwargs: Any) -> Any:
        for attempt in range(4):
            resp = self.http.request(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {self._auth()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                **kwargs,
            )
            limited = resp.status_code == 429 or (
                resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"
            )
            if limited or resp.status_code >= 500:
                time.sleep(min(float(resp.headers.get("Retry-After", 2**attempt)), 30))
                continue
            if ok404 and resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise GithubError(f"GitHub {method} {path} failed with {resp.status_code}: {resp.text[:200]}")
            return resp.json() if resp.content else {}
        raise GithubError(f"GitHub {method} {path} kept failing after retries")

    @staticmethod
    def _full(repo: RepoConfig) -> str:
        if not repo.github_repo:
            raise GithubError(f"repo {repo.name} has no github_repo")
        return repo.github_repo

    def head(self, repo: RepoConfig) -> str:
        return str(self._call("GET", f"/repos/{self._full(repo)}/branches/{repo.default_branch}")["commit"]["sha"])

    def list_tree(self, repo: RepoConfig, ref: str) -> list[dict[str, Any]]:
        data = self._call(
            "GET", f"/repos/{self._full(repo)}/git/trees/{ref or self.head(repo)}", params={"recursive": "1"}
        )
        return [
            {"path": e["path"], "sha": e["sha"], "size": e.get("size", 0)} for e in data["tree"] if e["type"] == "blob"
        ]

    def read_file(self, repo: RepoConfig, path: str, ref: str) -> str:
        data = self._call(
            "GET", f"/repos/{self._full(repo)}/contents/{path}", params={"ref": ref or repo.default_branch}
        )
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def list_commits(self, repo: RepoConfig, limit: int) -> list[dict[str, Any]]:
        data = self._call(
            "GET", f"/repos/{self._full(repo)}/commits", params={"sha": repo.default_branch, "per_page": limit}
        )
        return [
            {"sha": c["sha"], "message": c["commit"]["message"], "author": c["commit"]["author"]["name"]} for c in data
        ]

    def list_pull_requests(self, repo: RepoConfig) -> list[dict[str, Any]]:
        data = self._call("GET", f"/repos/{self._full(repo)}/pulls", params={"state": "open", "per_page": 50})
        return [
            {
                "id": p["id"],
                "number": p["number"],
                "title": p["title"],
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "state": p["state"],
            }
            for p in data
        ]

    def push_branch(
        self, repo: RepoConfig, branch: str, base_sha: str, files: dict[str, str], message: str, key: str
    ) -> dict[str, Any]:
        full = self._full(repo)
        existing = self._call("GET", f"/repos/{full}/git/ref/heads/{branch}", ok404=True)
        if existing:  # idempotent: the branch name is unique per run and step
            return {"branch": branch, "commit_sha": existing["object"]["sha"]}
        base_tree = self._call("GET", f"/repos/{full}/git/commits/{base_sha}")["tree"]["sha"]
        tree = self._call(
            "POST",
            f"/repos/{full}/git/trees",
            json={
                "base_tree": base_tree,
                "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()],
            },
        )
        commit = self._call(
            "POST", f"/repos/{full}/git/commits", json={"message": message, "tree": tree["sha"], "parents": [base_sha]}
        )
        self._call("POST", f"/repos/{full}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return {"branch": branch, "commit_sha": commit["sha"]}

    def open_pull_request(
        self, repo: RepoConfig, head: str, base: str, title: str, body: str, key: str
    ) -> dict[str, Any]:
        full = self._full(repo)
        owner = full.split("/")[0]
        open_prs = self._call("GET", f"/repos/{full}/pulls", params={"head": f"{owner}:{head}", "state": "open"})
        if open_prs:
            pr = open_prs[0]
        else:
            pr = self._call(
                "POST", f"/repos/{full}/pulls", json={"title": title, "body": body, "head": head, "base": base}
            )
        return {"id": pr["id"], "number": pr["number"], "url": pr["html_url"]}
