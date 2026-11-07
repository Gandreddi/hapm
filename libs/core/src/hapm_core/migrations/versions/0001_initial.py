"""Initial Phase 0 schema and least-privilege roles.

Revision ID: 0001
"""

import os

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_TABLES = [
    "tasks",
    "runs",
    "approvals",
    "outbox",
    "processed_events",
    "llm_usage",
    "memory_records",
    "tracker_stories",
    "tracker_comments",
    "repo_pull_requests",
    "eval_results",
    "oneview_events",
]


def upgrade() -> None:
    app_password = os.environ.get("HAPM_APP_DB_PASSWORD", "hapm-app-local").replace("'", "''")
    ro_password = os.environ.get("HAPM_READONLY_DB_PASSWORD", "hapm-ro-local").replace("'", "''")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hapm_app') THEN
            CREATE ROLE hapm_app LOGIN PASSWORD '{app_password}';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hapm_readonly') THEN
            CREATE ROLE hapm_readonly LOGIN PASSWORD '{ro_password}';
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE tasks (
          id text PRIMARY KEY,
          agent_id text NOT NULL,
          requester text NOT NULL,
          input jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE runs (
          id text PRIMARY KEY,
          task_id text NOT NULL REFERENCES tasks(id),
          agent_id text NOT NULL,
          requester text NOT NULL,
          state text NOT NULL,
          mode text NOT NULL DEFAULT 'live',
          config_hash text NOT NULL,
          autonomy text NOT NULL,
          output jsonb,
          error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX runs_task_idx ON runs(task_id);
        CREATE TABLE approvals (
          id text PRIMARY KEY,
          run_id text NOT NULL REFERENCES runs(id),
          task_id text NOT NULL,
          status text NOT NULL DEFAULT 'pending',
          what text NOT NULL,
          why text NOT NULL,
          proposed_action jsonb NOT NULL,
          risk text NOT NULL,
          preview text NOT NULL,
          approval_policy_id text NOT NULL,
          requested_by text NOT NULL,
          decided_by text,
          decision text,
          comment text,
          edited_args jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          expires_at timestamptz NOT NULL,
          decided_at timestamptz
        );
        CREATE INDEX approvals_status_idx ON approvals(status);
        CREATE TABLE audit_log (
          seq bigserial PRIMARY KEY,
          id uuid NOT NULL UNIQUE,
          ts timestamptz NOT NULL,
          run_id text,
          task_id text,
          actor text NOT NULL,
          action text NOT NULL,
          resource text NOT NULL,
          decision text NOT NULL,
          policy_rule_id text,
          inputs_hash text,
          outputs_hash text,
          trace_id text,
          prev_hash text NOT NULL,
          row_hash text NOT NULL
        );
        CREATE INDEX audit_run_idx ON audit_log(run_id);
        CREATE TABLE outbox (
          id uuid PRIMARY KEY,
          topic text NOT NULL,
          type text NOT NULL,
          payload jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          published_at timestamptz
        );
        CREATE INDEX outbox_unpublished_idx ON outbox(created_at) WHERE published_at IS NULL;
        CREATE TABLE processed_events (
          consumer text NOT NULL,
          event_id text NOT NULL,
          processed_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (consumer, event_id)
        );
        CREATE TABLE llm_usage (
          id bigserial PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(),
          agent_id text NOT NULL,
          team_id text NOT NULL,
          run_id text,
          task_id text,
          alias text NOT NULL,
          provider text NOT NULL,
          model text NOT NULL,
          prompt_tokens integer NOT NULL,
          completion_tokens integer NOT NULL,
          cost_usd numeric(12, 6) NOT NULL,
          latency_ms double precision NOT NULL,
          status text NOT NULL
        );
        CREATE INDEX llm_usage_run_idx ON llm_usage(run_id);
        CREATE TABLE memory_records (
          id text PRIMARY KEY,
          scope text NOT NULL,
          scope_key text NOT NULL,
          content text NOT NULL,
          metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          embedding vector(1536),
          created_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          source_run_id text,
          expires_at timestamptz
        );
        CREATE INDEX memory_scope_idx ON memory_records(scope, scope_key);
        CREATE TABLE tracker_stories (
          id text PRIMARY KEY,
          title text NOT NULL,
          body text NOT NULL,
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE tracker_comments (
          id text PRIMARY KEY,
          story_id text NOT NULL REFERENCES tracker_stories(id),
          body text NOT NULL,
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE repo_pull_requests (
          id text PRIMARY KEY,
          repo text NOT NULL,
          title text NOT NULL,
          branch text NOT NULL,
          body text NOT NULL,
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE eval_results (
          id text PRIMARY KEY,
          kind text NOT NULL,
          agent_id text NOT NULL,
          suite_id text,
          version text,
          config_hash text NOT NULL,
          run_id text,
          aggregate double precision NOT NULL,
          threshold double precision NOT NULL,
          passed boolean NOT NULL,
          cases jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX eval_agent_idx ON eval_results(agent_id, config_hash, created_at DESC);
        CREATE TABLE oneview_events (
          seq bigserial PRIMARY KEY,
          event_id text NOT NULL UNIQUE,
          type text NOT NULL,
          subject text,
          time timestamptz NOT NULL,
          data jsonb NOT NULL
        );
        CREATE INDEX oneview_subject_idx ON oneview_events(subject, seq);
        """
    )
    tables = ", ".join(APP_TABLES)
    op.execute(
        f"""
        GRANT USAGE ON SCHEMA public TO hapm_app, hapm_readonly;
        GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO hapm_app;
        GRANT SELECT, INSERT ON audit_log TO hapm_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM hapm_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hapm_app;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO hapm_readonly;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hapm_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO hapm_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO hapm_readonly;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Phase 0 has no downgrade; run make clean to reset local data.")
