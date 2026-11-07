"""GET /v1/audit with filters and GET /v1/audit/verify."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query
from hapm_core.audit import verify_db
from hapm_core.db import make_engine
from hapm_core.settings import CoreSettings
from hapm_telemetry import configure_logging, configure_tracing, health_router
from sqlalchemy import text


class AuditSettings(CoreSettings):
    service_name: str = "audit"


def create_app(settings: AuditSettings | None = None) -> FastAPI:
    settings = settings or AuditSettings()
    configure_logging(settings.service_name, settings.log_level)
    configure_tracing(settings.service_name, settings.otel_endpoint)
    engine = make_engine(settings.database_url)
    app = FastAPI(title="HAPM audit", version="0.1.0")

    async def db_ready() -> bool:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1")).scalar() == 1

    app.include_router(health_router({"postgres": db_ready}))

    @app.get("/v1/audit")
    def list_audit(
        run_id: Annotated[str | None, Query(max_length=128)] = None,
        task_id: Annotated[str | None, Query(max_length=128)] = None,
        actor: Annotated[str | None, Query(max_length=128)] = None,
        action: Annotated[str | None, Query(max_length=128)] = None,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        clauses, params = [], {"limit": limit}
        for name, value in (("run_id", run_id), ("task_id", task_id), ("actor", actor)):
            if value is not None:
                clauses.append(f"{name} = :{name}")
                params[name] = value
        if action is not None:
            clauses.append("action LIKE :action")
            params["action"] = action.replace("*", "%")
        if since is not None:
            clauses.append("ts >= :since")
            params["since"] = since
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT * FROM audit_log {where} ORDER BY seq LIMIT :limit"),  # noqa: S608
                params,
            ).all()
        return {"items": [_jsonable(dict(r._mapping)) for r in rows], "count": len(rows)}

    @app.get("/v1/audit/verify")
    def verify() -> dict[str, Any]:
        with engine.connect() as conn:
            result = verify_db(conn)
        return {
            "ok": result.ok,
            "rows_checked": result.rows_checked,
            "first_bad_seq": result.first_bad_seq,
            "reason": result.reason,
        }

    return app


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, datetime) else str(v) if k == "id" else v) for k, v in row.items()}
