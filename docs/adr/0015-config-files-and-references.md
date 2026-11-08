# ADR-0015: Office values in config files with secret references

- Status: accepted
- Date: 2026-11-08

## Context
Phase 1 must run offline with mocks and later on the office laptop with real repositories, Jira, GitHub and models, without code changes.

## Decision
All office values live in config/*.yaml (providers, repos/*, jira, github, approvers, sandbox), validated by Pydantic in libs/config. Secrets are references only (env:NAME or kv:NAME); a plain value is rejected at load time. Real files are git-ignored; committed *.example.yaml files point every feature at mocks and are used when the real file is absent. `hapm config check` resolves every reference without printing it and lists what is live, mocked or missing; `--pilot` exits 2 unless everything is live and data_approval is recorded. The gateway's providers file moved to config/providers.example.yaml.

## Options considered
Environment variables only (no structure, no validation); a settings service (another component).

## Consequences
The same images run offline and in the office. The config folder is mounted read-only into containers.
