"""Phase 2 tables: the Sentinel knowledge graph, its runs, file index, facts, caches and embeddings.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = [
    "sentinel_runs",
    "sentinel_heads",
    "sentinel_nodes",
    "sentinel_edges",
    "sentinel_files",
    "sentinel_facts",
    "sentinel_cache",
    "sentinel_embeddings",
]


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sentinel_runs (
          id text PRIMARY KEY,
          fleet_run_id text,
          repo text NOT NULL,
          sha text,
          status text NOT NULL,
          mode text NOT NULL DEFAULT 'incremental',
          stage text NOT NULL DEFAULT 'queued',
          engines jsonb NOT NULL DEFAULT '{}',
          precision_tier text,
          stats jsonb NOT NULL DEFAULT '{}',
          llm jsonb NOT NULL DEFAULT '{}',
          error text,
          started_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz
        );
        CREATE INDEX sentinel_runs_repo_idx ON sentinel_runs(repo, started_at DESC);
        CREATE INDEX sentinel_runs_fleet_idx ON sentinel_runs(fleet_run_id);
        CREATE TABLE sentinel_heads (
          repo text PRIMARY KEY,
          sha text NOT NULL,
          run_id text NOT NULL,
          ontology_version text NOT NULL,
          built_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE sentinel_nodes (
          repo text NOT NULL,
          sha text NOT NULL,
          id text NOT NULL,
          type text NOT NULL,
          label text NOT NULL,
          source_file text,
          line_start integer,
          line_end integer,
          tier integer NOT NULL,
          in_default boolean NOT NULL DEFAULT true,
          layer text NOT NULL DEFAULT 'repo',
          summary text,
          attrs jsonb NOT NULL DEFAULT '{}',
          provenance text[] NOT NULL DEFAULT '{}',
          ontology_version text NOT NULL,
          PRIMARY KEY (repo, sha, id)
        );
        CREATE INDEX sentinel_nodes_id_idx ON sentinel_nodes(id);
        CREATE INDEX sentinel_nodes_label_idx ON sentinel_nodes(lower(label));
        CREATE TABLE sentinel_edges (
          id bigserial PRIMARY KEY,
          repo text NOT NULL,
          sha text NOT NULL,
          source text NOT NULL,
          target text NOT NULL,
          relation text NOT NULL,
          confidence text NOT NULL,
          file text,
          line_start integer,
          line_end integer,
          provenance text[] NOT NULL DEFAULT '{}',
          cross_repo boolean NOT NULL DEFAULT false,
          layer text NOT NULL DEFAULT 'repo',
          attrs jsonb NOT NULL DEFAULT '{}'
        );
        CREATE INDEX sentinel_edges_src_idx ON sentinel_edges(source);
        CREATE INDEX sentinel_edges_dst_idx ON sentinel_edges(target);
        CREATE INDEX sentinel_edges_repo_idx ON sentinel_edges(repo, sha, layer);
        CREATE TABLE sentinel_files (
          repo text NOT NULL,
          sha text NOT NULL,
          path text NOT NULL,
          blob_sha text NOT NULL,
          line_count integer NOT NULL,
          PRIMARY KEY (repo, sha, path)
        );
        CREATE TABLE sentinel_facts (
          repo text NOT NULL,
          sha text NOT NULL,
          facts jsonb NOT NULL,
          PRIMARY KEY (repo, sha)
        );
        CREATE TABLE sentinel_cache (
          kind text NOT NULL,
          key text NOT NULL,
          value jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (kind, key)
        );
        -- Untyped vector column: the dimension comes from config (embeddings.dimension) and is stored per row,
        -- so local embedding models never write into the vector(1536) columns fixed by ADR-0020.
        CREATE TABLE sentinel_embeddings (
          repo text NOT NULL,
          sha text NOT NULL,
          node_id text NOT NULL,
          model text NOT NULL,
          dimension integer NOT NULL,
          embedding vector NOT NULL,
          PRIMARY KEY (repo, sha, node_id, model)
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
    raise NotImplementedError("Phase 0-2 have no downgrade; run make clean to reset local data.")
