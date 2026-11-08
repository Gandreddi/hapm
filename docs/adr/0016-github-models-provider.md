# ADR-0016: GitHub Models API as the office LLM route

- Status: accepted
- Date: 2026-11-08

## Context
The user's office GitHub account has Copilot with several models. Copilot credits are consumed by Copilot clients; a server application needs a real API.

## Decision
Add a github_models adapter (OpenAI-compatible chat and embeddings at https://models.github.ai/inference, bearer token from a fine-grained PAT with models:read, referenced as env:GITHUB_MODELS_TOKEN). Copilot's private IDE endpoints and tokens copied from editors are never used. Which models are available, and how usage is billed, is set by the organisation; record the admin's answer when known.

## Options considered
Reverse-engineered Copilot endpoints (breaks terms of use); Azure OpenAI or Anthropic directly (adapters already exist and remain available).

## Consequences
The Fake provider stays the default, so everything works without a token. Live calls are unverified until the token exists.
