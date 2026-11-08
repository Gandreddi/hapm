"""Phase 1 tables: mock Jira and GitHub data, code index, findings, sandbox runs.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = [
    "jira_issues",
    "jira_comments",
    "github_branches",
    "github_pull_requests",
    "code_chunks",
    "code_index_state",
    "findings",
    "sandbox_runs",
]


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE approvals ADD COLUMN approvers text[];
        CREATE TABLE jira_issues (
          key text PRIMARY KEY,
          project text NOT NULL,
          issue_type text NOT NULL,
          summary text NOT NULL,
          description text NOT NULL DEFAULT '',
          status text NOT NULL DEFAULT 'To Do',
          epic_key text,
          labels text[] NOT NULL DEFAULT '{}',
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX jira_issues_epic_idx ON jira_issues(epic_key);
        CREATE TABLE jira_comments (
          id text PRIMARY KEY,
          issue_key text NOT NULL REFERENCES jira_issues(key),
          body text NOT NULL,
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE github_branches (
          repo text NOT NULL,
          name text NOT NULL,
          base_sha text NOT NULL,
          commit_sha text NOT NULL,
          message text NOT NULL,
          files jsonb NOT NULL,
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (repo, name)
        );
        CREATE TABLE github_pull_requests (
          id text PRIMARY KEY,
          repo text NOT NULL,
          number serial,
          title text NOT NULL,
          body text NOT NULL,
          head text NOT NULL,
          base text NOT NULL,
          state text NOT NULL DEFAULT 'open',
          idempotency_key text UNIQUE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE code_index_state (
          repo text NOT NULL,
          sha text NOT NULL,
          files integer NOT NULL,
          chunks integer NOT NULL,
          skipped integer NOT NULL,
          reused integer NOT NULL,
          indexed_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (repo, sha)
        );
        CREATE TABLE code_chunks (
          id bigserial PRIMARY KEY,
          repo text NOT NULL,
          sha text NOT NULL,
          path text NOT NULL,
          blob_sha text NOT NULL,
          language text NOT NULL,
          start_line integer NOT NULL,
          end_line integer NOT NULL,
          content text NOT NULL,
          embedding vector(1536)
        );
        CREATE INDEX code_chunks_repo_sha_idx ON code_chunks(repo, sha);
        CREATE INDEX code_chunks_blob_idx ON code_chunks(repo, blob_sha);
        CREATE TABLE findings (
          id text PRIMARY KEY,
          run_id text,
          repo text NOT NULL,
          sha text NOT NULL,
          path text NOT NULL,
          start_line integer NOT NULL,
          end_line integer NOT NULL,
          symbol text,
          rule text NOT NULL,
          kind text NOT NULL,
          severity text NOT NULL,
          confidence double precision NOT NULL,
          fingerprint text NOT NULL,
          evidence text NOT NULL,
          proposed_change text NOT NULL,
          test_approach text NOT NULL,
          source text NOT NULL,
          status text NOT NULL DEFAULT 'proposed',
          ticket_key text,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX findings_run_idx ON findings(run_id);
        CREATE INDEX findings_fp_idx ON findings(repo, fingerprint);
        CREATE TABLE sandbox_runs (
          id text PRIMARY KEY,
          run_id text,
          repo text NOT NULL,
          sha text NOT NULL,
          attempt integer NOT NULL,
          exit_code integer,
          timed_out boolean NOT NULL,
          duration_ms integer NOT NULL,
          diff_lines integer NOT NULL,
          result jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    tables = ", ".join(TABLES)
    op.execute(
        f"""
        GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO hapm_app;
        GRANT SELECT ON {tables} TO hapm_readonly;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hapm_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("Phase 0/1 have no downgrade; run make clean to reset local data.")
