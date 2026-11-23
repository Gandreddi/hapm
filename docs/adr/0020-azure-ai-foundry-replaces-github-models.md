# ADR-0020: Azure AI Foundry replaces GitHub Models as the office LLM route

- Status: accepted
- Date: 2026-11-19
- Supersedes: ADR-0016

## Context
GitHub retired GitHub Models on 2026-07-30: the catalog, playground and inference API at https://models.github.ai are no longer available (GitHub Docs, "GitHub Models has been retired"). GitHub points to Azure AI Foundry for model access. ADR-0016's adapter can therefore never work.

## Decision
Remove the github_models adapter. The office route is the existing azure_openai adapter, pointed at OpenAI models deployed in the company's Azure AI Foundry (Microsoft Foundry) resource. In config/providers.yaml the target's model is the Foundry deployment name. The embedding deployment must return 1536-dimension vectors to match the vector(1536) columns. A config file that still names github_models fails to load with a message pointing here.

Provider credentials (AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, ANTHROPIC_API_KEY) are passed to the gateway container only; before this change compose passed no Azure values to any container.

## Options considered
GitHub Copilot (an editor assistant, not an API for a server; Copilot tokens stay ruled out as in ADR-0016); Anthropic directly (adapter exists and remains available); a new adapter for Foundry's non-OpenAI model API (deferred: start with OpenAI models, which the existing adapter supports).

## Consequences
The Fake provider stays the default, so everything works without Azure. The Azure adapter is tested against mocked HTTP only; a live call against the office Foundry deployment is still to be verified. Which models, region, auth method (key or Entra ID) and data approval apply is the company's decision; record the admin's answer when known.
