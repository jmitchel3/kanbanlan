# Retry transient GitHub 5xx responses in Runner

- Kanbanlan: `KBL-N3ZOODQUYJBJ3N4GMIWP65AGA4`
- Canonical home: `github`
- Canonical request: [#27](https://github.com/jmitchel3/kanbanlan/issues/27)

## Request

## Outcome

A transient GitHub 5xx does not abort a multi-step Kanbanlan command partway through.

## Problem

`Runner.run` in `src/kanbanlan/runner.py` treats every non-zero exit as terminal. During the GitHub incident on 2026-08-17, `kanbanlan init` died at `Checking Project owner` and again at `Copying template GitHub Project` because the GraphQL API was returning HTTP 503 intermittently. Measured roughly 3 of 4 calls failing at peak; the same command succeeded on retry once the incident cleared, with no configuration change.

`init` performs many sequential GitHub mutations, so a single transient failure leaves setup half-applied and forces a manual restart.

Diagnosis is worse than it needs to be because `gh` masks the cause. In `cli/cli` v2.96.0, `pkg/cmd/project/shared/queries/queries.go` `OwnerIDAndType` only inspects GraphQL `NOT_FOUND` errors; anything else, including an HTTP 503, falls through to `errors.New("unknown owner type")`. Kanbanlan surfaced that as an owner-configuration problem when the owner was fine.

## Acceptance

- Retry with bounded backoff on transient GitHub failures: HTTP 502/503/504 and "No server is currently available to service your request".
- Retry only where it is safe; do not silently repeat a mutation that may have already applied.
- Attempt count and backoff are bounded and testable; exhausting retries still raises `CommandError`.
- Consider mapping `gh`'s "unknown owner type" to a hint that names the upstream-outage possibility.

## Decisions

Retry is opt-in per call rather than automatic. `Runner` cannot tell a read from
a write by inspecting argv, and a mutation that reached GitHub may have applied
before the response was lost. Retrying those could double-create an issue or
re-apply a Project mutation. Callers that know the call is a read pass
`retry=True`; everything else keeps the previous single-attempt behavior.

`GitHub.graphql` carries the same opt-in for the same reason: it serves both
read queries and `UPDATE_STATUS_FIELD`. Retry is enabled at the nine read call
sites and nowhere else.

Retry triggers on evidence of an upstream outage, not on failure generally.
`is_transient_failure` matches gateway markers (`no server is currently
available`, HTTP 502/503/504) in command output. A 404, an auth failure, or a
genuine bad owner still fails on the first attempt, so real misconfiguration is
not hidden behind three attempts and three seconds of backoff.

Timeouts are deliberately excluded. `_execute` raises on `TimeoutExpired`
before the retry loop can see a result, so a hung command still fails at the
60-second default rather than hanging for three minutes.

Retry runs regardless of `check`, so probes using `check=False`, such as the
Project scope probe in `ensure_project_scope`, benefit too. Without this, a
transient failure would fall through to the scope-detection branch and be
reported as a missing-scope problem.

`_friendly_error` also grew a branch for `unknown owner type`. The GitHub CLI
emits that whenever owner resolution fails for any reason other than a GraphQL
`NOT_FOUND`, including a 503, so on its own it misdirects the operator toward a
configuration problem that may not exist.

## Verification

- `uv run pytest` — 151 passed, 12 subtests passed.
- `uv run ruff check .` — all checks passed.
- `uv run ruff format --check .` — 52 files already formatted.
- `uv build` — sdist and wheel built.
- End-to-end against a stub `gh` on `PATH` that returns HTTP 503 twice and then
  succeeds. `detect_owner_type` returned `organization` after 3 invocations in
  3.3s, matching the 1s + 2s backoff. With the same stub failing every time, a
  `graphql` mutation used exactly 1 invocation and raised `CommandError`,
  confirming mutations are never repeated.

## Delivered result

`Runner.run` and `Runner.json` accept `retry=False`. When enabled, a command
whose failure looks like an upstream GitHub outage is retried up to
`RETRY_ATTEMPTS` (3) with exponential backoff (1s, then 2s). Exhausting the
attempts still raises `CommandError`, so no failure is swallowed. Single
execution moved into `Runner._execute`, leaving `run` as the retry loop.

`is_transient_failure` is exported so the CLI can describe the same condition
it retries on.

Retry is enabled on the read paths that broke during the 2026-08-17 outage:
repository lookup, owner-type detection, Project listing, the Project scope
probe, Project item and pull request pagination, open issue listing, and the
two `gh issue view` reads. `init`, `ensure`, `refresh`, and `reconcile` all
ride on these.

`_friendly_error` now emits a status-page hint for transient failures and for
`unknown owner type`, instead of the generic "run this directly" text.

Not addressed: `gh` itself masks non-`NOT_FOUND` owner-lookup errors as
`unknown owner type` (`cli/cli` v2.96.0,
`pkg/cmd/project/shared/queries/queries.go`). That is upstream. The new hint
compensates locally but does not fix the underlying report.
