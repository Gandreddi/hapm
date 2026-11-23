# Developer agent pilot report

Generated 2026-11-08 05:02 UTC from the HAPM database. Repositories marked mock use the fixture repository and mock Jira/GitHub, so their numbers prove the plumbing only.

| Repository | Backend | Runs | Index files / chunks | LLM cost (USD) | Findings proposed | Accepted | Rejected | Plans approved | Sandbox runs (passed) | PRs opened | Avg run minutes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| payments-service | mock | 12 | 9 / 9 | 0.0054 | 32 | 4 | 8 | 4 | 4 (4) | 3 | 0.6 |

**Measured precision (human acceptance):** 0.33 (4 of 12 human-reviewed findings accepted)

## Scaling estimate (estimate, not a measurement)

Average per repository run: $0.0005 LLM cost and 0.6 minutes of wall time including human approval waits. Projected for 150 repositories, one scan each: about $0.07 and 1.4 hours of agent time, before human review. Real repositories are larger than the fixture; re-measure after the first real repository.
