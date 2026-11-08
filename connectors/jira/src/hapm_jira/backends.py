"""Jira backends behind one interface. Both return the same plain shapes."""

import base64
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from hapm_config import JiraConfig, resolve_ref
from hapm_core.ids import new_id
from sqlalchemy import Engine, text

Issue = dict[str, Any]


class JiraError(Exception):
    pass


class JiraBackend(Protocol):
    def get_issue(self, key: str) -> Issue: ...

    def list_epic_issues(self, epic_key: str) -> list[Issue]: ...

    def search(self, project: str, text_query: str | None, labels: list[str], limit: int) -> list[Issue]: ...

    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        epic_key: str | None,
        key: str,
    ) -> Issue: ...

    def add_comment(self, issue_key: str, body: str, key: str) -> dict[str, Any]: ...


def _row(r: Any) -> Issue:
    return {
        "key": r.key,
        "project": r.project,
        "type": r.issue_type,
        "summary": r.summary,
        "description": r.description,
        "status": r.status,
        "epic": r.epic_key,
        "labels": list(r.labels),
    }


class MockJira:
    """Postgres-backed Jira stand-in, seeded from tests/fixtures/jira/seed.yaml."""

    def __init__(self, engine: Engine, seed: Path | None = None) -> None:
        self.engine = engine
        if seed and seed.exists():
            data = yaml.safe_load(seed.read_text(encoding="utf-8")) or {}
            with engine.begin() as conn:
                for i in data.get("issues", []):
                    conn.execute(
                        text(
                            "INSERT INTO jira_issues (key, project, issue_type, summary, description, epic_key) "
                            "VALUES (:k, :p, :t, :s, :d, :e) ON CONFLICT (key) DO NOTHING"
                        ),
                        {
                            "k": i["key"],
                            "p": i["project"],
                            "t": i["issue_type"],
                            "s": i["summary"],
                            "d": i.get("description", ""),
                            "e": i.get("epic_key"),
                        },
                    )

    def get_issue(self, key: str) -> Issue:
        with self.engine.connect() as conn:
            r = conn.execute(text("SELECT * FROM jira_issues WHERE key = :k"), {"k": key}).first()
        if r is None:
            raise JiraError(f"issue {key} not found")
        return _row(r)

    def list_epic_issues(self, epic_key: str) -> list[Issue]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM jira_issues WHERE epic_key = :e ORDER BY key"), {"e": epic_key}
            ).all()
        return [_row(r) for r in rows]

    def search(self, project: str, text_query: str | None, labels: list[str], limit: int) -> list[Issue]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM jira_issues WHERE project = :p AND labels @> CAST(:l AS text[]) "
                    "AND (CAST(:q AS text) IS NULL OR summary ILIKE '%' || :q || '%') ORDER BY created_at DESC LIMIT :n"
                ),
                {"p": project, "l": labels, "q": text_query, "n": limit},
            ).all()
        return [_row(r) for r in rows]

    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        epic_key: str | None,
        key: str,
    ) -> Issue:
        with self.engine.begin() as conn:
            existing = conn.execute(text("SELECT * FROM jira_issues WHERE idempotency_key = :i"), {"i": key}).first()
            if existing:
                return {**_row(existing), "created": False}
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:p))"), {"p": project})
            n = conn.execute(text("SELECT count(*) FROM jira_issues WHERE project = :p"), {"p": project}).scalar_one()
            issue_key = f"{project}-{n + 1}"
            conn.execute(
                text(
                    "INSERT INTO jira_issues (key, project, issue_type, summary, description, epic_key, labels, idempotency_key) "
                    "VALUES (:k, :p, :t, :s, :d, :e, :l, :i)"
                ),
                {
                    "k": issue_key,
                    "p": project,
                    "t": issue_type,
                    "s": summary,
                    "d": description,
                    "e": epic_key,
                    "l": labels,
                    "i": key,
                },
            )
            row = conn.execute(text("SELECT * FROM jira_issues WHERE key = :k"), {"k": issue_key}).one()
        return {**_row(row), "created": True}

    def add_comment(self, issue_key: str, body: str, key: str) -> dict[str, Any]:
        self.get_issue(issue_key)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO jira_comments (id, issue_key, body, idempotency_key) VALUES (:id, :k, :b, :i) "
                    "ON CONFLICT (idempotency_key) DO NOTHING"
                ),
                {"id": new_id("cmt"), "k": issue_key, "b": body, "i": key},
            )
            row = conn.execute(text("SELECT id FROM jira_comments WHERE idempotency_key = :i"), {"i": key}).one()
        return {"id": row.id, "issue": issue_key}


def _adf(textual: str) -> dict[str, Any]:
    """Atlassian Document Format for Cloud v3 bodies."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": textual or " "}]}],
    }


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_plain(c) for c in value.get("content", [])) or str(value.get("text", ""))
    return ""


class RestJira:
    """Real Jira REST client. Cloud uses /rest/api/3 with email + API token; Data Center uses /rest/api/2 with a PAT."""

    def __init__(self, cfg: JiraConfig, client: httpx.Client | None = None) -> None:
        if not cfg.site_url:
            raise JiraError("jira.site_url is not configured")
        token = resolve_ref(cfg.token_ref) or ""
        self.cloud = cfg.deployment == "cloud"
        if self.cloud:
            basic = base64.b64encode(f"{resolve_ref(cfg.email_ref)}:{token}".encode()).decode()
            auth = f"Basic {basic}"
        else:
            auth = f"Bearer {token}"
        self.api = "/rest/api/3" if self.cloud else "/rest/api/2"
        self.cfg = cfg
        self.http = client or httpx.Client(
            base_url=cfg.site_url.rstrip("/"), timeout=30, headers={"Authorization": auth, "Accept": "application/json"}
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(4):
            resp = self.http.request(method, f"{self.api}{path}", **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(min(float(resp.headers.get("Retry-After", 2**attempt)), 30))
                continue
            if resp.status_code >= 400:
                raise JiraError(f"Jira {method} {path} failed with {resp.status_code}: {resp.text[:200]}")
            return resp.json() if resp.content else {}
        raise JiraError(f"Jira {method} {path} kept failing after retries")

    def _issue(self, data: dict[str, Any]) -> Issue:
        f = data.get("fields", {})
        parent = (f.get("parent") or {}).get("key")
        return {
            "key": data["key"],
            "project": data["key"].split("-")[0],
            "type": (f.get("issuetype") or {}).get("name"),
            "summary": f.get("summary", ""),
            "description": _plain(f.get("description")),
            "status": (f.get("status") or {}).get("name"),
            "epic": parent,
            "labels": f.get("labels", []),
        }

    def get_issue(self, key: str) -> Issue:
        return self._issue(self._call("GET", f"/issue/{key}"))

    def _jql(self, jql: str, limit: int) -> list[Issue]:
        fields = ["summary", "description", "status", "issuetype", "labels", "parent"]
        if self.cloud:
            data = self._call("POST", "/search/jql", json={"jql": jql, "maxResults": limit, "fields": fields})
        else:
            data = self._call("POST", "/search", json={"jql": jql, "maxResults": limit, "fields": fields})
        return [self._issue(i) for i in data.get("issues", [])]

    def list_epic_issues(self, epic_key: str) -> list[Issue]:
        return self._jql(f'parent = "{epic_key}" ORDER BY key', 100)

    def search(self, project: str, text_query: str | None, labels: list[str], limit: int) -> list[Issue]:
        clauses = [f'project = "{project}"'] + [f'labels = "{label}"' for label in labels]
        if text_query:
            clauses.append(f'summary ~ "{text_query.replace(chr(34), "")}"')
        return self._jql(" AND ".join(clauses) + " ORDER BY created DESC", limit)

    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
        epic_key: str | None,
        key: str,
    ) -> Issue:
        # Jira has no idempotency header: search for the idempotency label first.
        marker = f"hapm-idem-{key[-24:]}".replace(":", "-")
        existing = self.search(project, None, [marker], 1)
        if existing:
            return {**existing[0], "created": False}
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary[:250],
            "description": _adf(description) if self.cloud else description,
            "labels": [*labels, marker],
        }
        if epic_key:
            fields["parent"] = {"key": epic_key}
        created = self._call("POST", "/issue", json={"fields": fields})
        return {**self.get_issue(created["key"]), "created": True}

    def add_comment(self, issue_key: str, body: str, key: str) -> dict[str, Any]:
        payload = {"body": _adf(body) if self.cloud else body}
        data = self._call("POST", f"/issue/{issue_key}/comment", json=payload)
        return {"id": str(data.get("id")), "issue": issue_key}
