# Backlog

Deliberately left out of Phase 0.

## Developer agent follow-ups
- Read repositories as one tarball or tree request instead of file by file.
- Analysers for languages beyond Python (TypeScript, Java, Go, C#).
- Sandbox on AKS: Kubernetes Jobs in a locked-down namespace with gVisor or Kata.
- Several work items per run; changes spanning several files; build commands with dependency caches.
- Live verification of Jira Cloud and Data Center, GitHub App on Enterprise Server, GitHub Models.

## Security and governance
- External anchoring of the audit hash chain (periodic signed checkpoints outside the database).
- Entra ID authorization code flow in the portal (the dev stub uses the password grant).
- Rate limiting per user on the public APIs (budgets cover LLM spend only).
- Rego/OPA if policy conditions outgrow YAML matchers.
- Sandbox design for code execution on AKS (needed by the Developer agent).

## Platform
- Apply Terraform: private endpoints, VNet integration, diagnostics, role assignments, Temporal on AKS.
- Helm values per environment, HPA, PodDisruptionBudgets, network policies.
- OpenTelemetry collector and trace backend in compose (exporter already configurable).
- Upgrade MCP SDK to 2.x.
- Multiple relay instances and partitioned topics.

## Product
- Real Product, Developer and Quality Engineering agent logic.
- Multi-tenant billing.
- Live smoke tests of the Azure OpenAI and Anthropic adapters once credentials are approved.
