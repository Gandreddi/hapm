# Known issues

## Open
1. **No commits yet.** git `user.email` on this machine is set to `--list`, so the work is
   uncommitted. Fix the identity, then commit by concern.
2. **CI not yet green.** The first run failed on CI setup and real Python advisories, both fixed;
   see docs/progress.md. Trivy and the exit-demo job have not run on GitHub yet.
3. **Live LLM adapters unverified.** Azure OpenAI and Anthropic adapters are tested against mocked
   HTTP only; no credentials were provided. The demo uses the Fake provider, so eval scores prove the
   plumbing, not answer quality.
4. **Two dev-only npm advisories.** `minimatch` (moderate, through eslint-config-next) and
   `@eslint/plugin-kit` (low, through eslint 9.18). Neither ships in the portal image;
   `pnpm audit --prod` is clean.
5. **One unexplained slow run.** The first `make coverage` run took 64 minutes and failed
   `test_approved_run_succeeds_with_one_story_and_full_audit`. It did not reproduce: the next runs
   took 1m42s to 2m28s, and the only other failure was a test race that is now fixed. If it
   returns, run `make coverage` with `-o faulthandler_timeout=300` to capture where it hangs.
6. **Fake summaries echo the prompt prefix.** The Fake provider summarises the text after the first
   colon, so inputs that themselves start with "Summarise this:" produce odd summaries. Cosmetic.
7. **Audit chain is not externally anchored.** The database owner could rewrite the whole chain
   consistently (ADR-0004); anchoring is in the backlog.

8. **Phase 1 live paths unverified.** The real Jira, GitHub and GitHub Models clients are tested
   only against recorded responses; they will be verified on the office laptop.
9. **Fake-model limits.** With the Fake provider the LLM review finds nothing and model-written edits
   fail, so only mechanically fixable findings (rules R1, R2, R3, R8) can be implemented offline. A
   run whose only approved finding needs a model edit ends as failed after three attempts, by design.
10. **Scan quality is measured on our own fixture.** Recall 0.92 and precision 1.0 come from analysers
    written for the payments_service fixture; real precision is measured by human acceptance in the pilot.
11. **Snapshot reads every file through the connector.** Fine for small repositories; large ones need
    tarball or tree-at-once reads (backlog).

## Fixed during implementation
- Phase 1: the Developer step named `plan` collided with the workflow's own `plan` node; renamed to
  `plan_work`, and `plan`/`finish` are now reserved step ids.
- Phase 1: hidden tests covered several functions, so a correct fix for one still failed; split into
  one hidden test per function.
- Phase 1: the approvals insert gained a value without its column (approvers); fixed.
- `temporalio/temporal:1.1.2` does not exist; pinned `1.10.0`.
- Host-side Python clients hung on `localhost` (IPv6 first on Windows); all clients and Kafka's
  advertised address use 127.0.0.1.
- MCP errors raised inside the SDK session were wrapped in an `ExceptionGroup` and retried as
  network errors; errors are now raised after the session closes.
- MCP SDK 2.x renamed its server API; pinned to 1.x (ADR-0013).
- Approval refusals were audited inside a transaction that then rolled back; refusals now audit in
  their own transaction.
- `:param IS NULL` failed with an ambiguous-type error in Postgres; added explicit casts.
- Portal approve and reject buttons shared one server action and lost the button value (422);
  each button now has its own action.
- Next.js 15.1.6, Playwright 1.49 and PostCSS had advisories; upgraded to Next 15.5.29,
  Playwright 1.65 and PostCSS 8.5.32 (override).
- A `# noqa` comment briefly landed inside the migration's SQL string; replaced by a per-file ignore.
