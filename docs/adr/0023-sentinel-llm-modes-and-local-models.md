# ADR-0023: Sentinel LLM modes, the local_openai provider and the Copilot rule

- Status: accepted
- Date: 2026-11-22

## Context
Sentinel's graph needs no model, but summaries, capability tags, community labels, embeddings and
plain-language questions do. The office wants a free, in-house option, and asked whether GitHub Copilot
tokens could be used.

## Decision
- **`llm: off | local | azure`** in config/sentinel.yaml (per repository override). `off` is the default
  for the first production launch: no model call is ever made, and enrichment, embeddings and sentinel_ask
  report "disabled" instead of producing anything. Graph, links, tools and validators 1-5 work fully.
- **Gateway provider `local_openai`** for any OpenAI-compatible server on the office machine: Ollama
  (port 11434), llama.cpp llama-server (8080) or vLLM (8000); fields `base_url_ref`, `quantization`,
  `context_tokens`, `max_concurrency` (1 for Ollama and llama.cpp), `timeout_seconds`, `json_mode`.
  `response_format` (JSON schema) is forwarded so outputs are schema-constrained. Sentinel calls models only
  through the gateway (policy, budgets, redaction, usage records), as agent `sentinel`, team `sentinel`.
- **Model health check** (`sentinel doctor`, `hapm config check` when llm is not off, and the pilot):
  base URL reachable, `GET /v1/models` lists the configured model id exactly (otherwise it prints what the
  server offers), a schema-constrained test prompt returns valid JSON, and embeddings have the configured
  dimension. No secret is printed.
- **Embeddings** go to `sentinel_embeddings` (untyped vector plus a dimension column), never into the
  vector(1536) columns of ADR-0020. Changing the embedding model needs `sentinel build --force`.
- **Enrichment** sends one node's code plus its neighbours within `token_cap`, never a whole repository;
  output must match a JSON schema; up to `label_retries` (default 3) retries with backoff; capabilities are
  limited to the controlled list; results are cached by (content, prompt version, alias); each summary
  records provider and model, so Fake output is never mistaken for a real one.
- **GitHub Copilot**: tokens copied from an IDE session, Copilot's private endpoints and proxies that reuse
  Copilot are forbidden. GitHub Models was retired (ADR-0020). The official Copilot SDK (GA June 2026) is the
  only Copilot route, allowed for low-volume `sentinel_ask` only after the admin confirms terms, billing
  and data approval in a new ADR; never for bulk enrichment.
- Free hosted model tiers and Databricks Free Edition (no commercial use) are not allowed for repository
  content.

## Options considered
Copilot as a bulk model (terms and premium-request quotas); hosted free tiers (data leaves the company);
writing local vectors into the 1536 columns (wrong dimension for most local models).

## Consequences
- Validators 6 and 7 label Fake-provider results "plumbing only"; real quality numbers need `llm: local` or
  `azure` and the office model.
- `data_approval` is still required for a local provider, even though code stays in-house.
